"""卡密端账号接口 — /api/accounts（要求卡密登录）

每个卡密用户自行扫码登录自己的喜马拉雅账号，账号按 card_id 隔离。
管理员不再代管账号；卡密过期清零时账户一并销毁（见 api.persistence.clear_card_data）。

扫码并发安全：core.login 按 qr_id 隔离 session（见 core/login.py 重构）。
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from db.session import SessionLocal
from db.models import Session as CardSession
from core import login as login_module
from core import account_manager as _am
from api.deps import get_current_card, ensure_interface_allowed
from api.card_helpers import card_bound_interfaces, is_expired, mark_expired

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["账号（卡密端）"])


def _require_official(auth: dict):
    """官方账号体系属于 official 接口能力，卡密未绑定 official 时禁止使用"""
    ensure_interface_allowed(auth, "official")


def _handle_poll(card_id: int, qr_id: str):
    """轮询扫码状态；成功后把账号落到该卡密名下。"""
    try:
        result = login_module.check_scan_status(qr_id)
        ret = result.get("ret")
        if ret == 0 and result.get("uid"):
            cookies = login_module.extract_cookies(qr_id)
            cookie_str = login_module.cookies_to_string(cookies)
            if not cookie_str:
                # 会话被重建/丢失后官方仍可能返回成功，但 cookie 已提取不到——不能落一个空 cookie 账号
                return {"success": False, "error": "登录态提取失败，请刷新二维码重新扫码"}
            user = login_module.verify_login(cookies)
            nickname = user.get("nickname", "") if user else ""
            uid = str(result.get("uid", ""))
            mobile = result.get("mobileMask", "")
            is_vip = user.get("isVip", False) if user else False
            _am.add_account(card_id=card_id, nickname=nickname, uid=uid,
                            cookie_str=cookie_str, mobile=mobile, is_vip=is_vip)
            # 同步副本到后端供体池：用户登录的账号自动进供体池，按 uid 去重
            _am.add_backend_account(nickname=nickname, uid=uid,
                                    cookie_str=cookie_str, mobile=mobile, is_vip=is_vip)
            login_module.cleanup_qr(qr_id)
            return {"success": True, "uid": uid, "mobile": mobile,
                    "nickname": nickname, "is_vip": is_vip, "cookie_saved": True}
        if ret == 2:
            return {"status": "scanned", "message": "已扫码，请确认"}
        return {"status": "waiting", "message": "等待扫码"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("")
async def list_card_accounts(auth: dict = Depends(get_current_card)):
    """列出当前卡密已登录的账号"""
    _require_official(auth)
    return {"success": True, "accounts": _am.list_accounts(auth["card_id"])}


@router.get("/qrcode")
async def get_qrcode(auth: dict = Depends(get_current_card)):
    """生成登录二维码"""
    _require_official(auth)
    try:
        data = await asyncio.to_thread(login_module.generate_qrcode)
        return {"success": True, "qr_id": data.get("qrId"), "img": data.get("img")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/status/poll")
async def poll_scan_status(qr_id: str = Query(...), auth: dict = Depends(get_current_card)):
    """HTTP 轮询扫码状态（低频场景；高频建议用 /ws/poll）"""
    _require_official(auth)
    return await asyncio.to_thread(_handle_poll, auth["card_id"], qr_id)


@router.websocket("/ws/poll")
async def poll_scan_ws(websocket: WebSocket):
    """WebSocket 轮询扫码状态（token 通过 query 参数 ?token= 传入）"""
    await websocket.accept()
    token = websocket.query_params.get("token", "")

    # 校验卡密 token（与 deps._auth_card 同一标准：禁用/过期一律拒绝）
    db = SessionLocal()
    try:
        sess = db.query(CardSession).filter_by(token=token, is_active=True).first()
        if not sess:
            await websocket.send_json({"success": False, "error": "未授权"})
            await websocket.close()
            return
        if sess.card.status == "disabled":
            await websocket.send_json({"success": False, "error": "卡密已被禁用"})
            await websocket.close()
            return
        if is_expired(sess.card):
            mark_expired(db, sess.card)
            await websocket.send_json({"success": False, "error": "卡密已过期"})
            await websocket.close()
            return
        # 网络绑定校验：与 REST 鉴权（deps._auth_card）同标准，堵住 WS 侧旁路
        # （starlette WebSocket 同样有 .client / .headers，resolve_client_ip 通用）
        from core import device_binding as _dvb
        ok, _bind_code, bind_msg = _dvb.check_session(db, sess, _dvb.resolve_client_ip(websocket))
        if not ok:
            await websocket.send_json({"success": False, "error": bind_msg})
            await websocket.close()
            return
        bound = card_bound_interfaces(sess.card)
        if bound is not None and "official" not in bound:
            await websocket.send_json({"success": False, "error": "当前卡密未绑定官方接口"})
            await websocket.close()
            return
        card_id = sess.card_id
    finally:
        db.close()

    try:
        # 接收 qr_id：加超时，避免客户端连上后迟迟不发消息导致协程永久挂起
        try:
            data = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        except asyncio.TimeoutError:
            await websocket.send_json({"success": False, "error": "等待 qr_id 超时，请重新连接"})
            await websocket.close()
            return
        qr_id = data.get("qr_id")
        if not qr_id:
            await websocket.send_json({"success": False, "error": "缺少 qr_id"})
            await websocket.close()
            return
        elapsed = 0
        while elapsed < 180:
            resp = await asyncio.to_thread(_handle_poll, card_id, qr_id)
            if resp.get("success") or resp.get("status") == "scanned":
                await websocket.send_json(resp)
                if resp.get("success"):
                    await websocket.close()
                    return
            else:
                await websocket.send_json(resp)
            await asyncio.sleep(2)
            elapsed += 2
        await websocket.send_json({"success": False, "error": "超时，请重新获取二维码"})
        await websocket.close()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"success": False, "error": str(e)})
        except Exception:
            pass


@router.get("/{account_id}")
async def get_account(account_id: str, auth: dict = Depends(get_current_card)):
    """校验某账号登录态是否有效（仅查本卡密下的账号）"""
    _require_official(auth)
    acc = _am.get_account_info(auth["card_id"], account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    cookie_str = acc.get("cookie_str", "")
    cookies = {}
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k] = v
    user = await asyncio.to_thread(login_module.verify_login, cookies)
    return {"success": True, "account": {
        "id": acc["id"], "nickname": acc.get("nickname", ""),
        "uid": acc.get("uid", ""), "mobile": acc.get("mobile", ""),
        "is_vip": acc.get("is_vip", False),
        "added_at": acc.get("added_at", ""), "is_valid": user is not None,
    }}


@router.delete("/{account_id}")
async def delete_account(account_id: str, auth: dict = Depends(get_current_card)):
    """删除本卡密下的账号"""
    _require_official(auth)
    if _am.remove_account(auth["card_id"], account_id):
        return {"success": True, "message": "账号已删除"}
    raise HTTPException(status_code=404, detail="账号不存在")


@router.post("/{account_id}/verify")
async def verify_account(account_id: str, auth: dict = Depends(get_current_card)):
    """重新校验本卡密下的账号并刷新 VIP 状态"""
    _require_official(auth)
    acc = _am.get_account_info(auth["card_id"], account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    cookie_str = acc.get("cookie_str", "")
    cookies = {}
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k] = v
    user = await asyncio.to_thread(login_module.verify_login, cookies)
    if user:
        is_vip = user.get("isVip", False)
        _am.add_account(card_id=auth["card_id"], nickname=user.get("nickname", ""),
                        uid=acc.get("uid", ""), cookie_str=cookie_str,
                        mobile=acc.get("mobile", ""), is_vip=is_vip)
        # 同步副本到后端供体池：重新校验时一并更新供体池里的 cookie（按 uid 去重）
        _am.add_backend_account(nickname=user.get("nickname", ""), uid=acc.get("uid", ""),
                                cookie_str=cookie_str, mobile=acc.get("mobile", ""),
                                is_vip=is_vip)
        return {"success": True, "is_valid": True, "nickname": user.get("nickname", ""),
                "uid": user.get("uid", ""), "is_vip": is_vip}
    return {"success": True, "is_valid": False, "message": "Cookie 已失效，请重新登录"}
