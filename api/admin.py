"""管理员接口 — 挂载于 /api/{admin_path}（admin_path 可在后台配置，默认 admin）

包含：管理员登录、管理员账号修改、卡密 CRUD、卡密续期、卡密绑定接口、接口与密钥配置 CRUD。
所有接口除登录外均要求管理员 token（与业务卡密 token 完全隔离）。
路由前缀由 app.py 启动时按 api_config.admin_path 动态挂载，本文件不带前缀。
"""

import logging
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── 管理员登录限流（进程内状态）：连续失败 5 次锁 15 分钟 ──
# 管理端本就有 LAN 限制兜底，此为第二道防线（防局域网内爆破/脚本试错）。
_admin_attempts: dict[str, dict] = {}
_admin_attempts_lock = threading.Lock()
_ADMIN_MAX_ATTEMPTS = 5
_ADMIN_LOCK_SECONDS = 900


def _admin_check_lock(ip: str) -> int | None:
    """返回剩余锁定秒数；未锁定返回 None"""
    with _admin_attempts_lock:
        st = _admin_attempts.get(ip)
        if st and st["lock_until"] > time.time():
            return int(st["lock_until"] - time.time())
        return None


def _admin_register_fail(ip: str) -> None:
    with _admin_attempts_lock:
        st = _admin_attempts.setdefault(ip, {"count": 0, "lock_until": 0.0})
        st["count"] += 1
        if st["count"] >= _ADMIN_MAX_ATTEMPTS:
            st["lock_until"] = time.time() + _ADMIN_LOCK_SECONDS
            st["count"] = 0
            logger.warning("管理员登录连续失败 %d 次，IP %s 锁定 %d 秒", _ADMIN_MAX_ATTEMPTS, ip, _ADMIN_LOCK_SECONDS)

from db.session import SessionLocal
from db.models import (
    Admin, AdminToken, Card, ApiConfig, Interface, LocalTask, BackendXmAccount,
    Session as CardSession,
)
from api.deps import get_current_admin
from api.card_helpers import card_public_info, is_expired, dump_bound_interfaces, _aware
from core import config as _config
from core import login as login_module
from core import account_manager as _am
from core import device_binding as dvb
from db.init_db import log_card_event

router = APIRouter(tags=["管理后台"])

# ── 请求模型 ──
class AdminLoginRequest(BaseModel):
    username: str
    password: str


class CardGenerateRequest(BaseModel):
    count: int = 1
    expiry_type: str = "fixed"          # fixed / days
    expires_at: str | None = None       # fixed 类型：ISO 时间
    valid_days: int | None = None       # days 类型：有效天数
    note: str | None = None
    interface_names: list[str] | None = None  # 绑定接口名列表；None/空 = 不限制
    inject_xm_cookie: bool = False      # 生成时是否把后端供体池账号复制进新卡（免前端扫码）
    download_mode: str | None = None   # 下载模式：server/local/both；None = both（不限制）
    max_devices: int | None = None     # 设备绑定：每席位允许设备数（默认 1）


class CardInjectRequest(BaseModel):
    backend_ids: list[int] | None = None  # 指定注入的供体账号 id；None/空 = 注入全部供体


class CardPatchRequest(BaseModel):
    note: str | None = None
    status: str | None = None           # disable / enable
    expires_at: str | None = None
    valid_days: int | None = None
    interface_names: list[str] | None = None  # 传数组则覆盖绑定（空数组=取消限制）；不传则不变
    download_mode: str | None = None   # 下载模式：server/local/both；传 None 则不变，传 'both'/非法值 → 取消限制
    max_devices: int | None = None     # 设备绑定：每席位允许设备数（1~10）；不传则不变


class CardRenewRequest(BaseModel):
    days: int | None = None             # 顺延天数（fixed: 在到期时间上加；days: 累加 valid_days）
    expires_at: str | None = None       # 直接指定新的到期时间（仅 fixed 类型）


class ConfigUpdateRequest(BaseModel):
    config: dict                        # {key: value}


class AdminCredentialRequest(BaseModel):
    current_password: str
    new_username: str | None = None     # 不传/空 = 不变
    new_password: str


# ════════════════════════════════════════
#  管理员登录
# ════════════════════════════════════════
@router.post("/login")
async def admin_login(req: AdminLoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    remain = _admin_check_lock(ip)
    if remain is not None:
        raise HTTPException(
            status_code=423,
            detail=f"尝试次数过多，请于 {remain // 60 + 1} 分钟后重试",
            headers={"Retry-After": str(remain)},
        )
    db = SessionLocal()
    try:
        admin = db.query(Admin).filter_by(username=req.username).first()
        if not admin or not bcrypt.checkpw(req.password.encode(), admin.password_hash.encode()):
            _admin_register_fail(ip)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        with _admin_attempts_lock:
            _admin_attempts.pop(ip, None)
        token = secrets.token_urlsafe(32)
        db.add(AdminToken(token=token))
        db.commit()
        return {"success": True, "token": token}
    finally:
        db.close()


# ════════════════════════════════════════
#  管理员账号修改（用户名 / 密码）
# ════════════════════════════════════════
@router.post("/account")
async def change_admin_credential(req: AdminCredentialRequest, _: bool = Depends(get_current_admin)):
    """修改管理员用户名/密码。需验证当前密码定位账号；成功后清空所有管理员会话，强制重新登录。"""
    new_user = (req.new_username or "").strip()
    if new_user:
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", new_user):
            raise HTTPException(status_code=400, detail="用户名需为 2~32 位字母/数字/_/-")
    pw_bytes = req.new_password.encode()
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    if len(pw_bytes) > 72:  # bcrypt 上限 72 字节
        raise HTTPException(status_code=400, detail="新密码过长（bcrypt 最多 72 字节）")
    if new_user and req.new_password == new_user:
        raise HTTPException(status_code=400, detail="新密码不能与用户名相同")
    if req.new_password == req.current_password and not new_user:
        raise HTTPException(status_code=400, detail="新密码与当前密码相同，无需修改")

    db = SessionLocal()
    try:
        # 用当前密码定位操作者（系统无管理员 CRUD，通常仅一条记录；逐条 bcrypt 比对最稳妥）
        admin = next(
            (a for a in db.query(Admin).all()
             if bcrypt.checkpw(req.current_password.encode(), a.password_hash.encode())),
            None,
        )
        if not admin:
            raise HTTPException(status_code=400, detail="当前密码错误")
        if new_user and new_user != admin.username:
            clash = db.query(Admin).filter(Admin.username == new_user, Admin.id != admin.id).first()
            if clash:
                raise HTTPException(status_code=400, detail="用户名已被占用")
            admin.username = new_user
        admin.password_hash = bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode()
        # 凭据变更 → 踢掉所有管理员会话（含当前会话），强制用新凭据重新登录
        db.query(AdminToken).delete()
        db.commit()
        logger.warning("管理员凭据已修改（username=%s），所有管理员会话已失效", admin.username)
        return {"success": True, "message": "管理员账号已更新，请使用新凭据重新登录"}
    finally:
        db.close()


# ════════════════════════════════════════
#  卡密管理
# ════════════════════════════════════════
def _gen_card_code(db) -> str:
    """生成全局唯一的卡密号：XM-XXXX-XXXX-XXXX"""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        block = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
        code = f"XM-{block()}-{block()}-{block()}"
        if not db.query(Card).filter_by(code=code).first():
            return code


def _validate_interface_names(db, names: list[str] | None) -> str | None:
    """校验绑定的接口名存在，返回入库字符串（None/空列表 → NULL 不限制）"""
    if not names:
        return None
    clean = [str(n).strip() for n in names if str(n).strip()]
    if not clean:
        return None
    existing = {r.name for r in db.query(Interface.name).all()}
    unknown = [n for n in clean if n not in existing]
    if unknown:
        raise HTTPException(status_code=400, detail=f"接口不存在: {', '.join(unknown)}")
    return dump_bound_interfaces(clean)


@router.post("/cards/generate")
async def generate_cards(req: CardGenerateRequest, _: bool = Depends(get_current_admin)):
    if req.expiry_type not in ("fixed", "days"):
        raise HTTPException(status_code=400, detail="expiry_type 必须为 fixed 或 days")
    if req.count < 1 or req.count > 500:
        raise HTTPException(status_code=400, detail="count 需在 1~500 之间")

    expires_at_dt = None
    if req.expiry_type == "fixed":
        if not req.expires_at:
            raise HTTPException(status_code=400, detail="fixed 类型必须提供 expires_at")
        try:
            expires_at_dt = datetime.fromisoformat(req.expires_at)
            if expires_at_dt.tzinfo is None:
                expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="expires_at 格式错误（请用 ISO 8601）")
    else:
        # days 类型必须给有效天数，否则激活后永不过期（card_helpers 对空 valid_days 不判过期）
        if not req.valid_days or req.valid_days < 1:
            raise HTTPException(status_code=400, detail="days 类型必须提供 valid_days（≥1）")
        if req.valid_days > 36500:
            raise HTTPException(status_code=400, detail="valid_days 不能超过 36500")

    db = SessionLocal()
    try:
        bound_str = _validate_interface_names(db, req.interface_names)
        # 下载模式：仅允许 server/local，其它（含 both/None/非法）按 both 处理（存 NULL）
        dm = req.download_mode if req.download_mode in ("server", "local") else None
        # 设备绑定数：1~10，缺省 1
        md = req.max_devices if req.max_devices and 1 <= req.max_devices <= 10 else 1
        created = []
        created_ids = []
        for _ in range(req.count):
            card = Card(
                code=_gen_card_code(db),
                status="active",
                expiry_type=req.expiry_type,
                expires_at=expires_at_dt,
                valid_days=req.valid_days if req.expiry_type == "days" else None,
                note=req.note,
                bound_interfaces=bound_str,
                download_mode=dm,
                max_devices=md,
            )
            db.add(card)
            db.flush()
            created.append(card.code)
            created_ids.append(card.id)
        db.commit()
        injected_summary = None
        if req.inject_xm_cookie:
            # 生成时把后端供体池账号复制进每张新卡（免前端扫码即可下官方音频）
            injected_summary = {}
            for cid in created_ids:
                injected_summary[cid] = _am.inject_all_backend_cookies(cid)
        return {
            "success": True,
            "count": len(created),
            "codes": created,
            "injected": injected_summary,
        }
    finally:
        db.close()


@router.get("/cards")
async def list_cards(
    _: bool = Depends(get_current_admin),
    status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    db = SessionLocal()
    try:
        page = max(1, page)
        page_size = max(1, min(200, page_size))  # 负数 LIMIT 在 SQLite 等于无上限，必须钳制
        q = db.query(Card)
        if status:
            q = q.filter_by(status=status)
        if keyword:
            q = q.filter(Card.code.ilike(f"%{keyword}%"))
        total = q.count()
        cards = q.order_by(Card.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {
            "success": True,
            "total": total,
            "page": page,
            "page_size": page_size,
            "cards": [card_public_info(c) for c in cards],
        }
    finally:
        db.close()


@router.get("/cards/{card_id}")
async def get_card(card_id: int, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        return {"success": True, "card": card_public_info(card)}
    finally:
        db.close()


@router.patch("/cards/{card_id}")
async def patch_card(card_id: int, req: CardPatchRequest, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")

        if req.note is not None:
            card.note = req.note

        if req.status == "disable":
            card.status = "disabled"
        elif req.status == "enable":
            # 启用：若已激活过则回到 used，否则 active；过期的不强行启用
            if is_expired(card):
                raise HTTPException(status_code=400, detail="卡密已过期，无法启用")
            card.status = "used" if card.activated_at else "active"

        if req.expires_at is not None:
            if card.expiry_type != "fixed":
                raise HTTPException(status_code=400, detail="该卡密为 days 类型，不能修改 expires_at")
            try:
                dt = datetime.fromisoformat(req.expires_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                card.expires_at = dt
            except ValueError:
                raise HTTPException(status_code=400, detail="expires_at 格式错误")

        if req.valid_days is not None:
            if card.expiry_type != "days":
                raise HTTPException(status_code=400, detail="该卡密为 fixed 类型，不能修改 valid_days")
            if req.valid_days < 1 or req.valid_days > 36500:
                raise HTTPException(status_code=400, detail="valid_days 需在 1~36500 之间（0/空会变成永久卡）")
            card.valid_days = req.valid_days

        if req.interface_names is not None:
            # 传数组则覆盖绑定；空数组 = 取消限制（不限制可用接口）
            card.bound_interfaces = _validate_interface_names(db, req.interface_names)

        if req.download_mode is not None:
            # 传 'both'/非法值 → NULL（=both，取消限制）；server/local 则限定
            card.download_mode = req.download_mode if req.download_mode in ("server", "local") else None

        if req.max_devices is not None:
            if req.max_devices < 1 or req.max_devices > 10:
                raise HTTPException(status_code=400, detail="max_devices 需在 1~10 之间")
            card.max_devices = req.max_devices

        # 修改到期时间/有效天数后，若按新值实际未过期，复活 expired 状态
        # （is_expired 对 status==expired 恒真，必须绕开它按日期直接判断）
        if card.status == "expired":
            now = datetime.now(timezone.utc)
            alive = False
            if card.expiry_type == "fixed":
                alive = card.expires_at is not None and _aware(card.expires_at) > now
            elif card.expiry_type == "days" and card.activated_at is not None and card.valid_days:
                alive = _aware(card.activated_at) + timedelta(days=card.valid_days) > now
            if alive:
                card.status = "used" if card.activated_at else "active"
                log_card_event("renew", card.id, f"卡密 {card.code} 经 PATCH 修改有效期后复活")

        db.commit()
        return {"success": True, "card": card_public_info(card)}
    finally:
        db.close()


@router.post("/cards/{card_id}/renew")
async def renew_card(card_id: int, req: CardRenewRequest, _: bool = Depends(get_current_admin)):
    """卡密续期。

    - days：顺延 N 天。fixed 类型在当前到期时间（已过期则从现在开始）上加；days 类型累加 valid_days。
    - expires_at：直接指定新的到期时间（仅 fixed 类型）。
    - 已过期的卡密续期后自动复活（expired → used/active）；注意过期时数据已清零，复活不恢复历史任务/记录。
    """
    if req.days is None and req.expires_at is None:
        raise HTTPException(status_code=400, detail="必须提供 days 或 expires_at 之一")

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")

        now = datetime.now(timezone.utc)
        was_expired = card.status == "expired" or is_expired(card)
        detail_parts = []

        if req.days is not None:
            if req.days < 1 or req.days > 36500:
                raise HTTPException(status_code=400, detail="days 需在 1~36500 之间")
            if card.expiry_type == "fixed":
                base = card.expires_at
                if base is not None and base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
                if base is None or base < now:
                    base = now
                card.expires_at = base + timedelta(days=req.days)
                detail_parts.append(f"到期时间顺延 {req.days} 天至 {card.expires_at.isoformat()}")
            else:
                card.valid_days = (card.valid_days or 0) + req.days
                detail_parts.append(f"有效天数累加 {req.days} 天（共 {card.valid_days} 天）")

        if req.expires_at is not None:
            if card.expiry_type != "fixed":
                raise HTTPException(status_code=400, detail="该卡密为 days 类型，请使用 days 续期")
            try:
                dt = datetime.fromisoformat(req.expires_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail="expires_at 格式错误（请用 ISO 8601）")
            card.expires_at = dt
            detail_parts.append(f"到期时间设定为 {dt.isoformat()}")

        # 续期后仍未过期 → 复活 expired / 保持原状态
        if was_expired and not is_expired(card):
            card.status = "used" if card.activated_at else "active"
            detail_parts.append("卡密已复活")
        elif is_expired(card):
            raise HTTPException(status_code=400, detail="续期后仍处于过期状态，请增加续期时长")

        db.commit()
        log_card_event("renew", card.id, f"卡密 {card.code} 续期：{'; '.join(detail_parts)}")
        return {
            "success": True,
            "revived": was_expired,
            "message": "；".join(detail_parts) + ("（历史任务与记录已在过期时清零，不恢复）" if was_expired else ""),
            "card": card_public_info(card),
        }
    finally:
        db.close()


@router.delete("/cards/{card_id}")
async def delete_card(card_id: int, _: bool = Depends(get_current_admin)):
    """删除卡密（级联删除其任务与记录，磁盘音频文件保留）

    注意：local_tasks（浏览器插件本地下载任务）未纳入 Card 的 ORM 级联关系，
    但表上有 card_id 外键约束；若不显式清理，删除带插件任务的卡密会因外键冲突返回 500。
    故此处先删除 local_tasks，再删除卡密本体。
    """
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        db.query(LocalTask).filter_by(card_id=card.id).delete(synchronize_session=False)
        db.delete(card)  # 级联删除 sessions / devices / tasks / records / accounts
        db.commit()
        return {"success": True, "message": "已删除"}
    finally:
        db.close()


# ════════════════════════════════════════
#  设备绑定管理（卡密席位 × 设备）
# ════════════════════════════════════════


class DeviceUnbindRequest(BaseModel):
    device_id: str
    client_type: str | None = None      # web/extension；空 = 两个席位都解绑


@router.get("/cards/{card_id}/devices")
async def list_card_devices(card_id: int, _: bool = Depends(get_current_admin)):
    """列出卡密的席位绑定详情（web/extension 各自的设备、活跃时间、IP）与在线会话数"""
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        devices = dvb.device_binding_status(card)
        active_sessions = (
            db.query(CardSession)
            .filter_by(card_id=card.id, is_active=True)
            .count()
        )
        return {
            "success": True,
            "card_id": card.id,
            "max_devices": dvb.get_max_devices(card),
            "binding_enabled": dvb.device_binding_enabled(),
            "active_sessions": active_sessions,
            "devices": devices,
        }
    finally:
        db.close()


@router.post("/cards/{card_id}/devices/unbind")
async def unbind_card_device(card_id: int, req: DeviceUnbindRequest, _: bool = Depends(get_current_admin)):
    """解绑单个设备：删除席位绑定并失效其全部会话（该设备需重新登录，不占换绑语义）"""
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        ct = dvb.normalize_client_type(req.client_type) if req.client_type else None
        if req.client_type and not ct:
            raise HTTPException(status_code=400, detail="client_type 非法（web / extension）")
        n = dvb.unbind_device(db, card, req.device_id.strip(), ct)
        if not n:
            raise HTTPException(status_code=404, detail="该设备未绑定在此卡密")
        db.commit()
        return {"success": True, "message": "已解绑并下线该设备", "unbound": n}
    finally:
        db.close()


@router.post("/cards/{card_id}/devices/unbind-all")
async def unbind_all_card_devices(card_id: int, _: bool = Depends(get_current_admin)):
    """解绑全部设备并踢下线全部会话（用户换机/售后专用）"""
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        n = dvb.unbind_all(db, card)
        db.commit()
        return {"success": True, "message": f"已解绑 {n} 台设备并踢下线全部会话", "unbound": n}
    finally:
        db.close()


@router.post("/cards/{card_id}/kick")
async def kick_card_sessions(card_id: int, _: bool = Depends(get_current_admin)):
    """仅踢下线（保留设备绑定）：用户在本机重新登录即可恢复"""
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        n = dvb.kick_all_sessions(db, card)
        db.commit()
        return {"success": True, "message": f"已踢下线 {n} 个会话（设备绑定保留）", "kicked": n}
    finally:
        db.close()


# ════════════════════════════════════════
#  接口与密钥配置 CRUD（替代 config.json）
# ════════════════════════════════════════
@router.get("/config")
async def get_config(_: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        rows = db.query(ApiConfig).all()
        cfg = {r.cfg_key: r.cfg_value for r in rows}
        # 当前生效的后台地址（供前端展示；修改后需重启生效）
        cfg["admin_path"] = _config.get_admin_path()
        return {"success": True, "config": cfg}
    finally:
        db.close()


@router.put("/config")
async def update_config(req: ConfigUpdateRequest, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        for key, value in req.config.items():
            if value is None:
                continue
            if key == "admin_path":
                p = _config.validate_admin_path(str(value))
                if not p:
                    raise HTTPException(
                        status_code=400,
                        detail="admin_path 非法：需以小写字母开头，仅含小写字母/数字/-/_，2~32 位，且不能与业务路由重名",
                    )
                value = p
            row = db.query(ApiConfig).filter_by(cfg_key=key).first()
            str_val = "1" if value is True else ("0" if value is False else str(value))
            if row:
                row.cfg_value = str_val
            else:
                db.add(ApiConfig(cfg_key=key, cfg_value=str_val, category="settings"))
        db.commit()
        rows = db.query(ApiConfig).all()
        cfg = {r.cfg_key: r.cfg_value for r in rows}
        cfg["admin_path"] = _config.get_admin_path()
        # 设备绑定/风控配置走 5s 短缓存：保存后立即失效，开关即时生效
        try:
            dvb.invalidate_cfg_cache()
        except Exception:
            pass
        return {"success": True, "config": cfg}
    finally:
        db.close()


# ════════════════════════════════════════
#  喜马拉雅账号管理（含扫码登录）已迁移至 api/accounts.py（卡密端自行登录）
# ════════════════════════════════════════


# ── 辅助：按 id（纯数字）或卡密号解析 Card ──
def _resolve_card(db, ident: str):
    if str(ident).isdigit():
        return db.query(Card).filter_by(id=int(ident)).first()
    return db.query(Card).filter_by(code=str(ident)).first()


# ── 后端供体账号扫码轮询（成功写入供体池，绝不直接用于下载） ──
def _handle_backend_poll(qr_id: str) -> dict:
    try:
        result = login_module.check_scan_status(qr_id)
        ret = result.get("ret")
        if ret == 0 and result.get("uid"):
            cookies = login_module.extract_cookies(qr_id)
            cookie_str = login_module.cookies_to_string(cookies)
            if not cookie_str:
                return {"success": False, "error": "登录态提取失败，请刷新二维码重新扫码"}
            user = login_module.verify_login(cookies)
            nickname = user.get("nickname", "") if user else ""
            uid = str(result.get("uid", ""))
            mobile = result.get("mobileMask", "")
            is_vip = user.get("isVip", False) if user else False
            info = _am.add_backend_account(
                nickname=nickname, uid=uid, cookie_str=cookie_str,
                mobile=mobile, is_vip=is_vip,
            )
            login_module.cleanup_qr(qr_id)
            return {"success": True, "id": info.get("id"), "uid": uid,
                    "nickname": nickname, "is_vip": is_vip}
        if ret == 2:
            return {"status": "scanned", "message": "已扫码，请确认"}
        return {"status": "waiting", "message": "等待扫码"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ════════════════════════════════════════
#  后端喜马拉雅供体账号池（仅供注入，与下载链路隔离）
# ════════════════════════════════════════
@router.post("/xm-login/qr")
async def backend_xm_qr(_: bool = Depends(get_current_admin)):
    """生成后端喜马拉雅登录二维码（登录成功后写入供体池）"""
    try:
        data = login_module.generate_qrcode()
        return {"success": True, "qr_id": data.get("qrId"), "img": data.get("img")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/xm-login/qr/check")
async def backend_xm_qr_check(qr_id: str = Query(...), _: bool = Depends(get_current_admin)):
    """轮询后端供体账号扫码状态（低频 HTTP 轮询）"""
    return _handle_backend_poll(qr_id)


@router.get("/xm-login/accounts")
async def list_backend_xm_accounts(_: bool = Depends(get_current_admin)):
    """列出后端供体池账号（脱敏）"""
    return {"success": True, "accounts": _am.list_backend_accounts()}


@router.delete("/xm-login/accounts/{account_id}")
async def delete_backend_xm_account(account_id: int, _: bool = Depends(get_current_admin)):
    """从供体池删除一个后端账号"""
    if _am.remove_backend_account(account_id):
        return {"success": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail="账号不存在")


# ════════════════════════════════════════
#  把供体账号注入 / 撤销到指定卡密
# ════════════════════════════════════════
@router.post("/cards/{card_ident}/inject-cookie")
async def inject_cookie_to_card(card_ident: str, req: CardInjectRequest, _: bool = Depends(get_current_admin)):
    """把后端供体账号复制进指定卡密（免前端扫码即可下官方音频）。

    可传 backend_ids 指定注入的供体账号；不传则注入供体池全部账号。
    """
    db = SessionLocal()
    try:
        card = _resolve_card(db, card_ident)
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        if req.backend_ids:
            result = {"details": []}
            for bid in req.backend_ids:
                b = _am.get_backend_account(bid)
                if not b:
                    result["details"].append({"backend_id": bid, "error": "供体账号不存在"})
                    continue
                result["details"].append(
                    {"backend_id": bid, **_am.inject_backend_cookie_to_card(card.id, b)}
                )
        else:
            result = _am.inject_all_backend_cookies(card.id)
        return {"success": True, "card_id": card.id, "result": result}
    finally:
        db.close()


@router.post("/cards/{card_ident}/revoke-cookie")
async def revoke_cookie_from_card(card_ident: str, _: bool = Depends(get_current_admin)):
    """撤销指定卡密下所有由供体池注入的账号"""
    db = SessionLocal()
    try:
        card = _resolve_card(db, card_ident)
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        n = _am.revoke_injected(card.id)
        return {"success": True, "card_id": card.id, "revoked": n}
    finally:
        db.close()
