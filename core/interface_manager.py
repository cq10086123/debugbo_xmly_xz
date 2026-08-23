"""接口管理器 — 数据库驱动 + 适配器统一调度

设计目标：
- 把「官方接口 / 第三方接口 / Python 脚本接口」统一成可配置、可增删的接口实体。
- 内置适配器：official（包 XimalayaDownloader）、itingshu（包 itingshu 客户端）。
- Python 脚本接口：用户编写 ``def parse(params):``（与参考项目
  ``C:\\Users\\11754\\Desktop\\1\\py`` 的脚本契约完全一致），在子进程沙箱中运行，
  脚本自己发请求并返回标准结构（搜索 list[{id,bookTitle}] / 章节 list[{chapter_id,title}]
  / 音频 URL 字符串），支持加签、加密、多步请求、登录态保持等任意复杂逻辑。
"""

import json
import logging
import threading
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# 支持的接口类型
TYPE_OFFICIAL = "official"
TYPE_SCRIPT = "script"
VALID_TYPES = (TYPE_OFFICIAL, TYPE_SCRIPT)


def _fix_cover(url: str) -> str:
    if url and url.startswith("//"):
        return "https:" + url
    return url


# ════════════════════════════════════════════
#  适配器基类
# ════════════════════════════════════════════

class BaseInterfaceAdapter(ABC):
    """接口适配器基类

    统一输出格式（与前端现有字段保持一致）：
    - search_books -> {"success", "results":[{albumId,title,type,author,cover,intro,tracks,...}], "total"}
    - get_chapters -> {"success", "album_title", "tracks":[{trackId,title}], "track_total"}
    - get_audio_url -> str | None
    """

    # 是否支持「直接下载」（返回可直链 URL）。
    # 官方接口走专用面板（需 Selenium+cookie），统一批量下载仅支持直链型适配器。
    supports_direct_download: bool = True

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def search_books(self, keyword: str, page: int = 1) -> Dict[str, Any]:
        ...

    @abstractmethod
    def get_chapters(self, book_id: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def get_audio_url(self, book_id: str, chapter_id: str) -> Optional[str]:
        ...


# ════════════════════════════════════════════
#  官方接口适配器（包 XimalayaDownloader + 官方搜索）
# ════════════════════════════════════════════

class OfficialAdapter(BaseInterfaceAdapter):
    """官方接口：搜索用官方 Web 接口；章节/音频建议在专用面板下载"""

    supports_direct_download = False

    _SEARCH_API = "https://www.ximalaya.com/revision/search"
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.ximalaya.com/",
    }

    def __init__(self, name: str):
        super().__init__(name)
        self._session = requests.Session()
        self._session.headers.update(self._HEADERS)
        self._session.verify = False

    def search_books(self, keyword: str, page: int = 1) -> Dict[str, Any]:
        if not keyword or not keyword.strip():
            return {"success": False, "error": "请输入搜索关键词"}
        try:
            params = {
                "core": "album", "kw": keyword.strip(), "page": page,
                "spellchecker": "true", "rows": 20, "condition": "relation",
                "device": "iPhone",
            }
            resp = self._session.get(self._SEARCH_API, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ret") != 200:
                return {"success": False, "error": data.get("msg", "搜索失败")}
            response = data.get("data", {}).get("result", {}).get("response", {})
            docs = response.get("docs", [])
            num_found = response.get("numFound", 0)
            results = []
            for doc in docs:
                results.append({
                    "albumId": doc.get("id"),
                    "title": doc.get("title", ""),
                    "type": doc.get("category_title", ""),
                    "author": doc.get("nickname", ""),
                    "cover": _fix_cover(doc.get("cover_path", "")),
                    "intro": doc.get("intro", ""),
                    "tracks": doc.get("tracks", 0),
                    "is_finished": doc.get("is_finished", 0),
                    "play": doc.get("play", 0),
                    "score": doc.get("score", 0),
                })
            return {"success": True, "results": results, "total": num_found}
        except requests.Timeout:
            return {"success": False, "error": "搜索超时，请重试"}
        except requests.RequestException as e:
            return {"success": False, "error": f"搜索请求失败: {e}"}
        except Exception as e:
            logger.exception(f"official search error: {e}")
            return {"success": False, "error": f"搜索异常: {e}"}

    def get_chapters(self, book_id: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": "官方章节/下载请使用「官方接口」面板（需 VIP 账号与浏览器自动化）。",
        }

    def get_audio_url(self, book_id: str, chapter_id: str) -> Optional[str]:
        return None


# ════════════════════════════════════════════
#  Python 脚本接口适配器
# ════════════════════════════════════════════

class ScriptAdapter(BaseInterfaceAdapter):
    """Python 脚本接口：用户编写 ``def parse(params):``（与参考项目 py 的脚本契约完全一致）。

    每个阶段（搜索 / 章节 / 音频）一个独立脚本，均定义 ``def parse(params)``：
    - search   : params = {keyword, encoded_keyword, timestamp, timestamp_sec, page}
                 返回 list[dict]，每项含 id、bookTitle（及可选 bookImage/bookAnchor/count 等）
    - chapters : params = {bookId, page, page0, size, count, timestamp, timestamp_sec, **搜索结果自定义字段}
                 返回 list[dict]，每项含 chapter_id、title（及可选 order/duration 等）
    - audio    : params = {bookId, chapterId, trackId, rid, timestamp, timestamp_sec, **章节结果自定义字段}
                 返回 音频 URL 字符串

    适配器负责把 py 标准结构归一化为前端 / 批处理字段（albumId/title/author/cover、trackId/title），
    并把搜索结果、章节结果的自定义字段透传给后续阶段（与 py 的 params 透传一致）。

    config 结构：
    {
      "script_timeout": 30,
      "allowed_modules": [...],  # 可选：历史兼容字段；引擎默认已放行绝大多数模块（仅拦危险模块）
      "scripts": {
        "search": "def parse(params): ...",
        "chapters": "def parse(params): ...",
        "audio": "def parse(params): ..."
      }
    }
    """

    supports_direct_download = True

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name)
        from core.script_engine import ScriptInterface
        self.config = config or {}
        self.timeout = int(self.config.get("script_timeout") or 30)
        raw_modules = self.config.get("allowed_modules") or []
        self.allowed_modules = [str(m).strip() for m in raw_modules if str(m).strip()] \
            if isinstance(raw_modules, (list, tuple)) else []
        scripts = self.config.get("scripts") or {}
        search_src = (scripts.get("search") or "").strip()
        chapters_src = (scripts.get("chapters") or "").strip()
        audio_src = (scripts.get("audio") or "").strip()
        self._has_search = bool(search_src)
        self._has_chapters = bool(chapters_src)
        self._has_audio = bool(audio_src)
        # 常驻 worker 池：每个阶段一个，跨调用保持模块级状态（Session / 登录态 / 缓存）
        self._search_iface = ScriptInterface(search_src, "search", self.timeout,
                                             extra_modules=self.allowed_modules) if search_src else None
        self._chapters_iface = ScriptInterface(chapters_src, "chapters", self.timeout,
                                               extra_modules=self.allowed_modules) if chapters_src else None
        self._audio_iface = ScriptInterface(audio_src, "audio", self.timeout,
                                            extra_modules=self.allowed_modules) if audio_src else None
        # 自定义字段透传：搜索结果 / 章节结果按 id 缓存，供后续阶段读取（与 py 一致）
        self._search_cache: Dict[str, Dict] = {}
        self._chapter_cache: Dict[tuple, Dict] = {}

    def shutdown(self):
        """释放三个阶段的常驻 worker 池（接口被替换/删除时由管理器调用，防子进程泄漏）"""
        for iface in (self._search_iface, self._chapters_iface, self._audio_iface):
            if iface is not None:
                try:
                    iface.shutdown()
                except Exception:
                    logger.exception(f"释放脚本 worker 池失败: {self.name}")

    # ── 参数构建（与 py 的 _build_*_params 一致）──
    @staticmethod
    def _build_search_params(keyword: str, page: int) -> Dict[str, Any]:
        now = int(time.time())
        return {
            "keyword": keyword,
            "encoded_keyword": urllib.parse.quote(keyword),
            "timestamp": now * 1000,
            "timestamp_sec": now,
            "page": page,
        }

    @staticmethod
    def _build_chapters_params(book_id: str, page: int, size: int, count: Any,
                               search_item: Dict) -> Dict[str, Any]:
        now = int(time.time())
        params: Dict[str, Any] = {
            "bookId": str(book_id),
            "page": page,
            "page0": page - 1,
            "size": size,
            "count": int(count) if count else size,
            "timestamp": now * 1000,
            "timestamp_sec": now,
        }
        # 透传搜索结果的自定义字段（如 albumId / sourceUrl 等），脚本可 params.get('xxx') 读取
        for k, v in (search_item or {}).items():
            if k not in params:
                params[k] = v
        return params

    @staticmethod
    def _build_audio_params(book_id: str, chapter_id: str, chapter_item: Dict) -> Dict[str, Any]:
        now = int(time.time())
        params: Dict[str, Any] = {
            "bookId": str(book_id),
            "chapterId": str(chapter_id),
            "trackId": str(chapter_id),
            "rid": str(chapter_id),
            "timestamp": now * 1000,
            "timestamp_sec": now,
        }
        for k, v in (chapter_item or {}).items():
            if k not in params:
                params[k] = v
        return params

    # ── 归一化（py 标准结构 → 前端 / 批处理字段）──
    @staticmethod
    def _normalize_book(item: Dict) -> Dict[str, Any]:
        book_id = str(item.get("id", ""))
        title = item.get("bookTitle", "") or item.get("title", "")
        author = item.get("bookAnchor", "") or item.get("author", "")
        cover = item.get("bookImage", "") or item.get("cover", "")
        count = item.get("count", item.get("trackCount", item.get("total", 0)))
        try:
            count = int(count)
        except (ValueError, TypeError):
            count = 0
        norm = {
            "albumId": book_id,
            "id": book_id,
            "title": title,
            "bookTitle": title,
            "author": author,
            "bookAnchor": author,
            "cover": cover,
            "bookImage": cover,
            "count": count,
            "trackCount": count,
            "total": count,
            "type": item.get("category", "") or item.get("type", "") or "",
            "intro": item.get("bookDesc", "") or item.get("intro", "") or "",
        }
        # 保留脚本返回的其它自定义字段（供章节阶段透传）
        for k, v in item.items():
            if k not in norm:
                norm[k] = v
        return norm

    @staticmethod
    def _normalize_chapter(item: Dict) -> Dict[str, Any]:
        cid = str(item.get("chapter_id", ""))
        title = item.get("title", "")
        order = item.get("order", 0)
        try:
            order = int(order)
        except (ValueError, TypeError):
            order = 0
        norm = {
            "trackId": cid,
            "chapter_id": cid,
            "id": cid,
            "title": title,
            "order": order,
            "index": order,
            "duration": item.get("duration", "") or "",
        }
        for k, v in item.items():
            if k not in norm:
                norm[k] = v
        return norm

    def search_books(self, keyword: str, page: int = 1) -> Dict[str, Any]:
        if not keyword or not keyword.strip():
            return {"success": False, "error": "请输入搜索关键词"}
        if not self._has_search or self._search_iface is None:
            return {"success": False, "error": "该脚本接口未配置 search 脚本"}
        try:
            params = self._build_search_params(keyword.strip(), page)
            raw = self._search_iface.execute(params)
            self._search_cache.clear()
            results = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                bid = str(item.get("id", ""))
                self._search_cache[bid] = item
                results.append(self._normalize_book(item))
            return {"success": True, "results": results, "total": len(results)}
        except Exception as e:
            logger.exception(f"[{self.name}] script search error: {e}")
            return {"success": False, "error": f"脚本搜索异常: {e}"}

    def get_chapters(self, book_id: str) -> Dict[str, Any]:
        if not self._has_chapters or self._chapters_iface is None:
            return {"success": False, "error": "该脚本接口未配置 chapters 脚本"}
        try:
            search_item = self._search_cache.get(str(book_id), {})
            params = self._build_chapters_params(
                book_id, 1, 2000, search_item.get("count"), search_item
            )
            raw = self._chapters_iface.execute(params)
            self._chapter_cache.clear()
            tracks = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("chapter_id", ""))
                self._chapter_cache[(str(book_id), cid)] = item
                tracks.append(self._normalize_chapter(item))
            return {
                "success": True,
                "album_title": (search_item.get("bookTitle") or "") if search_item else "",
                "tracks": tracks,
                "track_total": len(tracks),
            }
        except Exception as e:
            logger.exception(f"[{self.name}] script chapters error: {e}")
            return {"success": False, "error": f"脚本章节异常: {e}"}

    def get_audio_url(self, book_id: str, chapter_id: str) -> Optional[str]:
        if not self._has_audio or self._audio_iface is None:
            return None
        try:
            chapter_item = self._chapter_cache.get((str(book_id), str(chapter_id)), {})
            params = self._build_audio_params(book_id, chapter_id, chapter_item)
            raw = self._audio_iface.execute(params)
            if isinstance(raw, dict):
                url = raw.get("url") or raw.get("audio_url")
                return url if isinstance(url, str) and url.strip() else None
            return raw if isinstance(raw, str) and raw.strip() else None
        except Exception as e:
            logger.exception(f"[{self.name}] script audio error: {e}")
            return None


# ════════════════════════════════════════════
#  接口管理器（单例，带缓存）
# ════════════════════════════════════════════

class InterfaceManager:
    def __init__(self):
        self._adapters: Dict[str, BaseInterfaceAdapter] = {}
        self._lock = threading.RLock()

    def _build_adapter(self, row) -> Optional[BaseInterfaceAdapter]:
        try:
            cfg = {}
            if row.config:
                try:
                    cfg = json.loads(row.config)
                except Exception:
                    cfg = {}
            if row.type == TYPE_OFFICIAL:
                return OfficialAdapter(row.name)
            elif row.type == TYPE_SCRIPT:
                return ScriptAdapter(row.name, cfg)
            else:
                logger.warning(f"未知接口类型: {row.type}")
                return None
        except Exception as e:
            logger.exception(f"构建适配器失败 {row.name}: {e}")
            return None

    @staticmethod
    def _shutdown_adapter(adapter) -> None:
        """释放适配器占用的外部资源（目前仅 ScriptAdapter 的常驻 worker 池）"""
        if adapter is not None and hasattr(adapter, "shutdown"):
            try:
                adapter.shutdown()
            except Exception:
                logger.exception(f"释放接口适配器资源失败: {getattr(adapter, 'name', '?')}")

    def reload_all(self):
        from db.session import SessionLocal
        from db.models import Interface
        db = SessionLocal()
        try:
            rows = db.query(Interface).order_by(Interface.priority, Interface.name).all()
            with self._lock:
                old = list(self._adapters.values())
                self._adapters.clear()
                for row in rows:
                    if not row.enabled:
                        continue
                    adapter = self._build_adapter(row)
                    if adapter:
                        self._adapters[row.name] = adapter
            for a in old:
                self._shutdown_adapter(a)
            logger.info(f"接口管理器已加载 {len(self._adapters)} 个接口")
        finally:
            db.close()

    def reload(self, name: Optional[str] = None):
        if name is None:
            self.reload_all()
            return
        from db.session import SessionLocal
        from db.models import Interface
        db = SessionLocal()
        try:
            row = db.query(Interface).filter_by(name=name).first()
            with self._lock:
                old = self._adapters.pop(name, None)
                if row and row.enabled:
                    adapter = self._build_adapter(row)
                    if adapter:
                        self._adapters[name] = adapter
            self._shutdown_adapter(old)
        finally:
            db.close()

    def get_adapter(self, name: str) -> Optional[BaseInterfaceAdapter]:
        with self._lock:
            if name in self._adapters:
                return self._adapters[name]
        # 懒加载：首次访问若为空则全量加载
        with self._lock:
            if not self._adapters:
                self.reload_all()
                return self._adapters.get(name)
        return None

    def get_adapter_any(self, name: str) -> Optional[BaseInterfaceAdapter]:
        """忽略启用状态，直接从数据库构建适配器（用于测试/编辑预览）"""
        from db.session import SessionLocal
        from db.models import Interface
        db = SessionLocal()
        try:
            row = db.query(Interface).filter_by(name=name).first()
            if not row:
                return None
            return self._build_adapter(row)
        finally:
            db.close()

    def list_interfaces(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        from db.session import SessionLocal
        from db.models import Interface
        db = SessionLocal()
        try:
            q = db.query(Interface).order_by(Interface.priority, Interface.name)
            rows = q.all()
            result = []
            for row in rows:
                if enabled_only and not row.enabled:
                    continue
                cfg = {}
                if row.config:
                    try:
                        cfg = json.loads(row.config)
                    except Exception:
                        cfg = {}
                result.append({
                    "name": row.name,
                    "display_name": row.display_name,
                    "type": row.type,
                    "enabled": bool(row.enabled),
                    "builtin": bool(row.builtin),
                    "priority": row.priority,
                    "description": row.description or "",
                    "config": cfg,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                })
            return result
        finally:
            db.close()

    def register(self, name: str, adapter: BaseInterfaceAdapter):
        with self._lock:
            self._adapters[name] = adapter

    def unregister(self, name: str):
        with self._lock:
            self._adapters.pop(name, None)


# 全局单例
manager = InterfaceManager()
