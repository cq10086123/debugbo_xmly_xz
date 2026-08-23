"""风控加固模块 — 同 token 多 IP 并用检测、席位 IP 不一致告警（设备绑定的辅助防线）

原则：
- 只做「提高共享成本」的辅助检测，绝不阻断正常请求：所有检测 try/except 包裹、
  失败一律放行（fail-open），不影响核心业务链路。
- 同 token 多 IP 并用（默认开启 token_multi_ip_kick=1）：同一 token 在短窗口内
  来自 ≥2 个公网 IP → 判定 token 被拷贝共享，立即失效该 token（重新登录即可，
  正常单设备用户的出口 IP 不会秒级反复横跳）。
- 席位 IP 不一致（默认仅告警 device_seat_ip_mismatch=alert / enforce 强制踢较旧方）：
  正常用户网页与插件在同一台电脑 → 同一出口 IP；两个席位 IP 不同 = 疑似
  「网页 A 机 + 插件 B 机」拆开共享。
"""

import logging
import ipaddress
import threading
import time

logger = logging.getLogger(__name__)

# ── 同 token 多 IP 检测（进程内滑动窗口；token 生命周期短，无需落库）──
_IP_WINDOW_SECONDS = 600   # 10 分钟窗口
_IP_MAP_LOCK = threading.Lock()
_IP_MAP: dict[str, dict[str, float]] = {}   # token -> {ip: last_seen_ts}
_IP_MAP_MAX = 10000        # 防 dangling token 撑爆内存

# ── 席位 IP 不一致告警节流（每卡密最多 10 分钟一条日志）──
_SEAT_WARN_LOCK = threading.Lock()
_SEAT_WARN_LAST: dict[int, float] = {}   # card_id -> last_warn_ts


def _cfg(key: str, default: str) -> str:
    """读取设备绑定模块的配置缓存（同一套 api_config）。"""
    try:
        from core.device_binding import _load_cfg_cached
        return _load_cfg_cached().get(key, default) or default
    except Exception:
        return default


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def _ip_network_key(ip: str) -> str | None:
    """IP 归一化为网段键，消除正常网络波动导致的多 IP 误判：

    - 私网/回环/链路本地地址：不作为共享证据（返回 None，不计入窗口）
    - IPv4 → /24 网段（PPPoE 重拨/同运营商同城跳变通常在 /24 内）
    - IPv6 → /48 网段（ISP 给家庭用户分配 /48~/56，后缀会自动轮换）
    - 双栈 v4/v6 并存：各自归一后仍是两个键 —— 但 v4 键必为公网网段；
      同一用户浏览器不会在 10 分钟内于两个公网网段间横跳（切换 v4/v6
      时网络键虽不同，但极小概率高频交替；若仍误判可关闭开关兜底）。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return None
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{addr}/24", strict=False))
    return str(ipaddress.ip_network(f"{addr}/48", strict=False))


def record_token_ip(token: str, ip: str | None) -> bool:
    """记录 token 本次来源 IP（按网段归一）；窗口内出现 ≥2 个不同公网网段
    时返回 True（判定共享拷贝）。

    fail-safe：任何异常返回 False（放行）。
    """
    if not token or not ip:
        return False
    if not _truthy(_cfg("token_multi_ip_kick", "1")):
        return False
    net = _ip_network_key(ip)
    if net is None:   # 私网/解析失败：不作为证据
        return False
    now = time.time()
    try:
        with _IP_MAP_LOCK:
            nets_map = _IP_MAP.setdefault(token, {})
            nets_map[net] = now
            # 清理过期网段键
            for k in list(nets_map.keys()):
                if now - nets_map[k] > _IP_WINDOW_SECONDS:
                    del nets_map[k]
            if len(_IP_MAP) > _IP_MAP_MAX:
                oldest = sorted(_IP_MAP.items(), key=lambda kv: max(kv[1].values() or [0]))[: _IP_MAP_MAX // 2]
                for t, _ in oldest:
                    del _IP_MAP[t]
            return len(nets_map) >= 2
    except Exception:
        return False


def forget_token(token: str) -> None:
    """token 失效后清理窗口记录。"""
    try:
        with _IP_MAP_LOCK:
            _IP_MAP.pop(token, None)
    except Exception:
        pass


def token_ips(token: str) -> list[str]:
    """token 在滑动窗口内出现过的网段键列表（日志展示用）。"""
    try:
        with _IP_MAP_LOCK:
            return list(_IP_MAP.get(token, {}).keys())
    except Exception:
        return []


def check_seat_ip_mismatch(card_id: int, client_type: str | None, ip: str | None,
                           other_seat_ip: str | None) -> None:
    """席位 IP 不一致检测：默认仅告警；enforce 模式踢出较旧一方（由调用方执行踢出）。

    IP 先按网段归一化（v4 /24、v6 /48、私网不计），避免正常网络波动误报；
    本函数只负责记录与告警日志；尽力而为，绝不抛异常。
    """
    if not client_type or not ip or not other_seat_ip:
        return
    try:
        from core.device_binding import binding_scope, SCOPE_IP
        if binding_scope() == SCOPE_IP:
            return  # 宽松模式按网络绑定，主校验已保证同网络，席位检测无意义
    except Exception:
        pass
    net_a = _ip_network_key(ip)
    net_b = _ip_network_key(other_seat_ip)
    if net_a is None or net_b is None or net_a == net_b:
        return
    mode = _cfg("device_seat_ip_mismatch", "alert")
    if mode not in ("alert", "enforce"):
        return
    now = time.time()
    try:
        with _SEAT_WARN_LOCK:
            last = _SEAT_WARN_LAST.get(card_id, 0)
            should_log = now - last > 600
            if should_log:
                _SEAT_WARN_LAST[card_id] = now
        if should_log:
            logger.warning(
                "[设备风控] 卡密 %s 的 %s 席位与另一席位出口网段不一致（%s vs %s），疑似网页/插件分机共享（mode=%s）",
                card_id, client_type, ip, other_seat_ip, mode,
            )
    except Exception:
        pass


def get_other_seat_ip(card_id: int, client_type: str | None, db) -> str | None:
    """查询同卡密另一席位最近活跃会话的 IP（用于席位 IP 一致性检测）。"""
    if not client_type:
        return None
    try:
        from db.models import Session as CardSession
        other = "extension" if client_type == "web" else "web"
        row = (
            db.query(CardSession)
            .filter_by(card_id=card_id, client_type=other, is_active=True)
            .order_by(CardSession.last_active_at.desc())
            .first()
        )
        return row.ip if row else None
    except Exception:
        return None


def log_kick_event(token_tail: str, card_id: int, nets: list[str]) -> None:
    """token 多网段并用被踢时落 card_logs。"""
    try:
        from db.init_db import log_card_event
        log_card_event(
            "risk_kick", card_id,
            f"风控：token …{token_tail} 在 {_IP_WINDOW_SECONDS // 60} 分钟内来自多个公网网段"
            f"（{', '.join(nets[:5])}），已强制下线"
        )
    except Exception:
        pass
