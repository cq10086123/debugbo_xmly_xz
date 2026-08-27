"""书籍封面 — 接口无关的通用提取 + 签名图片代理

设计原则（重要）：**绝不为单个接口写特判**。

1. 提取层 `extract_cover()`：按候选键名清单从任意搜索结果 dict 里挖封面。
   官方适配器输出 `cover`，脚本适配器 `_normalize_book` 输出 `cover`/`bookImage`，
   而未来新增的第三方脚本可能直接吐 `pic` / `img` / `thumb` / `avatar` …
   这里一次性穷举常见键名并支持嵌套，因此**新接口无需改任何代码即可支持封面**。

2. 投递层 `sign()` / `verify()`：AI 平台的图片渲染器是「无凭证的浏览器」，
   既不会带 Bearer 头，也常被上游 CDN 的防盗链拦掉。
   故由本服务器做中转：签发一枚 HMAC 签名、带过期时间的 URL，
   渲染器直接 GET 即可拿到图片字节，不泄露卡密 token。
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

# ── 候选键名：从最规范到最少见，命中即止 ──
# 新接口只要用了其中任意一种命名，封面功能自动生效。
_COVER_KEYS = (
    "cover", "cover_url", "coverUrl", "cover_path", "coverPath",
    "bookImage", "book_image", "albumCover", "album_cover",
    "image", "imageUrl", "image_url", "img", "imgUrl", "img_url",
    "pic", "picUrl", "pic_url", "picture", "photo",
    "thumb", "thumbnail", "thumbUrl", "thumb_url",
    "logo", "avatar", "poster", "middleCover", "largeCover",
)

# 可能藏着封面的嵌套容器
_NESTED_KEYS = ("novel", "book", "album", "info", "data", "detail")

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def normalize_cover_url(url: Any) -> str:
    """协议补全 + 去空白。`//img.x.com/a.jpg` → `https://img.x.com/a.jpg`"""
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


def _looks_like_image(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip().lower()
    if v.startswith("data:image/"):
        return True
    if not (v.startswith("http://") or v.startswith("https://") or v.startswith("//")):
        return False
    path = urlparse(v if not v.startswith("//") else "https:" + v).path
    # 带图片扩展名 → 确定；无扩展名的 CDN 动图链接也放行（很多接口如此）
    return path.endswith(_IMAGE_EXT) or True


def extract_cover(item: Any, _depth: int = 0) -> str:
    """从任意结构的搜索结果中提取封面 URL；提取不到返回空串。

    与接口实现完全解耦：先按候选键名匹配，再递归下探常见嵌套容器。
    """
    if not isinstance(item, dict) or _depth > 3:
        return ""
    for key in _COVER_KEYS:
        if key in item:
            url = normalize_cover_url(item.get(key))
            if url and _looks_like_image(item.get(key)):
                return url
    # 兜底：键名含 cover/image/pic/img 且值像图片 URL
    for key, value in item.items():
        if not isinstance(key, str):
            continue
        k = key.lower()
        if any(tag in k for tag in ("cover", "image", "img", "pic", "thumb")):
            url = normalize_cover_url(value)
            if url and _looks_like_image(value):
                return url
    for key in _NESTED_KEYS:
        nested = item.get(key)
        if isinstance(nested, dict):
            found = extract_cover(nested, _depth + 1)
            if found:
                return found
    return ""


def describe_cover_detection(item: Any) -> str:
    """封面没被识别时，给管理员一句可执行的说明（后台「测试接口」用）。

    会扫描该条结果里所有「看起来像图片 URL」的字段，直接点名建议改成 cover。
    """
    if not isinstance(item, dict):
        return "搜索结果不是标准字典结构，无法提取封面。"
    candidates = [k for k, v in item.items()
                  if isinstance(k, str) and normalize_cover_url(v) and _looks_like_image(v)]
    if candidates:
        return (
            f"该接口返回了疑似图片字段 {candidates}，但字段名不含 "
            "cover/image/img/pic/thumb 等可识别语义，因此未被自动识别。"
            f"请在搜索脚本里把它改名或补一份为 cover，例如："
            f"book['cover'] = item.get('{candidates[0]}')"
        )
    return (
        "该接口的搜索结果里没有发现任何图片 URL 字段。"
        "若上游确实提供封面，请在搜索脚本中取出并以 cover 为键返回："
        "book['cover'] = item.get('你的字段名')"
    )


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
