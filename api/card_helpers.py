"""卡密策略与公共信息辅助函数（auth / admin / 后台清理任务共用）"""

import json
from datetime import datetime, timedelta, timezone

from db.models import Card
from db.init_db import log_card_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的 datetime 为 naive，统一按 UTC 补齐时区后再比较"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_expired(card: Card) -> bool:
    """判断卡密当前是否已过期（按 expiry_type 计算，不修改数据库）"""
    if card.status == "expired":
        return True
    if card.expiry_type == "fixed":
        if card.expires_at is None:
            return False
        return _aware(card.expires_at) <= _now()
    if card.expiry_type == "days":
        if card.activated_at is None or not card.valid_days:
            # 未激活的「days」卡不视为过期（激活后才开始计时）
            return False
        return _aware(card.activated_at) + timedelta(days=card.valid_days) <= _now()
    return False


def mark_expired(db, card: Card) -> None:
    """将卡密标记为 expired，清除其全部会话，并记录日志（落库）"""
    if card.status != "expired":
        card.status = "expired"
        log_card_event("expire", card.id, "卡密过期，数据清零")
    # 清除该卡密所有登录会话
    for s in card.sessions:
        s.is_active = False
    db.commit()


def card_bound_interfaces(card: Card) -> list[str] | None:
    """解析卡密绑定的接口名列表。

    返回 None 表示不限制（可用全部接口）；返回列表表示仅可使用列表内接口。
    """
    raw = getattr(card, "bound_interfaces", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    names = [str(x) for x in data if x]
    return names or None


def dump_bound_interfaces(names: list[str] | None) -> str | None:
    """绑定列表 → 数据库存储字符串（空列表按不限制处理，存 NULL）"""
    if not names:
        return None
    return json.dumps(list(dict.fromkeys(names)), ensure_ascii=False)


def card_public_info(card: Card) -> dict:
    """组装返回给前端的卡密信息（含剩余时间/天数）"""
    now = _now()
    remaining_seconds = None
    if card.expiry_type == "fixed" and card.expires_at is not None:
        remaining_seconds = max(0, int((_aware(card.expires_at) - now).total_seconds()))
    elif card.expiry_type == "days" and card.activated_at is not None and card.valid_days:
        deadline = _aware(card.activated_at) + timedelta(days=card.valid_days)
        remaining_seconds = max(0, int((deadline - now).total_seconds()))

    if remaining_seconds is not None:
        days = remaining_seconds // 86400
        hours = (remaining_seconds % 86400) // 3600
        remaining_text = f"{days}天{hours}小时" if days else f"{hours}小时"
    else:
        remaining_text = ""

    return {
        "id": card.id,
        "code": card.code,
        "status": card.status,
        "expiry_type": card.expiry_type,
        "expires_at": card.expires_at.isoformat() if card.expires_at else None,
        "valid_days": card.valid_days,
        "activated_at": card.activated_at.isoformat() if card.activated_at else None,
        "remaining_seconds": remaining_seconds,
        "remaining_text": remaining_text,
        "note": card.note,
        "bound_interfaces": card_bound_interfaces(card),
        "download_mode": getattr(card, "download_mode", None) or "both",
        "max_devices": getattr(card, "max_devices", None) or 1,
        "quark_sync": bool(getattr(card, "quark_sync", False)),
        "last_login_at": card.last_login_at.isoformat() if card.last_login_at else None,
    }
