"""卡密登录接口 — /api/auth

卡密登录后发放随机 token（secrets.token_urlsafe(32)），存于 sessions 表，
便于即时踢下线（管理员禁用 / 过期时清除会话）。
"""

import base64
import random
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db.session import SessionLocal
from db.models import Card, Session as CardSession
from api.card_helpers import is_expired, mark_expired, card_public_info
from api.deps import get_current_card
from db.init_db import log_card_event

router = APIRouter(prefix="/api/auth", tags=["卡密鉴权"])

# ===== 验证码 + 登录限流（进程内状态，单进程自托管足够） =====
_CAPTCHA_TTL = 300          # 验证码有效期（秒）
_MAX_ATTEMPTS = 3           # 最大失败次数
_LOCK_SECONDS = 3600        # 锁定时长：1 小时
_captchas: dict[str, tuple[str, float]] = {}   # captchaId -> (code, expire_at)
_attempts: dict[str, dict] = {}                # client_ip -> {count, lock_until}
_state_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # 直连部署默认不信 X-Forwarded-For——攻击者每请求换一个伪造 IP 即可让按 IP
    # 计数的失败锁定失效（与 app.py 的 admin_lan_only 中间件同一原则）。
    # 管理端开启 trust_proxy_header（可信反代部署）后才解析 XFF/X-Real-IP，
    # 统一走 core.device_binding.resolve_client_ip（IP 绑定/限流同一来源）。
    from core.device_binding import resolve_client_ip
    return resolve_client_ip(request) or "unknown"


def _gen_captcha() -> tuple[str, str]:
    """生成 4 位字母数字验证码，返回 (code, svg_data_uri)"""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符
    code = "".join(random.choices(chars, k=4))
    w, h = 120, 44
    colors = ["#1e88e5", "#e53935", "#43a047", "#fb8c00", "#8e24aa", "#00897b"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="#f3f5f9"/>',
    ]
    for _ in range(4):
        x1, y1, x2, y2 = (random.randint(0, w), random.randint(0, h),
                          random.randint(0, w), random.randint(0, h))
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{random.choice(colors)}" stroke-width="1" opacity="0.35"/>')
    for i, ch in enumerate(code):
        x = 14 + i * 26
        y = 30 + random.randint(-4, 4)
        fs = random.randint(22, 28)
        rot = random.randint(-18, 18)
        col = random.choice(colors)
        parts.append(f'<text x="{x}" y="{y}" font-size="{fs}" font-family="Arial,monospace" '
                     f'font-weight="bold" fill="{col}" transform="rotate({rot} {x} {y})">{ch}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return code, f"data:image/svg+xml;base64,{b64}"


def _check_lock(ip: str):
    """返回剩余锁定秒数；未锁定返回 None"""
    with _state_lock:
        st = _attempts.get(ip)
        if st and st["lock_until"] > time.time():
            return int(st["lock_until"] - time.time())
        return None


def _register_fail(ip: str):
    with _state_lock:
        st = _attempts.get(ip)
        if not st:
            st = {"count": 0, "lock_until": 0.0}
            _attempts[ip] = st
        st["count"] += 1
        if st["count"] >= _MAX_ATTEMPTS:
            st["lock_until"] = time.time() + _LOCK_SECONDS


def _clear_fail(ip: str):
    with _state_lock:
        _attempts.pop(ip, None)


class LoginRequest(BaseModel):
    code: str
    captchaId: str = ""
    captcha: str = ""
    client: str = "web"     # 登录端类型：web=网页 | extension=插件（默认 web，信息字段）


@router.get("/captcha")
async def get_captcha():
    """获取图形验证码（SVG data URI）。无需登录。"""
    cid = secrets.token_urlsafe(16)
    code, svg = _gen_captcha()
    with _state_lock:
        now = time.time()
        expired = [k for k, v in _captchas.items() if v[1] < now]
        for k in expired:
            del _captchas[k]
        _captchas[cid] = (code, now + _CAPTCHA_TTL)
    return {"captchaId": cid, "svg": svg}


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    """卡密登录：先校验限流/验证码，再校验卡密，发放 token。

    失败 3 次（验证码错误或卡密错误）后，该客户端 IP 锁定 1 小时。
    """
    code = (req.code or "").strip()
    ip = _client_ip(request)

    # 1) 限流：已锁定的客户端直接拒绝
    remain = _check_lock(ip)
    if remain is not None:
        raise HTTPException(
            status_code=423,
            detail=f"尝试次数过多，请于 {remain} 秒（约 {remain // 60} 分钟）后重试",
            headers={"Retry-After": str(remain)},
        )

    if not code:
        raise HTTPException(status_code=400, detail="请输入卡密")

    # 2) 验证码校验（一次性使用，校验即失效）
    if not req.captchaId or not req.captcha:
        _register_fail(ip)
        raise HTTPException(status_code=400, detail="请输入验证码")
    with _state_lock:
        c = _captchas.pop(req.captchaId, None)
    if not c or c[1] < time.time():
        _register_fail(ip)
        raise HTTPException(status_code=400, detail="验证码已过期，请点击刷新")
    if c[0].upper() != (req.captcha or "").strip().upper():
        _register_fail(ip)
        raise HTTPException(status_code=400, detail="验证码错误")

    # 3) 卡密校验
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(code=code).first()
        if not card:
            _register_fail(ip)
            raise HTTPException(status_code=404, detail="卡密不存在")

        if card.status == "disabled":
            _register_fail(ip)
            raise HTTPException(status_code=403, detail="卡密已被禁用，请联系管理员")

        if is_expired(card):
            mark_expired(db, card)
            _register_fail(ip)
            raise HTTPException(status_code=401, detail="卡密已过期")

        # 首次登录（active → used）：days 类型在此开始计时
        if card.status == "active":
            card.status = "used"
            if card.expiry_type == "days" and card.activated_at is None:
                card.activated_at = datetime.now(timezone.utc)

        # ── 网络绑定：同一出口 IP 不限设备，换公网 IP 登录才顶号（开关关闭时完全跳过）──
        from core import device_binding as dvb
        client_type = dvb.normalize_client_type(req.client)   # 信息字段，非法值按 None 记录
        net_key = None
        if dvb.device_binding_enabled():
            net_key = dvb.ip_scope_key(ip)
            if net_key:
                dvb.bind_network_on_login(db, card, client_type, net_key, ip)
            # net_key 为 None（客户端 IP 无法解析）：不建绑定，会话按历史免绑定
            # 会话处理（校验放行），fail-open 不阻断登录。

        # 生成新会话 token
        token = secrets.token_urlsafe(32)
        db.add(CardSession(token=token, card_id=card.id, is_active=True,
                           last_active_at=datetime.now(timezone.utc),
                           client_type=client_type, device_id=net_key, ip=ip))
        card.last_login_at = datetime.now(timezone.utc)
        db.commit()

        _clear_fail(ip)
        log_card_event("login", card.id, f"卡密 {card.code} 登录")
        return {"success": True, "token": token, "card": card_public_info(card)}
    finally:
        db.close()


@router.post("/logout")
async def logout(auth: dict = Depends(get_current_card)):
    """使当前 token 失效"""
    db = SessionLocal()
    try:
        sess = db.query(CardSession).filter_by(token=auth["token"], is_active=True).first()
        if sess:
            sess.is_active = False
            db.commit()
        return {"success": True, "message": "已退出登录"}
    finally:
        db.close()


@router.get("/me")
async def me(auth: dict = Depends(get_current_card)):
    """返回当前卡密信息（code、过期时间、剩余天数、状态）"""
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=auth["card_id"]).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        return {"success": True, "card": card_public_info(card)}
    finally:
        db.close()
