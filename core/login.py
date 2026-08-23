"""登录模块 — 喜马拉雅二维码登录

并发安全：每个二维码（qr_id）使用独立的 requests.Session，避免多个卡密用户
同时扫码时共用全局 session 互相污染 device/WAF cookie。会话按 qr_id 缓存，
登录成功或超时后由调用方调用 cleanup_qr 清理。
"""

import uuid
import time
import urllib3
import requests
from core.config import COOKIE_FILE

# 关闭 SSL 证书验证警告（内网工具）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_QRCODE_URL = "https://passport.ximalaya.com/web/qrCode"

# 统一 User-Agent（二维码会话与登录态校验共用，避免引用已删除的全局 SESSION）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) ????/4.0.11 Chrome/102.0.5005.167 "
    "Electron/19.1.1 Safari/537.36 4.0.11"
)

# qr_id -> (Session, 创建时间戳)；每个二维码独立会话，保证并发互不干扰
_QR_SESSIONS: dict[str, tuple[requests.Session, float]] = {}
_QR_SESSION_TTL = 360.0  # 秒，过期自动回收


def _new_session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://passport.ximalaya.com",
        "Referer": "https://passport.ximalaya.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    })
    return s


def _generate_device_id() -> str:
    device_uuid = str(uuid.uuid4())
    return f"win32&{device_uuid}&4.0.11"


def _init_waf_cookies(s: requests.Session) -> None:
    try:
        s.get("https://passport.ximalaya.com", timeout=5)
    except Exception:
        pass


def _qr_session(qr_id: str) -> requests.Session:
    """获取（或惰性重建）某个二维码对应的 session"""
    item = _QR_SESSIONS.get(qr_id)
    now = time.time()
    if item is not None:
        s, created = item
        if now - created < _QR_SESSION_TTL:
            return s
        # 过期：回收
        _QR_SESSIONS.pop(qr_id, None)
    # 重建（极少触发：二维码已过期但仍被轮询）。
    # 必须写回 _QR_SESSIONS：否则之后每次轮询都新建 session（device id 各不相同），
    # 且 extract_cookies 读不到该会话，扫码登录必然失败。
    s = _new_session()
    s.cookies.set("1&_device", _generate_device_id())
    _QR_SESSIONS[qr_id] = (s, now)
    return s


def generate_qrcode() -> dict:
    """生成二维码，返回 {"qrId": "...", "img": "base64..."}，并缓存独立会话"""
    s = _new_session()
    s.cookies.set("1&_device", _generate_device_id())
    _init_waf_cookies(s)

    url = f"{BASE_QRCODE_URL}/gen"
    params = {"level": "L", "source": "喜马拉雅电脑版"}
    resp = s.get(url, params=params, timeout=15)
    data = resp.json()

    if data.get("ret") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('msg', 'unknown error')}")

    qr_id = data.get("qrId") or (data.get("data") or {}).get("qrId")
    if qr_id:
        _QR_SESSIONS[qr_id] = (s, time.time())
    return data


def check_scan_status(qr_id: str) -> dict:
    """轮询扫码状态（使用 qr_id 专属会话，并发安全）"""
    s = _qr_session(qr_id)
    ts = int(time.time() * 1000)
    url = f"{BASE_QRCODE_URL}/check/{qr_id}/{ts}"
    resp = s.get(url, timeout=15)
    return resp.json()


def extract_cookies(qr_id: str) -> dict:
    """提取某个二维码会话中的所有关键 Cookie"""
    s = _QR_SESSIONS.get(qr_id)
    if s is None:
        return {}
    return {cookie.name: cookie.value for cookie in s[0].cookies}


def cleanup_qr(qr_id: str) -> None:
    """登录成功/超时后清理二维码会话，避免内存泄漏"""
    _QR_SESSIONS.pop(qr_id, None)


def cookies_to_string(cookies: dict) -> str:
    """合并 Cookie 为 HTTP 格式"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def verify_login(cookies: dict) -> dict | None:
    """验证登录态，返回用户信息或 None"""
    s = requests.Session()
    s.verify = False
    for name, value in cookies.items():
        s.cookies.set(name, value)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN",
        "Content-Type": "application/json;charset=utf-8",
    })
    try:
        resp = s.get(
            "https://pc.ximalaya.com/pc-application-server/user/current/info",
            timeout=15,
        )
        data = resp.json()
        if data.get("ret") == 200 and data.get("data", {}).get("uid"):
            return data["data"]
    except Exception:
        pass
    return None


def save_cookie_string(cookie_str: str) -> str:
    """保存 Cookie 到文件"""
    COOKIE_FILE.write_text(cookie_str, encoding="utf-8")
    return str(COOKIE_FILE)


def load_cookie_string() -> str | None:
    """从文件加载 Cookie"""
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    return None
