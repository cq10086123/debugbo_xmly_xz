"""鉴权依赖：业务卡密 token 与管理员 token 完全分离"""

from datetime import datetime, timezone

from fastapi import Header, HTTPException, Query

from db.session import SessionLocal
from db.models import Session as CardSession, AdminToken
from api.card_helpers import is_expired, mark_expired, card_bound_interfaces


async def _auth_card(raw_token: str) -> dict:
    """用原始 token 字符串校验卡密登录态，返回 {card_id, code, token, bound}。

    bound 为该卡密绑定的接口名列表；None 表示不限制（可用全部接口）。
    """
    db = SessionLocal()
    try:
        sess = db.query(CardSession).filter_by(token=raw_token, is_active=True).first()
        if not sess:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

        card = sess.card
        if card.status == "disabled":
            raise HTTPException(status_code=403, detail="卡密已被禁用")

        if is_expired(card):
            mark_expired(db, card)
            raise HTTPException(status_code=401, detail="卡密已过期")

        # 活跃时间每 60 秒最多写一次：原实现每请求一次 UPDATE+commit，高并发下写放大严重
        now = datetime.now(timezone.utc)
        last = sess.last_active_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or (now - last).total_seconds() > 60:
            sess.last_active_at = now
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


async def get_current_card(authorization: str | None = Header(default=None)) -> dict:
    """校验卡密登录态（仅 Authorization 头），返回 {card_id, code}。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    return await _auth_card(token)


async def get_current_card_download(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict:
    """下载接口专用：优先取 Authorization 头，缺失时回退到 ?token= 查询参数。

    用于前端原生 <a> 下载（浏览器无法在普通导航里带自定义请求头）。
    """
    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:].strip()
    if not raw and token:
        raw = token
    if not raw:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    return await _auth_card(raw)


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
