"""搜索接口 — /api/search（喜马拉雅官方接口，免登录，需卡密鉴权）"""

import asyncio
import logging
import requests
import urllib3
from fastapi import APIRouter, Depends

from api.deps import get_current_card, ensure_interface_allowed

logger = logging.getLogger(__name__)
urllib3.disable_warnings()

router = APIRouter(prefix="/api/search", tags=["搜索（官方）"])

SEARCH_API = "https://www.ximalaya.com/revision/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://www.ximalaya.com/",
}
_session = requests.Session()
_session.headers.update(HEADERS)


def _fix_cover_url(url: str) -> str:
    if url and url.startswith("//"):
        return "https:" + url
    return url


def _do_search(keyword: str, page: int) -> dict:
    params = {
        "core": "album", "kw": keyword, "page": page, "spellchecker": "true",
        "rows": 20, "condition": "relation", "device": "iPhone",
    }
    resp = _session.get(SEARCH_API, params=params, timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()


@router.get("")
async def search_albums(keyword: str, page: int = 1, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    if not keyword or not keyword.strip():
        return {"success": False, "error": "请输入搜索关键词"}
    try:
        data = await asyncio.to_thread(_do_search, keyword.strip(), page)
        if data.get("ret") != 200:
            return {"success": False, "error": data.get("msg", "搜索失败")}
        response = data.get("data", {}).get("result", {}).get("response", {})
        docs = response.get("docs", [])
        num_found = response.get("numFound", 0)
        if not docs:
            return {"success": True, "results": [], "total": 0}
        results = []
        for doc in docs:
            results.append({
                "albumId": doc.get("id"), "title": doc.get("title", ""),
                "type": doc.get("category_title", ""), "author": doc.get("nickname", ""),
                "cover": _fix_cover_url(doc.get("cover_path", "")), "intro": doc.get("intro", ""),
                "tracks": doc.get("tracks", 0), "is_finished": doc.get("is_finished", 0),
                "play": doc.get("play", 0), "score": doc.get("score", 0),
            })
        return {"success": True, "results": results, "total": num_found}
    except requests.Timeout:
        return {"success": False, "error": "搜索超时，请重试"}
    except requests.RequestException as e:
        return {"success": False, "error": f"搜索请求失败: {e}"}
    except Exception as e:
        logger.exception(f"{e}")
        return {"success": False, "error": f"搜索异常: {e}"}
