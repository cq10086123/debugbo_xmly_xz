"""多账号管理模块 — 基于 ximalaya_accounts 表（替代旧 accounts.json）

按 card_id 隔离：每个卡密用户扫码登录的账号只属于该卡密；卡密过期清零时
一并删除。所有函数均要求传入 card_id。

对外函数（均带 card_id）：
list_accounts / add_account / remove_account / get_account_cookie /
get_account_info / get_default_cookie / set_rate_limited /
is_in_cooldown / get_active_account_ids

后端供体池（与下载链路隔离，仅注入逻辑使用）：
list_backend_accounts / get_backend_account / add_backend_account /
remove_backend_account / inject_backend_cookie_to_card /
inject_all_backend_cookies / revoke_injected
"""

import time
from datetime import datetime, timezone
from typing import Optional

from db.session import SessionLocal
from db.models import XimalayaAccount, BackendXmAccount

COOLDOWN_SECONDS = 86400  # 24 小时


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: Optional[datetime]) -> float:
    """datetime → Unix 时间戳。SQLite 读回的是 naive datetime，而写入时是 UTC aware；
    naive 直接 .timestamp() 会按服务器本地时区解析，导致冷却期偏移（UTC+8 下差 8 小时）。
    统一按 UTC 补齐再换算。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _resolve(db, card_id: int, account_id: str) -> Optional[XimalayaAccount]:
    """按 card_id + (acc_id 或 uid) 定位账号"""
    acc = db.query(XimalayaAccount).filter_by(card_id=card_id, acc_id=account_id).first()
    if acc is None:
        uid = str(account_id).replace("acc_", "")
        acc = db.query(XimalayaAccount).filter_by(card_id=card_id, uid=uid).first()
    return acc


def list_accounts(card_id: int) -> list[dict]:
    """列出某卡密下已保存的账号（脱敏，不含完整 cookie）"""
    db = SessionLocal()
    try:
        accounts = db.query(XimalayaAccount).filter_by(card_id=card_id).all()
        result = []
        for acc in accounts:
            result.append({
                "id": acc.acc_id or f"acc_{acc.uid}",
                "nickname": acc.nickname or "",
                "uid": acc.uid or "",
                "mobile": acc.mobile or "",
                "is_vip": bool(acc.is_vip),
                "cooling": acc.rate_limited_until is not None
                and _ts(acc.rate_limited_until) > time.time(),
                "added_at": _fmt(acc.added_at),
            })
        return result
    finally:
        db.close()


def add_account(card_id: int, nickname: str, uid: str, cookie_str: str,
                mobile: str = "", is_vip: bool = False) -> dict:
    """为某卡密添加账号（同卡下同 uid 不重复添加，更新 cookie）。"""
    acc_id = f"acc_{uid}"
    db = SessionLocal()
    try:
        acc = db.query(XimalayaAccount).filter_by(card_id=card_id, uid=str(uid)).first()
        if acc is None:
            acc = db.query(XimalayaAccount).filter_by(card_id=card_id, acc_id=acc_id).first()
        if acc is not None:
            acc.cookie_str = cookie_str
            acc.nickname = nickname or acc.nickname
            acc.mobile = mobile or acc.mobile
            acc.is_vip = is_vip
            db.commit()
            return {"id": acc.acc_id or acc_id, "updated": True}

        new_acc = XimalayaAccount(
            card_id=card_id,
            acc_id=acc_id,
            nickname=nickname,
            uid=str(uid),
            mobile=mobile,
            cookie_str=cookie_str,
            is_vip=is_vip,
            added_at=_now(),
        )
        db.add(new_acc)
        db.commit()
        return {"id": acc_id, "updated": False}
    finally:
        db.close()


def remove_account(card_id: int, account_id: str) -> bool:
    """删除某卡密下的指定账号。"""
    db = SessionLocal()
    try:
        acc = _resolve(db, card_id, account_id)
        if acc is None:
            return False
        db.delete(acc)
        db.commit()
        return True
    finally:
        db.close()


def get_account_cookie(card_id: int, account_id: str) -> Optional[str]:
    """获取某卡密下指定账号的 cookie 字符串"""
    db = SessionLocal()
    try:
        acc = _resolve(db, card_id, account_id)
        return acc.cookie_str if acc else None
    finally:
        db.close()


def get_account_info(card_id: int, account_id: str) -> Optional[dict]:
    """获取某卡密下指定账号的完整信息"""
    db = SessionLocal()
    try:
        acc = _resolve(db, card_id, account_id)
        if not acc:
            return None
        return {
            "id": acc.acc_id or f"acc_{acc.uid}",
            "nickname": acc.nickname,
            "uid": acc.uid,
            "mobile": acc.mobile,
            "is_vip": bool(acc.is_vip),
            "cookie_str": acc.cookie_str,
            "added_at": _fmt(acc.added_at),
            "rate_limited_until": acc.rate_limited_until,
        }
    finally:
        db.close()


def get_default_cookie(card_id: int) -> Optional[str]:
    """获取某卡密下可用的 cookie（取第一个账号）。无账号返回 None。"""
    db = SessionLocal()
    try:
        acc = db.query(XimalayaAccount).filter_by(card_id=card_id).order_by(XimalayaAccount.id).first()
        if acc and acc.cookie_str:
            return acc.cookie_str
    finally:
        db.close()
    return None


# ========== 账号限流冷却 ==========

def set_rate_limited(card_id: int, account_id: str) -> None:
    """标记某卡密下账号被限流，进入冷却期（24小时）。

    冷却按 uid 全局生效：同一供体 VIP 账号可能被复制进多张卡（同 uid、不同
    card_id），任一张卡触发限流后，所有同名 uid 的副本一并进入冷却，避免共享
    VIP 账号被多卡并发拖垮/封禁（审查报告 2026-08-17 中危 D）。
    """
    db = SessionLocal()
    try:
        acc = _resolve(db, card_id, account_id)
        if acc is None:
            return
        until = datetime.fromtimestamp(time.time() + COOLDOWN_SECONDS, timezone.utc)
        if acc.uid:
            # 全局冷却：同名 uid 的所有副本（跨卡）一起进入冷却
            db.query(XimalayaAccount).filter_by(uid=acc.uid).update(
                {XimalayaAccount.rate_limited_until: until}
            )
        else:
            acc.rate_limited_until = until
        db.commit()
    finally:
        db.close()


def is_in_cooldown(card_id: int, account_id: str) -> bool:
    """检查某卡密下账号是否在冷却期"""
    db = SessionLocal()
    try:
        acc = _resolve(db, card_id, account_id)
        if acc is None:
            return False
        until = acc.rate_limited_until
        if until is None:
            return False
        return _ts(until) > time.time()
    finally:
        db.close()


def get_active_account_ids(card_id: int) -> list[str]:
    """获取某卡密下不在冷却期的账号ID列表（自动清理过期冷却）。"""
    db = SessionLocal()
    try:
        accounts = db.query(XimalayaAccount).filter_by(card_id=card_id).all()
        now = time.time()
        active = []
        needs_save = False
        for acc in accounts:
            until = acc.rate_limited_until
            if until is not None:
                if now >= _ts(until):
                    acc.rate_limited_until = None
                    needs_save = True
                elif _ts(until) > now:
                    continue
            active.append(acc.acc_id or f"acc_{acc.uid}")
        if needs_save:
            db.commit()
        return active
    finally:
        db.close()


# ========== 后端供体账号池（仅供注入逻辑读取，与下载链路隔离） ==========

def list_backend_accounts() -> list[dict]:
    """列出后端供体池中的喜马拉雅账号（脱敏，不含完整 cookie）"""
    db = SessionLocal()
    try:
        rows = db.query(BackendXmAccount).order_by(BackendXmAccount.id).all()
        return [{
            "id": b.id,
            "nickname": b.nickname or "",
            "uid": b.uid or "",
            "mobile": b.mobile or "",
            "is_vip": bool(b.is_vip),
            "added_at": _fmt(b.added_at),
            "updated_at": _fmt(b.updated_at),
        } for b in rows]
    finally:
        db.close()


def get_backend_account(backend_id: int) -> Optional[dict]:
    """按 id 取供体账号完整信息（含 cookie，仅供注入逻辑内部使用）"""
    db = SessionLocal()
    try:
        b = db.query(BackendXmAccount).filter_by(id=backend_id).first()
        if not b:
            return None
        return {
            "id": b.id, "uid": b.uid, "nickname": b.nickname,
            "mobile": b.mobile, "cookie_str": b.cookie_str, "is_vip": b.is_vip,
        }
    finally:
        db.close()


def add_backend_account(nickname: str, uid: str, cookie_str: str,
                        mobile: str = "", is_vip: bool = False) -> dict:
    """向供体池新增/更新一个后端账号（同 uid 不重复，更新 cookie）"""
    db = SessionLocal()
    try:
        b = db.query(BackendXmAccount).filter_by(uid=str(uid)).first()
        if b is not None:
            b.cookie_str = cookie_str
            b.nickname = nickname or b.nickname
            b.mobile = mobile or b.mobile
            b.is_vip = is_vip
            db.commit()
            return {"id": b.id, "updated": True}
        new_b = BackendXmAccount(
            uid=str(uid), nickname=nickname, mobile=mobile,
            cookie_str=cookie_str, is_vip=is_vip, added_at=_now(), updated_at=_now(),
        )
        db.add(new_b)
        db.commit()
        return {"id": new_b.id, "updated": False}
    finally:
        db.close()


def remove_backend_account(backend_id: int) -> bool:
    """从供体池删除一个后端账号"""
    db = SessionLocal()
    try:
        b = db.query(BackendXmAccount).filter_by(id=backend_id).first()
        if not b:
            return False
        db.delete(b)
        db.commit()
        return True
    finally:
        db.close()


def inject_backend_cookie_to_card(card_id: int, backend: dict) -> dict:
    """把单个供体账号的 cookie 复制成某卡密名下的 XimalayaAccount 行。

    同一卡密下该 uid 已存在（无论用户自扫还是已注入）则跳过，避免覆盖用户账号或重复。
    """
    uid = str(backend.get("uid") or "")
    db = SessionLocal()
    try:
        existing = db.query(XimalayaAccount).filter_by(card_id=card_id, uid=uid).first()
        if existing:
            return {"uid": uid, "skipped": True, "reason": "already_exists"}
        new_acc = XimalayaAccount(
            card_id=card_id,
            acc_id=f"acc_{uid}",
            nickname=backend.get("nickname"),
            uid=uid,
            mobile=backend.get("mobile"),
            cookie_str=backend.get("cookie_str"),
            is_vip=bool(backend.get("is_vip", False)),
            injected=True,
            backend_src_id=backend.get("id"),
            added_at=_now(),
        )
        db.add(new_acc)
        db.commit()
        return {"uid": uid, "injected": True}
    finally:
        db.close()


def inject_all_backend_cookies(card_id: int) -> dict:
    """把供体池全部账号复制到指定卡密。返回注入/跳过统计。"""
    db = SessionLocal()
    try:
        backends = db.query(BackendXmAccount).order_by(BackendXmAccount.id).all()
    finally:
        db.close()
    results = [inject_backend_cookie_to_card(card_id, {
        "id": b.id, "uid": b.uid, "nickname": b.nickname,
        "mobile": b.mobile, "cookie_str": b.cookie_str, "is_vip": b.is_vip,
    }) for b in backends]
    injected = sum(1 for r in results if r.get("injected"))
    skipped = sum(1 for r in results if r.get("skipped"))
    return {"total": len(results), "injected": injected, "skipped": skipped}


def revoke_injected(card_id: int) -> int:
    """撤销某卡密下所有由供体池注入的账号，返回删除行数。"""
    db = SessionLocal()
    try:
        n = db.query(XimalayaAccount).filter_by(card_id=card_id, injected=True).delete(
            synchronize_session=False
        )
        db.commit()
        return n
    finally:
        db.close()

