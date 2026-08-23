"""鉴权依赖：业务卡密 token 与管理员 token 完全分离"""

import logging
from datetime import datetime, timezone

from fastapi import Header, HTTPException, Query, Request

from db.session import SessionLocal
from db.models import Session as CardSession, AdminToken
from api.card_helpers import is_expired, mark_expired, card_bound_interfaces

logger = logging.getLogger(__name__)


async def _auth_card(raw_token: str, device_id: str | None = None, client_ip: str | None = None) -> dict:
    """用原始 token 字符串校验卡密登录态，返回 {card_id, code, token, bound}。

    bound 为该卡密绑定的接口名列表；None 表示不限制（可用全部接口）。
    device_id：请求头 X-Device-Id（设备绑定开启时校验，见 core/device_binding.py）。
    """
    db = SessionLocal()
    try:
        sess = db.query(CardSession).filter_by(token=raw_token, is_active=True).first()
        if not sess:
            # 设备绑定语义优化：token 已失效时回查归属，被顶号/解绑的设备给出明确提示
            # （顶号时旧 token 被直接置 inactive，走不到下方设备校验分支）
            from core import device_binding as _dvb0
            try:
                request_identity = _dvb0.login_identity(device_id, client_ip)
                old = db.query(CardSession).filter_by(token=raw_token).first() if request_identity else None
                if (old is not None and not old.is_active and old.device_id
                        and old.device_id == request_identity and old.client_type
                        and _dvb0.device_binding_enabled()
                        and old.device_id not in _dvb0.bound_device_ids(db, old.card_id, old.client_type)):
                    if _dvb0.binding_scope() == _dvb0.SCOPE_IP:
                        raise HTTPException(status_code=401, detail="账号已在其他网络登录，本设备已下线")
                    raise HTTPException(status_code=401, detail="账号已在其他设备登录，本设备已下线")
            except HTTPException:
                raise
            except Exception:
                pass
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

        card = sess.card
        if card.status == "disabled":
            raise HTTPException(status_code=403, detail="卡密已被禁用")

        if is_expired(card):
            mark_expired(db, card)
            raise HTTPException(status_code=401, detail="卡密已过期")

        # ── 设备绑定校验（总开关关闭时短路；历史会话 device_id 为空放行）──
        # 唯一接入点：所有业务路由（搜索/下载/重试/插件等）经此依赖自动受控，
        # 业务文件零改动。校验失败返回带语义的 401，客户端据此提示"已被顶下线"。
        from core import device_binding as _dvb
        ok, _code, message = _dvb.check_session(db, sess, device_id, client_ip)
        if not ok:
            raise HTTPException(status_code=401, detail=message)

        # ── 风控加固（fail-open，不影响正常请求）──
        try:
            from api import risk_control as _risk
            if _risk.record_token_ip(raw_token, client_ip):
                sess.is_active = False
                db.commit()
                _risk.log_kick_event(raw_token[-8:], card.id, _risk.token_ips(raw_token))
                _risk.forget_token(raw_token)
                raise HTTPException(status_code=401, detail="检测到账号在多个网络环境同时使用，已强制下线，请重新登录")
            _risk.check_seat_ip_mismatch(
                card.id, sess.client_type, sess.ip or client_ip,
                _risk.get_other_seat_ip(card.id, sess.client_type, db),
            )
        except HTTPException:
            raise
        except Exception:
            logger.debug("风控检查异常（已放行）", exc_info=True)

        # 活跃时间每 60 秒最多写一次：原实现每请求一次 UPDATE+commit，高并发下写放大严重
        now = datetime.now(timezone.utc)
        last = sess.last_active_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or (now - last).total_seconds() > 60:
            sess.last_active_at = now
            _dvb.touch_binding(db, card.id, sess.client_type, sess.device_id, client_ip)
            db.commit()
        return {
            "card_id": card.id,
            "code": card.code,
            "token": raw_token,
            "bound": card_bound_interfaces(card),
            "download_mode": getattr(card, "download_mode", None) or "both",
        }
    finally:
        db.close()


def ensure_interface_allowed(auth: dict, name: str) -> None:
    """校验当前卡密是否允许使用指定接口；未绑定（bound=None）则放行。"""
    bound = auth.get("bound")
    if bound is not None and name not in bound:
        raise HTTPException(
            status_code=403,
            detail=f"当前卡密未绑定接口「{name}」，仅可使用: {', '.join(bound)}",
        )


def ensure_download_mode_allowed(auth: dict, mode: str) -> None:
    """校验当前卡密是否允许指定下载模式（server / local）。

    download_mode=both（或缺失/非法值）则放行；否则仅允许与设定一致的模式。
    历史卡与未设置卡 download_mode 为 NULL → 按 both 处理，向后兼容。
    """
    allowed = (auth.get("download_mode") or "both")
    if allowed not in ("both", "server", "local"):
        # 非法值视作 both（与缺省一致），直接放行
        return
    if allowed == "both":
        return
    if mode != allowed:
        label_allowed = "服务器" if allowed == "server" else "本地"
        label_req = "本地" if mode == "local" else "服务器"
        raise HTTPException(
            status_code=403,
            detail=f"当前卡密仅允许「{label_allowed}」下载，不可使用「{label_req}」下载",
        )


async def get_current_card(
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    request: Request = None,
) -> dict:
    """校验卡密登录态（Authorization 头 + X-Device-Id 头），返回 {card_id, code}。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    from core.device_binding import resolve_client_ip
    client_ip = resolve_client_ip(request)
    return await _auth_card(token, (x_device_id or "").strip() or None, client_ip)


async def get_current_card_download(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    device: str | None = Query(default=None, alias="device"),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    request: Request = None,
) -> dict:
    """下载接口专用：优先取 Authorization 头，缺失时回退到 ?token= 查询参数。

    用于前端原生 <a> 下载（浏览器无法在普通导航里带自定义请求头），
    设备 ID 同理回退到 ?device= 查询参数。
    """
    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:].strip()
    if not raw and token:
        raw = token
    if not raw:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    dev = (x_device_id or device or "").strip() or None
    from core.device_binding import resolve_client_ip
    client_ip = resolve_client_ip(request)
    return await _auth_card(raw, dev, client_ip)


async def get_current_admin(authorization: str | None = Header(default=None)) -> bool:
    """校验管理员 token（与业务 token 完全隔离）"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")

    db = SessionLocal()
    try:
        at = db.query(AdminToken).filter_by(token=token).first()
        if not at:
            raise HTTPException(status_code=401, detail="管理员登录已失效")
        return True
    finally:
        db.close()
