"""书籍封面 — 契约字段读取 + 签名图片代理

设计原则：**信任接口契约，不猜测**。

后台添加第三方接口时，搜索脚本已经负责把上游千奇百怪的字段
统一成标准字段（见 core/script_examples.py 的契约）：

    book = {'id': ..., 'bookTitle': ..., 'bookImage': item.get('随便什么原始字段')}

既然映射工作在脚本里已经完成，本模块**只读契约字段**（cover / bookImage），
绝不扫描 dict 去「猜」哪个字段像封面。

为什么不猜：搜索结果里的自定义字段会原样透传给章节脚本（契约明确要求），
因此 dict 中常混有 avatar（主播头像）、logo（站点标识）、qrcode（二维码）、
shareImage（分享缩略图）等图片 URL。贪心匹配会把它们误当封面显示——
用户本来就是靠封面区分同名书籍，给错图比不给图更糟。

启发式扫描只保留在 `describe_cover_detection()` 里，作为后台「测试接口」时
给管理员看的**诊断建议**（提示「你是不是想把 xxx 映射成 bookImage」），
永远不参与运行时取值。
"""

import base64
import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 契约字段：脚本接口按 script_examples.py 的约定输出 bookImage；
# cover 是适配器归一化后的标准名，官方适配器也用它。二者之外一律不认。
_CONTRACT_KEYS = ("cover", "bookImage")

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def normalize_cover_url(url: Any) -> str:
    """协议补全 + 校验。`//img.x.com/a.jpg` → `https://img.x.com/a.jpg`

    非字符串、空值、或不是 http(s)/data:image 的内容一律返回空串，
    避免把 'null'、'暂无封面' 这类脏数据塞进 <img src>。
    """
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://", "data:image/")):
        return u
    return ""


def extract_cover(item: Any) -> str:
    """读取契约字段中的封面 URL 并做协议补全；没有则返回空串。

    只认 cover / bookImage —— 字段映射是搜索脚本的职责，不在这里猜。
    """
    if not isinstance(item, dict):
        return ""
    for key in _CONTRACT_KEYS:
        url = normalize_cover_url(item.get(key))
        if url:
            return url
    return ""


# ── 仅用于后台诊断：帮管理员定位「该把哪个字段映射成 bookImage」──
_HINT_SCAN_KEYS = ("cover", "image", "img", "pic", "thumb", "photo", "poster")
# 这些几乎肯定不是书籍封面，诊断时明确排除，避免误导管理员
_HINT_EXCLUDE = ("avatar", "logo", "qrcode", "qr_code", "share", "icon", "banner", "ad")


def describe_cover_detection(item: Any) -> str:
    """封面为空时，给管理员一句可执行的说明（后台「测试接口」用）。

    这里会扫描疑似图片字段作为**建议**，但绝不影响运行时取值。
    """
    if not isinstance(item, dict):
        return "搜索结果不是标准字典结构，无法读取封面。"

    likely, unlikely = [], []
    for k, v in item.items():
        if not isinstance(k, str) or k in _CONTRACT_KEYS:
            continue
        if not normalize_cover_url(v):
            continue
        lower = k.lower()
        if any(bad in lower for bad in _HINT_EXCLUDE):
            unlikely.append(k)
        elif any(tag in lower for tag in _HINT_SCAN_KEYS):
            likely.append(k)
        else:
            unlikely.append(k)

    base = ("未读取到封面：搜索脚本需要把封面映射到标准字段 bookImage（或 cover），"
            "例如 book['bookImage'] = item.get('上游字段名')。")
    if likely:
        return base + f" 该接口返回了疑似封面字段 {likely}，可优先尝试。"
    if unlikely:
        return (base + f" 另外检测到图片字段 {unlikely}，"
                "但它们看起来像头像/图标/分享图而非书籍封面，请确认后再映射。")
    return base + " 当前结果中未发现任何图片 URL，可能该接口本身不提供封面。"


# ════════════════════════════════════════
#  签名图片代理
# ════════════════════════════════════════

_SECRET_KEY = "cover_sign_secret"
_secret_cache: Optional[str] = None


def _get_secret() -> str:
    """读取（必要时生成并持久化）HMAC 签名密钥。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    try:
        from db.session import SessionLocal
        from db.models import ApiConfig
        db = SessionLocal()
        try:
            row = db.query(ApiConfig).filter_by(cfg_key=_SECRET_KEY).first()
            if row and row.cfg_value:
                _secret_cache = row.cfg_value
                return _secret_cache
            value = secrets.token_urlsafe(32)
            if row:
                row.cfg_value = value
            else:
                db.add(ApiConfig(cfg_key=_SECRET_KEY, cfg_value=value, category="settings"))
            db.commit()
            _secret_cache = value
            return value
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        # 数据库不可用时退化为进程内随机密钥（重启后旧链接失效，但功能不中断）
        logger.warning(f"读取封面签名密钥失败，使用临时密钥: {e}")
        _secret_cache = secrets.token_urlsafe(32)
        return _secret_cache


def _b64e(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(raw: str) -> str:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad).decode("utf-8")


def _sign(payload: str, expires: int) -> str:
    msg = f"{payload}.{expires}".encode("utf-8")
    return hmac.new(_get_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def sign(url: str, ttl_seconds: int = 86400) -> str:
    """把上游封面 URL 签成本服务器的相对代理路径（含过期时间与签名）。"""
    if not url:
        return ""
    payload = _b64e(url)
    expires = int(time.time()) + max(60, ttl_seconds)
    return f"/api/skills/cover?u={payload}&e={expires}&s={_sign(payload, expires)}"


def verify(payload: str, expires: str, signature: str) -> Optional[str]:
    """校验签名与有效期，通过则返回原始 URL，否则 None。"""
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    if not hmac.compare_digest(_sign(payload, exp), signature or ""):
        return None
    try:
        return _b64d(payload)
    except Exception:  # noqa: BLE001
        return None


def is_safe_fetch_target(url: str) -> bool:
    """SSRF 防护：只允许 http(s)，且解析后的 IP 不得指向内网/回环/链路本地。

    签名机制已保证 URL 出自本服务器签发（即适配器返回值），此处为纵深防御。
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except Exception:  # noqa: BLE001
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True
