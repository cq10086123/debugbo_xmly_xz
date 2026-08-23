"""风控加固模块 — 同 token 多 IP 并用检测（网络绑定的兜底防线）

原则：
- 只做「提高共享成本」的辅助检测，绝不阻断正常请求：所有检测 try/except 包裹、
  失败一律放行（fail-open），不影响核心业务链路。
- 同 token 多 IP 并用（默认开启 token_multi_ip_kick=1）：同一 token 在短窗口内
  来自 ≥2 个公网网段 → 判定 token 被拷贝共享，立即失效该 token（重新登录即可，
  正常单设备用户的出口 IP 不会秒级反复横跳）。
- 网络绑定开启时该检测基本不可达（异网请求先被绑定校验 401）；主要为
  绑定开关关闭、以及历史免绑定会话提供基础防共享能力。
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
