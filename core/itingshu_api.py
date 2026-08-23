"""第三方 API 客户端 — api.itingshu.iiisss.top

提供搜索、章节列表、音频直链获取功能，无需登录。
"""

import hashlib
import json
import time
import base64
from urllib.parse import urlencode

import requests
import urllib3
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 共享 session（复用连接）
# 连接池调到 32：批量下载并发上限为 30，默认池大小 10 会导致连接被丢弃重建
_session = requests.Session()
_session.verify = False
_session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
_session.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=32))


# ── 接口级配置（必须由接口管理中该接口自身提供，无全局默认值）──
_REQUIRED_KEYS = ("host", "toket", "device_key", "user", "sign", "book_token", "app_version")


def _validate_cfg(cfg: dict | None) -> dict:
    """校验接口级配置完整性，缺失则抛出清晰错误（不再回退全局默认值）。"""
    if not isinstance(cfg, dict) or not cfg:
        raise RuntimeError(
            "第三方接口未配置密钥：请在「接口管理」中为该接口填写"
            " 接口地址 / Token / 设备 Key / User / Sign / Book Token / App 版本"
        )
    missing = [k for k in _REQUIRED_KEYS if not str(cfg.get(k, "")).strip()]
    if missing:
        raise RuntimeError(
            "第三方接口密钥不完整，缺少以下字段：" + "、".join(missing) +
            "。请在「接口管理」中补全后重试。"
        )
    return cfg


def _host(cfg: dict) -> str:
    return cfg["host"]


def _app_ver(cfg: dict) -> str:
    return cfg["app_version"]


def _token(ts: int | None, cfg: dict) -> tuple[str, int]:
    """生成 MD5 时间戳 token"""
    if ts is None:
        ts = int(time.time())
    toket = cfg["toket"]
    return hashlib.md5(f"{toket}{ts}".encode()).hexdigest(), ts


def _enc(data, key: str) -> str:
    """AES-ECB 加密"""
    if isinstance(data, (dict, list)):
        p = json.dumps(data, separators=(",", ":"))
    else:
        p = str(data)
    c = AES.new(key.encode(), AES.MODE_ECB)
    return base64.b64encode(c.encrypt(pad(p.encode(), 16))).decode()


def _request(url: str, cfg: dict) -> dict:
    """发送 GET 请求，返回 JSON 响应"""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 uni-app",
        "x-Requested-With": "com.itingshu.hearbook",
        "Accept": "application/json",
        "sign": cfg["sign"],
    }
    r = _session.get(url, headers=hdrs, timeout=30)
    r.raise_for_status()
    return r.json()


# ════════════════════════════════════════════
#  公开 API
# ════════════════════════════════════════════

def search_books(
    keyword: str,
    cfg: dict,
    platform: str = "qq",
    limit: int = 30,
    offset: int = 1,
) -> list:
    """搜索书籍

    :param keyword: 书名/作者
    :param cfg: 接口级配置（必填，由接口管理提供）
    :param platform: 平台标识（qq）
    :param limit: 每页数量
    :param offset: 偏移
    :return: [{novel: {id, name, cover, intro, ...}, author: {name}, ...}]
    """
    cfg = _validate_cfg(cfg)
    t, ts = _token(ts=None, cfg=cfg)
    params = {
        'platform': platform,
        'key': keyword,
        'type': 1,
        'limit': limit,
        'offset': offset,
        'types': 'lastupdate',
        'time': ts,
        'token': t,
        'appVersion': _app_ver(cfg),
    }
    url = f"{_host(cfg)}/api/itingshu/cloudsearch?{urlencode(params)}"
    resp = _request(url, cfg)
    return resp.get("data", [])


def get_chapters(book_id: str, cfg: dict) -> dict:
    """获取书籍全部章节

    :param book_id: 书籍 ID（如 "ujMqEg"）
    :param cfg: 接口级配置（必填，由接口管理提供）
    :return: {info: {id, name, cover, zhubo}, total: int, list: [{id, oid, name}, ...]}
    """
    cfg = _validate_cfg(cfg)
    t, ts = _token(ts=None, cfg=cfg)
    params = {
        'id': book_id,
        'user': cfg["user"],
        'time': ts,
        'token': cfg["book_token"],
        'appVersion': _app_ver(cfg),
    }
    url = f"{_host(cfg)}/api/itingshu/bookdirst?{urlencode(params)}"
    resp = _request(url, cfg)
    return {
        "book_id": book_id,
        "info": resp.get("info", {}),
        "total": resp.get("total", 0),
        "list": resp.get("list", []),
    }


def get_audio_url(book_id: str, chapter_id: str, cfg: dict) -> str:
    """获取音频下载直链

    :param book_id:    书籍 ID
    :param chapter_id: 章节 ID
    :param cfg: 接口级配置（必填，由接口管理提供）
    :return: mp3 音频直链 URL
    """
    cfg = _validate_cfg(cfg)
    ts = int(time.time())
    device_key = cfg["device_key"]
    app_ver = _app_ver(cfg)

    # 生成内部签名
    inner = _enc(
        f"{app_ver}-{ts}-{hashlib.md5(f'{book_id}-{chapter_id}.mp3'.encode()).hexdigest()}",
        device_key,
    )

    # 加密请求体
    encrypted = _enc({
        "bookID": book_id,
        "chapterID": chapter_id,
        "time": ts,
        "koten": cfg["user"],
        "sign": inner,
    }, device_key)

    # 生成 token
    t = hashlib.md5(f"{cfg['toket']}{ts}".encode()).hexdigest()

    params = {
        'encrypted': encrypted,
        'time': str(ts),
        'token': t,
        'appVersion': app_ver,
    }
    url = f"{_host(cfg)}/api/itingshu/audio?{urlencode(params)}"

    resp = _request(url, cfg)
    # 容错：itingshu 可能返回 HTTP 200 但业务错误（data/src 缺失），避免裸 KeyError 暴露难懂堆栈
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data or not data.get("src"):
        msg = resp.get("msg") if isinstance(resp, dict) else ""
        raise RuntimeError(f"第三方音频接口未返回直链（{msg or '未知错误'}）")
    return data["src"]
