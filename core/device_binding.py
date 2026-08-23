"""网络绑定模块 — 卡密×出口网络的绑定、顶号换绑、会话网络校验

机制（宽松的"一卡一网络"）：
- 按「出口网络」绑定：客户端 IP 归一化为网络键（IPv4 /24、IPv6 /48、
  局域网/内网统一 "lan"）。同一网络下不限设备与浏览器数量 ——
  家里 Chrome / Edge / 手机 / 插件随便用，互不顶号。
- 每张卡密允许 max_devices 个网络（默认 1，管理端可调 1~10，
  设 2 可同时容纳家里+公司两个网络）。
- 顶号自动换绑：新网络登录且名额已满 → 淘汰 bound_at 最早的网络，
  并失效该网络的全部会话（"谁最后登录谁用"）。
- token 拿到别的网络使用 → 拒绝但不顶号（回原网络自动恢复）；
  只有在新网络"登录"才触发换绑踢人。
- 非侵入：本模块是绑定逻辑唯一的家；api/deps.py 只在 _auth_card 调用一次
  check_session，其余业务代码零感知。
- 总开关 api_config.device_binding_enabled（默认关闭）：关闭时登录不落
  网络键、鉴权跳过校验 → 行为与历史版本完全一致，等价于一键回退。

兼容策略：
- 存量 sessions/bindings 中 device_id 为 NULL 或旧版浏览器设备 ID
  （非网络键）→ 校验一律放行，下次登录起自然换轨（不误踢在线用户）。
- 数据库沿用 device_id 列名存网络键，无需迁移。
"""

import ipaddress
import threading
import time
from datetime import datetime, timedelta, timezone

from db.session import SessionLocal
from db.models import Session as CardSession, DeviceBinding, ApiConfig
from db.init_db import log_card_event

# ── 常量与默认值 ──
CLIENT_WEB = "web"
CLIENT_EXTENSION = "extension"
VALID_CLIENT_TYPES = (CLIENT_WEB, CLIENT_EXTENSION)
CLIENT_LABELS = {CLIENT_WEB: "网页", CLIENT_EXTENSION: "插件"}

DEFAULT_MAX_DEVICES = 1         # 每张卡默认允许的网络数
MAX_DEVICES_LIMIT = 10          # 管理端可设置的上限，防误填
DEFAULT_SESSION_IDLE_DAYS = 7   # 会话空闲过期天数（可配 session_idle_days）

# 局域网/私网来源统一归一为该键（直连内网部署时同一户内所有机器视作同一网络）
LAN_SCOPE_KEY = "lan"


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


def trust_proxy_header() -> bool:
    """是否信任反向代理传递的 X-Forwarded-For / X-Real-IP（api_config.trust_proxy_header，默认关）。

    仅当服务部署在可信反向代理（nginx 等）之后才应开启，否则攻击者可伪造头
    绕过按 IP 的绑定/限流。直连部署保持关闭（默认）。
    """
    return _truthy(_load_cfg_cached().get("trust_proxy_header", "0"))


def resolve_client_ip(request) -> str | None:
    """解析客户端真实 IP：

    - trust_proxy_header 开启：优先 X-Forwarded-For 首个合法 IP，再看 X-Real-IP；
    - 否则/解析失败：回退 request.client.host（直连语义）。
    网络绑定、登录限流、风控均应经此取 IP，保证反代部署下语义正确。
    """
    direct = request.client.host if request is not None and request.client else None
    if request is None or not trust_proxy_header():
        return direct
    try:
        # XFF 从右往左取第一个合法 IP：最右侧是可信代理亲手追加的真实来源；
        # 左侧字段可被客户端伪造（nginx $proxy_add_x_forwarded_for 为追加模式，
        # 若取最左侧，攻击者自带伪造 XFF 即可冒充任意 IP/局域网）。
        xff = request.headers.get("x-forwarded-for") or ""
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        for p in reversed(parts):
            try:
                ipaddress.ip_address(p)
                return p
            except ValueError:
                continue   # 非法段跳过，继续向左找/回退 X-Real-IP
        xri = (request.headers.get("x-real-ip") or "").strip()
        if xri:
            try:
                ipaddress.ip_address(xri)
                return xri
            except ValueError:
                pass
    except Exception:
        pass
    return direct


# ════════════════════════════════════════
#  IP → 网络键归一化（绑定标识）
# ════════════════════════════════════════
def ip_scope_key(ip: str | None) -> str | None:
    """把客户端 IP 归一化为「网络键」（绑定标识）：

    - 私网/回环/链路本地（直连内网部署、同一户内网）→ 统一 "lan"
    - IPv4 → /24 网段（家庭宽带重拨/同城跳变通常落在同网段，避免误踢）
    - IPv6 → /48 网段（ISP 给家庭分配 /48~/56，接口后缀会轮换）
    - 解析失败/为空 → None（调用方 fail-open：不建绑定、不作踢出证据）
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return LAN_SCOPE_KEY
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{addr}/24", strict=False))
    return str(ipaddress.ip_network(f"{addr}/48", strict=False))


def is_ip_scope_key(v: str | None) -> bool:
    """判断存量绑定/会话里的标识是否是网络键（旧版曾存浏览器设备 ID）。

    网络键形如 "1.2.3.0/24"、"2001:db8::/48" 或 "lan"；旧设备 ID 不含 "/"。
    非网络键的存量数据一律放行/照常展示，等用户下次登录自然换轨。
    """
    if not v:
        return False
    return v == LAN_SCOPE_KEY or "/" in v


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
    """读取卡密允许的网络数上限（NULL/非法 → 1，钳制 1~10）。"""
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
    sess_net_key: str | None,
    sess_last_active: datetime | None,
    request_net_key: str | None,
    bound_net_keys: set[str],
    idle_days: int,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """会话网络校验（纯函数）。返回 (ok, code, message)：

    - ok=True                放行（同网络下不限设备/浏览器数量）
    - code=network_changed   出口网络与登录时不同（换了家庭/网络）→ 重新登录即换绑
    - code=kicked            所属网络的绑定已被顶号换绑 / 解绑
    - code=session_expired   会话空闲超时
    宽松原则：
    - sess_net_key 为空（历史/免绑定会话）或不是网络键（旧版遗留）→ 放行
    - request_net_key 为空（拿不到客户端 IP）→ 不作为踢出证据，放行
    """
    if not sess_net_key or not is_ip_scope_key(sess_net_key):
        return True, "", ""

    now = now or datetime.now(timezone.utc)

    # 1) 出口网络必须与登录时一致（换 IP = 换设备语义；不顶号，回原网络恢复）
    if request_net_key is not None and request_net_key != sess_net_key:
        return False, "network_changed", "网络环境已变更，请重新登录"

    # 2) 会话所属网络必须仍在绑定名额内（防已被顶号/解绑的旧网络继续用）
    if sess_net_key not in bound_net_keys:
        return False, "kicked", "账号已在其他网络登录，本设备已下线"

    # 3) 空闲过期
    last = _aware(sess_last_active)
    if last is not None and (now - last) > timedelta(days=idle_days):
        return False, "session_expired", "登录已过期（长期未使用），请重新登录"

    return True, "", ""


# ════════════════════════════════════════
#  绑定维护（登录 / 解绑 / 踢线，需要 db 会话）
# ════════════════════════════════════════
def bound_network_keys(db, card_id: int) -> set[str]:
    """卡密当前绑定的网络键集合。"""
    rows = db.query(DeviceBinding).filter_by(card_id=card_id).all()
    return {r.device_id for r in rows}


def _id_label(v: str | None) -> str:
    """绑定标识的展示形式：网络键整串展示，旧版设备 ID 只展示尾部。"""
    if not v:
        return ""
    return f"网络 {v}" if is_ip_scope_key(v) else f"…{v[-8:]}"


# 绑定维护互斥锁：登录的「查名额 → 淘汰 → 插入」是非原子序列，并发登录同一卡密
# 会双写超额绑定（TOCTOU）。单进程自托管（uvicorn 单 worker）下进程内锁即可
# 保证串行；登录是低频操作，全局锁足够。
_BIND_LOCK = threading.Lock()


def deactivate_network_sessions(db, card_id: int, net_key: str) -> int:
    """失效指定网络的全部活跃会话（顶号/解绑共用）。返回失效条数。"""
    rows = db.query(CardSession).filter_by(
        card_id=card_id, device_id=net_key, is_active=True
    ).all()
    for s in rows:
        s.is_active = False
    return len(rows)


def bind_network_on_login(db, card, client_type: str | None, net_key: str, ip: str | None = None) -> dict:
    """登录时维护网络绑定（名额已满 → 顶号自动换绑，淘汰最早绑定的网络）。

    返回 {binding, evicted: [被踢网络键], kicked_sessions: n}。调用方负责 commit。
    """
    with _BIND_LOCK:
        max_devices = get_max_devices(card)
        existing = (
            db.query(DeviceBinding)
            .filter_by(card_id=card.id)
            .order_by(DeviceBinding.bound_at.asc())
            .all()
        )

        # 同网络已绑定：续活并更新 IP，无需换绑
        for row in existing:
            if row.device_id == net_key:
                row.last_active_at = datetime.now(timezone.utc)
                row.last_ip = ip or row.last_ip
                return {"binding": row, "evicted": [], "kicked_sessions": 0}

        # 名额已满：淘汰最早的绑定（顶号换绑）
        evicted: list[str] = []
        kicked_sessions = 0
        while len(existing) >= max_devices:
            oldest = existing.pop(0)
            evicted.append(oldest.device_id)
            kicked_sessions += deactivate_network_sessions(db, card.id, oldest.device_id)
            db.delete(oldest)

        binding = DeviceBinding(
            card_id=card.id,
            client_type=client_type or CLIENT_WEB,   # 信息字段：最先从哪端绑定
            device_id=net_key,
            bound_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
            last_ip=ip,
        )
        db.add(binding)

        if evicted:
            log_card_event(
                "device_kick", card.id,
                f"卡密 {card.code} 顶号换绑：新登录 {_id_label(net_key)} 踢掉 "
                f"{', '.join(_id_label(e) for e in evicted)}（失效会话 {kicked_sessions} 条）"
            )
        else:
            log_card_event(
                "device_bind", card.id,
                f"卡密 {card.code} 绑定 {_id_label(net_key)}" + (f"（IP {ip}）" if ip else "")
            )

        return {"binding": binding, "evicted": evicted, "kicked_sessions": kicked_sessions}


def touch_binding(db, card_id: int, net_key: str | None, ip: str | None = None) -> None:
    """鉴权通过后刷新绑定的活跃时间/IP（尽力而为，失败不影响请求）。"""
    if not net_key:
        return
    try:
        row = db.query(DeviceBinding).filter_by(card_id=card_id, device_id=net_key).first()
        if row:
            row.last_active_at = datetime.now(timezone.utc)
            if ip:
                row.last_ip = ip
    except Exception:
        pass


def trim_bindings_to_limit(db, card) -> int:
    """把绑定数裁剪回 max_devices（淘汰 bound_at 最早的），并失效被裁网络的会话。

    用于管理员下调 max_devices 后即时收敛存量绑定。返回被裁掉的绑定数。
    """
    with _BIND_LOCK:
        max_devices = get_max_devices(card)
        rows = (
            db.query(DeviceBinding)
            .filter_by(card_id=card.id)
            .order_by(DeviceBinding.bound_at.asc())
            .all()
        )
        excess = rows[: max(0, len(rows) - max_devices)]
        for row in excess:
            deactivate_network_sessions(db, card.id, row.device_id)
            db.delete(row)
        if excess:
            log_card_event(
                "device_unbind", card.id,
                f"卡密 {card.code} 网络数上限下调为 {max_devices}，裁剪超额绑定 {len(excess)} 个（即时失效）"
            )
        return len(excess)


def unbind_device(db, card, net_key: str) -> int:
    """解绑指定网络（删除绑定 + 失效其会话）。返回解绑数。

    持 _BIND_LOCK：与并发登录的「查名额→淘汰→插入」互斥。
    """
    with _BIND_LOCK:
        rows = db.query(DeviceBinding).filter_by(card_id=card.id, device_id=net_key).all()
        for row in rows:
            deactivate_network_sessions(db, card.id, row.device_id)
            db.delete(row)
        if rows:
            log_card_event(
                "device_unbind", card.id,
                f"卡密 {card.code} 管理员解绑 {_id_label(net_key)}（含会话清理）"
            )
        return len(rows)


def unbind_all(db, card) -> int:
    """解绑该卡密全部网络并踢下线全部会话。返回解绑数。"""
    with _BIND_LOCK:
        rows = db.query(DeviceBinding).filter_by(card_id=card.id).all()
        for row in rows:
            db.delete(row)
        # 兜底：清掉所有活跃会话（含历史免绑定会话）
        for s in db.query(CardSession).filter_by(card_id=card.id, is_active=True).all():
            s.is_active = False
        if rows:
            log_card_event("device_unbind", card.id, f"卡密 {card.code} 管理员解绑全部网络（{len(rows)} 个）并踢下线")
        return len(rows)


def kick_all_sessions(db, card) -> int:
    """仅踢下线（保留绑定关系）：用户在原网络重新登录即可，不占换绑名额。"""
    with _BIND_LOCK:
        rows = db.query(CardSession).filter_by(card_id=card.id, is_active=True).all()
        for s in rows:
            s.is_active = False
        if rows:
            log_card_event("device_kick", card.id, f"卡密 {card.code} 管理员踢下线全部会话（{len(rows)} 条）")
        return len(rows)


# ════════════════════════════════════════
#  鉴权入口（api/deps.py 唯一调用点）
# ════════════════════════════════════════
def check_session(db, sess: CardSession, client_ip: str | None) -> tuple[bool, str, str]:
    """总开关下的会话网络校验（开关关闭 → 直接放行）。"""
    if not device_binding_enabled():
        return True, "", ""
    return check_session_pure(
        sess.device_id,
        sess.last_active_at,
        ip_scope_key(client_ip),
        bound_network_keys(db, sess.card_id),
        session_idle_days(),
    )


def device_binding_status(card) -> list[dict]:
    """卡密的网络绑定列表（管理端展示用）。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(DeviceBinding)
            .filter_by(card_id=card.id)
            .order_by(DeviceBinding.bound_at.asc())
            .all()
        )
        return [
            {
                "device_id": r.device_id,
                "label": _id_label(r.device_id),
                "client_type": r.client_type,
                "client_label": CLIENT_LABELS.get(r.client_type, r.client_type),
                "bound_at": r.bound_at.isoformat() if r.bound_at else None,
                "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
                "last_ip": r.last_ip,
            }
            for r in rows
        ]
    finally:
        db.close()
