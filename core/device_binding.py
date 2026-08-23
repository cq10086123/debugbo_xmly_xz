"""设备绑定模块 — 卡密席位×设备的绑定、顶号换绑、会话设备校验

设计原则（见《设备绑定需求方案.md》D2/D5）：
- 席位模型：每张卡密有 web / extension 两类席位，各允许 max_devices 台设备；
  同一台电脑的正常用户 = 网页占 web 席 + 插件占 extension 席，互不干扰。
- 顶号自动换绑（D1）：新设备登录且席位已满 → 淘汰 bound_at 最早的绑定，
  并失效该设备的全部会话（"谁最后登录谁用"）。
- 非侵入（D5）：本模块是设备绑定逻辑的唯一的家；api/deps.py 只在
  _auth_card 末尾调用一次 check_session，其余业务代码零感知。
- 总开关 api_config.device_binding_enabled（默认关闭）：关闭时登录不要求
  deviceId、鉴权跳过设备校验 → 行为与历史版本完全一致，等价于一键回退。

兼容策略：
- 存量 sessions 行 device_id 为 NULL（升级部署前已登录的用户）→ 校验放行，
  下次登录起才写入绑定（平滑过渡，不暴力踢出在线用户）。
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from db.session import SessionLocal
from db.models import Card, Session as CardSession, DeviceBinding, ApiConfig
from db.init_db import log_card_event

# ── 常量与默认值 ──
CLIENT_WEB = "web"
CLIENT_EXTENSION = "extension"
VALID_CLIENT_TYPES = (CLIENT_WEB, CLIENT_EXTENSION)

DEFAULT_MAX_DEVICES = 1
MAX_DEVICES_LIMIT = 10          # 管理端可设置的上限，防误填
DEFAULT_SESSION_IDLE_DAYS = 7   # 会话空闲过期天数（可配 session_idle_days）

# 客户端展示名（管理端/日志用）
CLIENT_LABELS = {CLIENT_WEB: "网页", CLIENT_EXTENSION: "插件"}


# ════════════════════════════════════════
#  配置读取（总开关与策略参数）
# ════════════════════════════════════════
_cfg_lock = threading.Lock()
_cfg_cache: dict = {"values": {}, "loaded_at": 0.0}
_CFG_TTL = 5.0  # 秒：开关类配置短缓存，避免每请求全表扫 api_config


def _load_cfg_cached() -> dict:
    """读取 api_config（5 秒缓存）。数据库异常时返回空 dict（安全回退默认值）。"""
    with _cfg_lock:
        now = time.time()
        if now - _cfg_cache["loaded_at"] < _CFG_TTL and _cfg_cache["values"]:
            return _cfg_cache["values"]
    try:
        db = SessionLocal()
        try:
            rows = db.query(ApiConfig).all()
            values = {r.cfg_key: r.cfg_value for r in rows}
        finally:
            db.close()
    except Exception:
        values = {}
    with _cfg_lock:
        _cfg_cache["values"] = values
        _cfg_cache["loaded_at"] = time.time()
    return values


def invalidate_cfg_cache() -> None:
    """管理端修改配置后调用，立即生效。"""
    with _cfg_lock:
        _cfg_cache["values"] = {}
        _cfg_cache["loaded_at"] = 0.0


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def device_binding_enabled() -> bool:
    """总开关：api_config.device_binding_enabled，默认关闭（false）。"""
    return _truthy(_load_cfg_cached().get("device_binding_enabled", "0"))


def session_idle_days() -> int:
    """会话空闲过期天数，默认 7，非法/缺失回退默认。"""
    raw = _load_cfg_cached().get("session_idle_days")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_IDLE_DAYS
    return v if 1 <= v <= 365 else DEFAULT_SESSION_IDLE_DAYS


# ════════════════════════════════════════
#  纯函数（可单测，不碰数据库）
# ════════════════════════════════════════
def normalize_client_type(v: str | None) -> str | None:
    """归一化登录端类型；非法值返回 None。默认（未传）按 web 处理。"""
    s = (v or "").strip().lower()
    if not s:
        return CLIENT_WEB
    return s if s in VALID_CLIENT_TYPES else None


def get_max_devices(card) -> int:
    """读取卡密的每席位设备数上限（NULL/非法 → 1，钳制 1~10）。"""
    raw = getattr(card, "max_devices", None)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DEVICES
    return max(1, min(MAX_DEVICES_LIMIT, v))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def check_session_pure(
    sess_device_id: str | None,
    sess_client_type: str | None,
    sess_last_active: datetime | None,
    request_device_id: str | None,
    bound_device_ids: set[str],
    idle_days: int,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """会话设备校验（纯函数）。

    返回 (ok, code, message)：
    - ok=True                放行
    - code=device_mismatch   token 被拿到别的浏览器/环境使用
    - code=kicked            所属设备已被顶号换绑 / 解绑 / 踢下线
    - code=session_expired   会话空闲超时
    历史/免设备会话（sess_device_id 为空）一律放行（平滑过渡）。
    """
    if not sess_device_id:
        return True, "", ""  # 历史会话或开关关闭期登录的会话：放行

    now = now or datetime.now(timezone.utc)

    # 1) token 与设备必须匹配（防 token 拷贝到别的浏览器）
    if (request_device_id or "") != sess_device_id:
        return False, "device_mismatch", "登录环境校验失败，请在本机重新登录"

    # 2) 会话所属设备必须是当前席位绑定（防已被顶号/解绑的旧设备继续用）
    if sess_client_type and sess_device_id not in bound_device_ids:
        return False, "kicked", "账号已在其他设备登录，本设备已下线"

    # 3) 空闲过期
    last = _aware(sess_last_active)
    if last is not None and (now - last) > timedelta(days=idle_days):
        return False, "session_expired", "登录已过期（长期未使用），请重新登录"

    return True, "", ""


# ════════════════════════════════════════
#  绑定维护（登录 / 解绑 / 踢线，需要 db 会话）
# ════════════════════════════════════════
def bound_device_ids(db, card_id: int, client_type: str) -> set[str]:
    """某席位当前绑定的设备 ID 集合。"""
    rows = (
        db.query(DeviceBinding)
        .filter_by(card_id=card_id, client_type=client_type)
        .all()
    )
    return {r.device_id for r in rows}


# 绑定维护互斥锁：登录的「查席位 → 淘汰 → 插入」是非原子序列，并发登录同一卡密
# 会双写超额绑定（TOCTOU）。单进程自托管（uvicorn 单 worker，与 api/auth.py 的
# 验证码限流状态同一前提）下，进程内锁即可保证串行。登录是低频操作，全局锁足够。
_BIND_LOCK = threading.Lock()


def deactivate_device_sessions(db, card_id: int, client_type: str | None, device_id: str) -> int:
    """失效指定设备的全部活跃会话（顶号/解绑/踢线共用）。返回失效条数。"""
    q = db.query(CardSession).filter_by(
        card_id=card_id, device_id=device_id, is_active=True
    )
    if client_type:
        q = q.filter_by(client_type=client_type)
    rows = q.all()
    for s in rows:
        s.is_active = False
    return len(rows)


def bind_device_on_login(db, card, client_type: str, device_id: str, ip: str | None = None) -> dict:
    """登录时维护席位绑定（顶号自动换绑）。

    返回 {binding, evicted: [被踢设备ID], kicked_sessions: n}
    调用方负责 commit。
    """
    with _BIND_LOCK:
        return _bind_device_on_login_locked(db, card, client_type, device_id, ip)


def _bind_device_on_login_locked(db, card, client_type: str, device_id: str, ip: str | None = None) -> dict:
    max_devices = get_max_devices(card)
    existing = (
        db.query(DeviceBinding)
        .filter_by(card_id=card.id, client_type=client_type)
        .order_by(DeviceBinding.bound_at.asc())
        .all()
    )

    evicted: list[str] = []
    kicked_sessions = 0

    # 同设备已绑定：续活并更新 IP，无需换绑
    for row in existing:
        if row.device_id == device_id:
            row.last_active_at = datetime.now(timezone.utc)
            row.last_ip = ip or row.last_ip
            return {"binding": row, "evicted": [], "kicked_sessions": 0}

    # 席位已满：淘汰最早的绑定（顶号换绑）
    while len(existing) >= max_devices:
        oldest = existing.pop(0)
        evicted.append(oldest.device_id)
        kicked_sessions += deactivate_device_sessions(
            db, card.id, oldest.client_type, oldest.device_id
        )
        db.delete(oldest)

    binding = DeviceBinding(
        card_id=card.id,
        client_type=client_type,
        device_id=device_id,
        bound_at=datetime.now(timezone.utc),
        last_active_at=datetime.now(timezone.utc),
        last_ip=ip,
    )
    db.add(binding)

    if evicted:
        log_card_event(
            "device_kick", card.id,
            f"卡密 {card.code} [{CLIENT_LABELS.get(client_type, client_type)}席位] 顶号换绑："
            f"新设备 …{device_id[-8:]} 踢掉设备 …{', …'.join(e[-8:] for e in evicted)}（失效会话 {kicked_sessions} 条）"
        )
    else:
        log_card_event(
            "device_bind", card.id,
            f"卡密 {card.code} [{CLIENT_LABELS.get(client_type, client_type)}席位] 绑定设备 …{device_id[-8:]}"
            + (f"（IP {ip}）" if ip else "")
        )

    return {"binding": binding, "evicted": evicted, "kicked_sessions": kicked_sessions}


def touch_binding(db, card_id: int, client_type: str | None, device_id: str, ip: str | None = None) -> None:
    """鉴权通过后刷新绑定的活跃时间/IP（尽力而为，失败不影响请求）。"""
    if not device_id:
        return
    try:
        q = db.query(DeviceBinding).filter_by(card_id=card_id, device_id=device_id)
        if client_type:
            q = q.filter_by(client_type=client_type)
        row = q.first()
        if row:
            row.last_active_at = datetime.now(timezone.utc)
            if ip:
                row.last_ip = ip
    except Exception:
        pass


def trim_bindings_to_limit(db, card) -> int:
    """把各席位绑定数裁剪回 max_devices（淘汰 bound_at 最早的），并失效被裁设备的会话。

    用于管理员下调 max_devices 后即时收敛存量绑定。返回被裁掉的绑定数。
    """
    with _BIND_LOCK:
        max_devices = get_max_devices(card)
        trimmed = 0
        for ct in VALID_CLIENT_TYPES:
            rows = (
                db.query(DeviceBinding)
                .filter_by(card_id=card.id, client_type=ct)
                .order_by(DeviceBinding.bound_at.asc())
                .all()
            )
            excess = rows[: max(0, len(rows) - max_devices)]
            for row in excess:
                deactivate_device_sessions(db, card.id, row.client_type, row.device_id)
                db.delete(row)
                trimmed += 1
        if trimmed:
            log_card_event(
                "device_unbind", card.id,
                f"卡密 {card.code} 设备数上限下调为 {max_devices}，裁剪超额绑定 {trimmed} 台（即时失效）"
            )
        return trimmed


def unbind_device(db, card, device_id: str, client_type: str | None = None) -> int:
    """解绑设备（删除绑定 + 失效其会话）。client_type 为空表示两个席位都查。返回解绑数。"""
    q = db.query(DeviceBinding).filter_by(card_id=card.id, device_id=device_id)
    if client_type:
        q = q.filter_by(client_type=client_type)
    rows = q.all()
    n = 0
    for row in rows:
        deactivate_device_sessions(db, card.id, row.client_type, row.device_id)
        db.delete(row)
        n += 1
    if n:
        label = CLIENT_LABELS.get(client_type, "全部") if client_type else "全部"
        log_card_event(
            "device_unbind", card.id,
            f"卡密 {card.code} 管理员解绑设备 …{device_id[-8:]}（{label}席位，含会话清理）"
        )
    return n


def unbind_all(db, card) -> int:
    """解绑该卡密全部设备并踢下线全部会话。返回解绑设备数。"""
    rows = db.query(DeviceBinding).filter_by(card_id=card.id).all()
    for row in rows:
        deactivate_device_sessions(db, card.id, row.client_type, row.device_id)
        db.delete(row)
    # 兜底：清掉所有活跃会话（含历史无设备会话）
    for s in db.query(CardSession).filter_by(card_id=card.id, is_active=True).all():
        s.is_active = False
    if rows:
        log_card_event("device_unbind", card.id, f"卡密 {card.code} 管理员解绑全部设备（{len(rows)} 台）并踢下线")
    return len(rows)


def kick_all_sessions(db, card) -> int:
    """仅踢下线（保留绑定关系）：用户下次在本机重新登录即可，不占换绑语义。"""
    rows = db.query(CardSession).filter_by(card_id=card.id, is_active=True).all()
    for s in rows:
        s.is_active = False
    if rows:
        log_card_event("device_kick", card.id, f"卡密 {card.code} 管理员踢下线全部会话（{len(rows)} 条）")
    return len(rows)


# ════════════════════════════════════════
#  鉴权入口（api/deps.py 唯一调用点）
# ════════════════════════════════════════
def check_session(db, sess: CardSession, request_device_id: str | None) -> tuple[bool, str, str]:
    """总开关下的会话设备校验（开关关闭 → 直接放行）。"""
    if not device_binding_enabled():
        return True, "", ""
    bound = bound_device_ids(db, sess.card_id, sess.client_type) if sess.client_type else set()
    return check_session_pure(
        sess.device_id,
        sess.client_type,
        sess.last_active_at,
        request_device_id,
        bound,
        session_idle_days(),
    )


def device_binding_status(card) -> dict:
    """卡密的设备绑定概要（管理端展示用，含席位占用）。"""
    db = SessionLocal()
    try:
        out = {}
        for ct in VALID_CLIENT_TYPES:
            rows = (
                db.query(DeviceBinding)
                .filter_by(card_id=card.id, client_type=ct)
                .order_by(DeviceBinding.bound_at.asc())
                .all()
            )
            out[ct] = [
                {
                    "device_id": r.device_id,
                    "device_tail": ("…" + r.device_id[-8:]) if r.device_id else "",
                    "client_type": r.client_type,
                    "client_label": CLIENT_LABELS.get(r.client_type, r.client_type),
                    "bound_at": r.bound_at.isoformat() if r.bound_at else None,
                    "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
                    "last_ip": r.last_ip,
                }
                for r in rows
            ]
        return out
    finally:
        db.close()
