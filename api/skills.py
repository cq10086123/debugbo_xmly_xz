import asyncio
import logging
from typing import Optional
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import (
    get_current_card,
    get_current_card_skill,
    get_current_card_download,
    ensure_interface_allowed,
)
from api.search import _do_search
from api.download import (
    start_batch_download, 
    BatchDownloadRequest, 
    get_batch_status, 
    retry_batch_failed,
    _make_downloader,
    _batch_tasks
)
from core import config as _config
from core import cover as cover_mod
from core.cover import extract_cover
from core.account_manager import list_accounts
from api.extension import (list_local_tasks, create_local_task, CreateTaskRequest,
                       _release_slot)

logger = logging.getLogger(__name__)

# 封面代理的安全上限
_COVER_MAX_REDIRECTS = 3
_COVER_MAX_BYTES = 8 * 1024 * 1024  # 8MB，封面图远小于此


class _CoverFetchError(Exception):
    """封面抓取被安全策略拒绝（重定向越界 / 目标不被允许）。"""


router = APIRouter(prefix="/api/skills", tags=["AI_Skills"])

class SearchRequest(BaseModel):
    keyword: str = Field(..., description="用户想要搜索的书籍名称或作者名称")
    source: str = Field("official", description="使用的搜索接口名称。如果不指定，默认为 'official' (官方接口)。可以通过 get_sources 接口获取可用的 source 列表。")

class AlbumIdRequest(BaseModel):
    album_id: str = Field(..., description="书籍（专辑）的唯一 ID (可能是整数也可能是字符串)")
    source: str = Field("official", description="获取该书籍使用的接口名称。必须与搜索时使用的 source 保持一致！")

class DownloadSubmitRequest(BaseModel):
    album_id: str = Field(..., description="书籍（专辑）的唯一 ID")
    start_episode: int = Field(1, description="起始集数（如果不指定默认从 1 开始）")
    end_episode: Optional[int] = Field(None, description="结束集数（如果不指定则默认下载到最后一集）")
    fmt: str = Field("mp3", description="下载格式，通常为 mp3 或 m4a")
    source: str = Field("official", description="执行下载的接口名称。必须与搜索时使用的 source 保持一致！")

class TaskIdRequest(BaseModel):
    task_id: str = Field(..., description="下载任务的唯一 task_id")

class ShowCoversRequest(BaseModel):
    count: int = Field(3, description="要显示前几本书的封面。例如用户说「看第一本的封面」传 1，「前三本」传 3。不传默认 3。")
    index: Optional[int] = Field(None, description="只看某一本时传它的序号（从 1 开始）。例如「第 2 本的封面」传 2。传了 index 时忽略 count。")

class BookNameRequest(BaseModel):
    book_name: str = Field(..., description="已下载书籍的名称（专辑目录名，支持模糊匹配）")

class QuarkJobIdRequest(BaseModel):
    job_id: str = Field("", description="sync_book_to_quark 返回的 job_id；不传则查询最近任务")


@router.post("/get_sources", summary="获取所有可用的音源接口", description="返回系统中当前可用的音源接口列表。大模型在搜索书籍前，可以先调用此接口让用户选择使用哪个音源，或者直接列出给用户看。")
async def skill_get_sources(auth: dict = Depends(get_current_card_skill)):
    from api.interfaces import public_interface_list
    return await public_interface_list(auth)

@router.post("/search_books", summary="搜索书籍", description="当用户想听某本书但不知道 album_id 时调用此接口。可以指定 source（默认 official）。返回相关书籍列表与对应的 album_id。返回中的 has_cover 表示该书有封面图可看；若用户想通过封面辨认是哪一本（书名重复时很常见），再调用 show_covers 显示图片。")
async def skill_search_books(req: SearchRequest, auth: dict = Depends(get_current_card_skill)):
    if req.source == "official":
        ensure_interface_allowed(auth, "official")
        data = await asyncio.to_thread(_do_search, req.keyword, 1)
        if data.get("ret") != 200:
            return {"success": False, "error": data.get("msg", "搜索失败")}
        
        docs = data.get("data", {}).get("result", {}).get("response", {}).get("docs", [])
        simplified = []
        for doc in docs[:10]:
            aid = doc.get("id", doc.get("albumId"))
            simplified.append({
                "source": "official",
                "album_id": aid,
                "title": doc.get("title"),
                "author": doc.get("nickname"),
                "intro": doc.get("intro", "")[:100],
                "tracks_count": doc.get("tracks"),
                "is_finished": doc.get("isFinished") == 2,
                "_cover": extract_cover(doc),
            })
        return _with_cover_meta(simplified, auth["card_id"], req.keyword, "official")
    else:
        from api.interfaces import intf_search
        resp = await intf_search(req.source, req.keyword, 1, auth=auth)
        if not resp.get("success"):
            return resp
        
        docs = resp.get("results", resp.get("list", []))
        simplified = []
        for doc in docs[:10]:
            simplified.append({
                "source": req.source,
                "album_id": doc.get("id", doc.get("albumId")),
                "title": doc.get("title"),
                "author": doc.get("author"),
                "intro": doc.get("intro", "")[:100],
                "tracks_count": doc.get("trackCount", doc.get("tracks", doc.get("count", "未知"))),
                # 适配器的 _normalize_book 已用通用提取器统一产出 cover；
                # 这里只做兜底（万一某适配器未经归一化直接返回原始结构）。
                "_cover": doc.get("cover") or extract_cover(doc),
            })
        return _with_cover_meta(simplified, auth["card_id"], req.keyword, req.source)


# 最近一次搜索结果的封面缓存：card_id -> {keyword, source, items:[{title, cover}]}
# 只存封面 URL 与书名，供后续「显示第 N 本封面」按序号取用，无需重新搜索。
_last_search: dict[int, dict] = {}
_LAST_SEARCH_MAX_CARDS = 500


def _with_cover_meta(items: list[dict], card_id: int, keyword: str, source: str) -> dict:
    """把内部 _cover 字段转成对 AI 友好的 has_cover 标记，并缓存供 show_covers 使用。"""
    if len(_last_search) > _LAST_SEARCH_MAX_CARDS:
        _last_search.clear()
    _last_search[card_id] = {
        "keyword": keyword,
        "source": source,
        "items": [{"title": it.get("title"), "album_id": it.get("album_id"),
                   "cover": it.get("_cover", "")} for it in items],
    }
    results = []
    for idx, it in enumerate(items, start=1):
        row = {k: v for k, v in it.items() if k != "_cover"}
        row["index"] = idx
        row["has_cover"] = bool(it.get("_cover"))
        results.append(row)

    with_cover = sum(1 for r in results if r["has_cover"])
    out = {"success": True, "results": results}
    if with_cover:
        out["suggestion"] = (
            f"共 {len(results)} 条结果，其中 {with_cover} 条有封面。"
            "若书名重复、用户难以分辨是哪一本，可以主动提示「需要我把封面显示出来吗」；"
            "用户确认后调用 show_covers（count=前几本，或 index=指定第几本）。"
        )
    return out


@router.post("/show_covers", summary="显示搜索结果的书籍封面", description="在 search_books 之后调用，把结果里的书籍封面作为图片显示给用户。书名重复时用来辨认是哪一本。用户说「显示第一本的封面」传 index=1；说「前三本封面」传 count=3。适用于所有音源接口。")
async def skill_show_covers(req: ShowCoversRequest = ShowCoversRequest(), auth: dict = Depends(get_current_card_skill)):
    cached = _last_search.get(auth["card_id"])
    if not cached or not cached.get("items"):
        return {"success": False, "error": "还没有搜索记录",
                "suggestion": "请先调用 search_books 搜索书籍，然后再显示封面。"}

    items = cached["items"]
    if req.index is not None:
        if req.index < 1 or req.index > len(items):
            return {"success": False,
                    "error": f"序号 {req.index} 超出范围，本次搜索共 {len(items)} 条结果。"}
        chosen = [(req.index, items[req.index - 1])]
    else:
        count = max(1, min(int(req.count or 3), len(items)))
        chosen = list(enumerate(items[:count], start=1))

    covers, missing = [], []
    for idx, it in chosen:
        if it.get("cover"):
            covers.append({
                "index": idx,
                "title": it.get("title"),
                "album_id": it.get("album_id"),
                # 签名代理 URL：AI 渲染器无凭证也能取图，且绕开上游防盗链
                "image_url": cover_mod.sign(it["cover"]),
            })
        else:
            missing.append({"index": idx, "title": it.get("title")})

    if not covers:
        return {"success": False, "error": "所选书籍均无封面图",
                "missing": missing,
                "suggestion": "该音源未提供封面，请改用书名、主播与集数帮用户区分。"}

    return {
        "success": True,
        "keyword": cached.get("keyword"),
        "source": cached.get("source"),
        "covers": covers,
        "missing": missing,
        "suggestion": (
            "请用 Markdown 图片语法把每本书的封面显示出来，例如 "
            "![书名](image_url)，并在图片旁标注序号与书名，方便用户指认要下载哪一本。"
        ),
    }


@router.get("/cover", summary="封面图片代理（签名访问）", description="由 show_covers 返回的签名 URL 指向此处，供 AI 客户端直接加载图片，无需鉴权头。")
async def skill_cover_proxy(u: str = "", e: str = "", s: str = ""):
    from fastapi.responses import Response

    target = cover_mod.verify(u, e, s)
    if not target:
        raise HTTPException(status_code=403, detail="封面链接无效或已过期")
    if not cover_mod.is_safe_fetch_target(target):
        raise HTTPException(status_code=400, detail="封面地址不被允许")

    def _fetch():
        """手动跟随重定向，并对每一跳都做 SSRF 校验。

        requests 默认 allow_redirects=True，若上游用 302 指向 169.254.169.254
        等内网地址，只校验原始 URL 会被绕过，故此处逐跳校验。
        """
        import requests
        url = target
        headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
                   # 带上 Referer 以绕过上游防盗链
                   "Referer": "https://www.ximalaya.com/"}
        for _ in range(_COVER_MAX_REDIRECTS):
            resp = requests.get(url, timeout=10, verify=False, headers=headers,
                                allow_redirects=False, stream=True)
            if resp.status_code in (301, 302, 303, 307, 308):
                nxt = resp.headers.get("Location") or ""
                resp.close()
                if not nxt:
                    raise _CoverFetchError("重定向缺少 Location")
                url = urljoin(url, nxt)
                if not cover_mod.is_safe_fetch_target(url):
                    raise _CoverFetchError("重定向目标不被允许")
                continue
            return resp
        raise _CoverFetchError("重定向次数过多")

    try:
        resp = await asyncio.to_thread(_fetch)
    except _CoverFetchError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"封面代理失败 {target}: {ex}")
        raise HTTPException(status_code=502, detail="封面获取失败")

    try:
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"封面获取失败（{resp.status_code}）")
        ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            raise HTTPException(status_code=502, detail="上游返回的不是图片")
        # 限制响应体大小，避免上游返回超大文件耗尽内存
        body = b""
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > _COVER_MAX_BYTES:
                raise HTTPException(status_code=502, detail="封面文件过大")
        return Response(content=body, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"封面代理失败 {target}: {ex}")
        raise HTTPException(status_code=502, detail="封面获取失败")
    finally:
        resp.close()


@router.post("/get_chapters", summary="获取书籍章节概况", description="在用户要下载前，获取此书籍共有多少集。必须传入搜索时获得的 source 和 album_id。")
async def skill_get_chapters(req: AlbumIdRequest, auth: dict = Depends(get_current_card_skill)):
    try:
        if req.source == "official":
            ensure_interface_allowed(auth, "official")
            dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
            result = await asyncio.to_thread(dl.get_track_list, int(req.album_id))
            if not result.get("success"):
                return result
            return {
                "success": True,
                "source": "official",
                "album_title": result.get("albumTitle"),
                "total_count": result.get("totalCount"),
                "suggestion": f"共找到 {result.get('totalCount')} 集，请询问用户需要下载哪几集（例如：1到10集）"
            }
        else:
            from api.interfaces import intf_album_list, AlbumListRequest as IntfAlbumListReq
            inner_req = IntfAlbumListReq(book_id=str(req.album_id))
            result = await intf_album_list(req.source, inner_req, auth=auth)
            if not result.get("success"):
                return result
            
            tracks = result.get("tracks", [])
            return {
                "success": True,
                "source": req.source,
                "total_count": len(tracks),
                "suggestion": f"共找到 {len(tracks)} 集，请询问用户需要下载哪几集（例如：1到10集）"
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/submit_download", summary="提交服务器下载任务", description="当用户确认书籍和章节范围后调用。如果成功会返回 task_id，务必将 task_id 记住以查询进度。")
async def skill_submit_download(req: DownloadSubmitRequest, auth: dict = Depends(get_current_card_skill)):
    try:
        if req.source == "official":
            inner_req = BatchDownloadRequest(
                album_id=int(req.album_id),
                start_episode=req.start_episode,
                end_episode=req.end_episode,
                fmt=req.fmt
            )
            return await start_batch_download(inner_req, auth=auth)
        else:
            from api.interfaces import intf_batch, BatchRequest as IntfBatchReq
            inner_req = IntfBatchReq(
                book_id=str(req.album_id),
                start_episode=req.start_episode,
                end_episode=req.end_episode,
                fmt=req.fmt,
                concurrency=3 
            )
            return await intf_batch(req.source, inner_req, auth=auth)
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/check_task_status", summary="查询下载任务进度", description="使用 submit_download 返回的 task_id 查询实时进度、失败情况。自动覆盖官方接口、第三方接口与浏览器插件本地任务。")
async def skill_check_task_status(req: TaskIdRequest, auth: dict = Depends(get_current_card_skill)):
    """按 task_id 查询进度——依次穿透四个任务登记处。

    历史 bug：本接口只查 `_batch_tasks`（官方引擎内存字典），而第三方接口的批量任务
    登记在 `api.interfaces._intf_tasks`、插件任务在 `local_tasks` 表，
    于是「第三方任务明明在跑，查询却返回任务不存在」。
    查询顺序：官方内存 → 第三方内存 → 本地插件表 → download_tasks 表（重启后兜底）。
    """
    task_id = (req.task_id or "").strip()
    if not task_id:
        return {"success": False, "error": "task_id 不能为空"}

    summary: Optional[dict] = None

    # ① 官方引擎（内存实时状态）
    status_resp = await get_batch_status(task_id, auth=auth)
    if status_resp.get("success"):
        summary = {
            "task_id": task_id,
            "source": status_resp.get("interface_name", "official"),
            "task_type": "server",
            "album_title": status_resp.get("album_title", ""),
            "status": status_resp.get("status"),
            "total": status_resp.get("total"),
            "completed": status_resp.get("completed"),
            "skipped": status_resp.get("skipped_count", 0),
            "percent": status_resp.get("percent"),
            "current_title": status_resp.get("current_title", ""),
            "eta_text": status_resp.get("eta_text", ""),
            "failed_count": len(status_resp.get("failed_list", [])),
            "errors": status_resp.get("failed_list", [])[:5],
            "error_msg": status_resp.get("error", ""),
        }

    # ② 第三方接口批量任务（内存实时状态）
    if summary is None:
        from api.interfaces import get_intf_task
        intf_resp = await get_intf_task(task_id, auth=auth)
        if intf_resp.get("success"):
            summary = {
                "task_id": task_id,
                "source": intf_resp.get("interface", ""),
                "task_type": "server",
                "album_title": intf_resp.get("album_title", ""),
                "status": intf_resp.get("status"),
                "total": intf_resp.get("total"),
                "completed": intf_resp.get("completed"),
                "skipped": intf_resp.get("skipped_count", 0),
                "percent": intf_resp.get("percent"),
                "current_title": intf_resp.get("current_title", ""),
                "failed_count": len(intf_resp.get("failed_list", [])),
                "errors": intf_resp.get("failed_list", [])[:5],
                "error_msg": intf_resp.get("error", "") or intf_resp.get("last_error", ""),
            }

    # ③ 浏览器插件本地任务（local_tasks 表）
    if summary is None:
        summary = _local_task_summary(task_id, auth["card_id"])

    # ④ download_tasks 表兜底：服务重启后内存字典清空，但数据库仍有该行
    if summary is None:
        from api.persistence import get_task_row
        row = get_task_row(task_id, auth["card_id"])
        if row:
            summary = {
                "task_id": task_id,
                "source": row.get("interface_name", "official"),
                "task_type": "server",
                "album_title": row.get("album_title", ""),
                "status": row.get("status"),
                "total": row.get("total"),
                "completed": row.get("completed"),
                "skipped": row.get("skipped_count", 0),
                "percent": row.get("percent"),
                "current_title": row.get("current_title", ""),
                "failed_count": row.get("failed_count", 0),
                "errors": row.get("failed_list", [])[:5],
                "error_msg": row.get("error", ""),
                "from_db": True,
            }

    if summary is None:
        return _task_not_found(task_id, auth["card_id"])

    summary["success"] = True
    status = summary.get("status") or ""
    failed_count = summary.get("failed_count") or 0

    if status in ("running", "pending", "cancelling"):
        if status == "pending":
            summary["suggestion"] = "任务在排队等待浏览器插件接管，请提醒用户打开浏览器并启用插件。"
        elif status == "cancelling":
            summary["suggestion"] = "任务正在取消中，稍后可再次查询确认已停止。"
        else:
            summary["suggestion"] = "任务正在下载中，请告诉用户当前进度，稍后可再次查询。"
    elif status == "done":
        summary["suggestion"] = (
            f"下载已完成，但有 {failed_count} 集失败，可以提示用户使用 retry_task 重试。"
            if failed_count else "下载已完成！"
        )
    elif status == "interrupted":
        summary["suggestion"] = "任务因服务器重启而中断，可提示用户在网页任务列表点击「继续」恢复下载。"
    elif status in ("error", "failed") or failed_count > 0:
        summary["suggestion"] = "有部分文件下载失败或任务出错，可以提示用户使用 retry_task 接口重试。"
    elif status == "cancelled":
        summary["suggestion"] = "任务已被取消。"

    return summary


@router.post("/list_active_downloads", summary="列出当前所有正在进行的下载", description="不需要 task_id。汇总当前卡密下所有进行中的下载（官方接口、第三方接口、浏览器插件本地下载）。当用户问「现在下载到哪了」但你没有 task_id、或 check_task_status 返回任务不存在时，优先调用此接口。")
async def skill_list_active_downloads(auth: dict = Depends(get_current_card_skill)):
    from api.download import list_batch_tasks
    from api.interfaces import list_intf_tasks
    from core import download_slot

    active_states = ("running", "pending", "cancelling")
    tasks: list[dict] = []

    # 官方引擎
    try:
        off = await list_batch_tasks(auth=auth)
        for t in off.get("tasks", []):
            if t.get("status") in active_states:
                tasks.append({
                    "task_id": t.get("task_id"), "task_type": "server",
                    "source": t.get("interface_name", "official"),
                    "album_title": t.get("album_title", ""), "status": t.get("status"),
                    "total": t.get("total"), "completed": t.get("completed"),
                    "percent": t.get("percent"), "eta_text": t.get("eta_text", ""),
                    "current_title": t.get("current_title", ""),
                    "failed_count": t.get("failed_count", 0),
                })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"列出官方任务失败: {e}")

    # 第三方接口
    try:
        intf = await list_intf_tasks(auth=auth)
        for t in intf.get("tasks", []):
            if t.get("status") in active_states:
                tasks.append({
                    "task_id": t.get("task_id"), "task_type": "server",
                    "source": t.get("interface", ""),
                    "album_title": t.get("album_title", ""), "status": t.get("status"),
                    "total": t.get("total"), "completed": t.get("completed"),
                    "percent": t.get("percent"),
                    "current_title": t.get("current_title", ""),
                    "failed_count": t.get("failed_count", 0),
                })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"列出第三方任务失败: {e}")

    # 浏览器插件本地任务
    try:
        loc = await list_local_tasks(auth=auth)
        for t in loc.get("tasks", []):
            s = _local_task_summary(t.get("task_id"), auth["card_id"])
            if s and s.get("status") in active_states:
                tasks.append({
                    "task_id": s["task_id"], "task_type": "local", "source": s["source"],
                    "album_title": s["album_title"], "status": s["status"],
                    "total": s["total"], "completed": s["completed"],
                    "percent": s["percent"], "failed_count": s["failed_count"],
                })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"列出本地任务失败: {e}")

    if tasks:
        return {
            "success": True, "active_count": len(tasks), "tasks": tasks,
            "suggestion": "把上面每个任务的书名与进度百分比告诉用户；后续可用对应 task_id 调用 check_task_status 追踪。",
        }

    # 一个都没有，但下载槽仍被占用 → 状态不一致，如实说明
    try:
        holder = download_slot.get_lock(auth["card_id"])
    except Exception:  # noqa: BLE001
        holder = None
    if holder and not holder.get("expired"):
        return {
            "success": True, "active_count": 0, "tasks": [],
            "slot_holder": holder,
            "suggestion": (
                f"任务列表为空，但下载槽仍被 task_id={holder.get('task_id')}"
                f"（来源 {holder.get('source')}）占用且心跳正常。"
                "这通常意味着服务重启过、进程内任务状态已丢失。"
                "请提示用户到网页任务列表查看，或稍等租约过期后重新提交。"
            ),
        }
    return {"success": True, "active_count": 0, "tasks": [],
            "message": "当前没有正在进行的下载任务。"}


def _parse_local_progress(progress, tracks, failed_list) -> dict:
    """统一解析插件本地任务的进度。

    插件心跳写入的键是 {total, done, failed}（见 extension/background.js），
    历史数据可能是 {completed, skipped}，两者都兼容。
    check_task_status 与 check_local_tasks 共用此函数，避免两处各写一份口径。
    """
    prog = progress if isinstance(progress, dict) else {}

    def _int(value, default=0):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    total = _int(prog.get("total")) or len(tracks or []) or 0
    completed = _int(prog.get("done", prog.get("completed", 0)))
    skipped = _int(prog.get("skipped", 0))
    done = completed + skipped
    # 插件偶发多报（重试重复计数）时把百分比钳在 100，避免出现 180% 这种数字
    percent = min(100, round(done / total * 100)) if total else 0
    return {
        "total": total,
        "completed": completed,
        "skipped": skipped,
        "percent": percent,
        "failed_count": _int(prog.get("failed", len(failed_list or []))),
    }


def _local_task_summary(task_id: str, card_id: int) -> Optional[dict]:
    """从 local_tasks 表读取插件任务进度（插件心跳写入 progress={total,done,failed}）。"""
    import json as _json
    from db.session import SessionLocal
    from db.models import LocalTask

    def _load(raw, fallback):
        """脏 JSON 不能让「任务存在」变成「任务不存在」，降级为默认值即可。"""
        if not raw:
            return fallback
        try:
            return _json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(f"本地任务 {task_id} 的字段不是合法 JSON，已降级处理")
            return fallback

    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t:
            return None
        failed_list = _load(t.failed_list, [])
        if not isinstance(failed_list, list):
            failed_list = []
        prog = _parse_local_progress(
            _load(t.progress, {}),
            _load(t.tracks, []),
            failed_list,
        )
        return {
            "task_id": task_id,
            "source": t.source,
            "task_type": "local",
            "album_title": t.album_title or "",
            "status": t.status,
            "errors": failed_list[:5],
            "error_msg": t.error or "",
            **prog,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取本地任务 {task_id} 失败: {e}")
        return None
    finally:
        db.close()


def _task_not_found(task_id: str, card_id: int) -> dict:
    """任务四处皆无：结合下载槽给出可执行的诊断，而不是干巴巴一句「任务不存在」。"""
    from core import download_slot

    hint = (
        "已查询官方任务、第三方接口任务、浏览器插件任务与历史任务记录，均无此 task_id。"
        "请确认 task_id 是否抄写正确、是否属于当前卡密。"
    )
    try:
        holder = download_slot.get_lock(card_id)
    except Exception:  # noqa: BLE001
        holder = None
    if holder and not holder.get("expired"):
        hint += (
            f" 注意：该卡密当前确实有下载在进行中——"
            f"task_id={holder.get('task_id')}、来源={holder.get('source')}、"
            f"类型={'浏览器插件' if holder.get('holder_type') == 'local' else '服务器'}"
            f"《{holder.get('album_title') or '未知'}》。"
            f"请改用这个 task_id 查询进度。"
        )
    return {"success": False, "error": "任务不存在", "task_id": task_id, "suggestion": hint}

@router.post("/retry_task", summary="重试失败的下载任务", description="当 check_task_status 显示有失败项时，调用此接口触发重试。目前仅官方接口任务支持自动重试。")
async def skill_retry_task(req: TaskIdRequest, auth: dict = Depends(get_current_card_skill)):
    resp = await retry_batch_failed(req.task_id, auth=auth)
    if resp.get("success"):
        return resp

    # 官方任务里没找到 → 可能是第三方 / 本地插件任务，给出准确说明而不是「原任务不存在」
    from api.interfaces import get_intf_task
    intf_resp = await get_intf_task(req.task_id, auth=auth)
    if intf_resp.get("success"):
        return {
            "success": False,
            "error": "第三方接口任务暂不支持一键重试",
            "suggestion": (
                f"任务《{intf_resp.get('album_title') or req.task_id}》来自第三方接口 "
                f"{intf_resp.get('interface')}，请提示用户在网页任务列表中对该任务重新提交下载"
                "（已下载的集数会自动跳过，不会重复下载）。"
            ),
        }

    local = _local_task_summary(req.task_id, auth["card_id"])
    if local:
        return {
            "success": False,
            "error": "浏览器插件本地任务不支持此重试接口",
            "suggestion": "这是浏览器插件的本地下载任务。若卡死请改用 reset_stuck_local_task 重置。",
        }
    return resp


@router.post("/reset_stuck_local_task", summary="重置卡死的本地插件任务", description="当 check_local_tasks 发现有任务长时间卡在 running 状态进度不动，或者是由于浏览器崩溃导致的僵尸任务时，调用此接口将其重置为 pending，让插件能重新接管下载。")
def skill_reset_stuck_local_task(req: TaskIdRequest, auth: dict = Depends(get_current_card_skill)):
    from db.session import SessionLocal
    from db.models import LocalTask
    
    db = SessionLocal()
    try:
        task = db.query(LocalTask).filter_by(task_id=req.task_id, card_id=auth["card_id"]).first()
        if not task:
            return {"success": False, "error": "任务不存在或不属于当前卡密"}
            
        if task.status != "running":
            return {"success": False, "message": f"任务当前状态为 {task.status}，无需重置。"}

        task.status = "pending"
        task.claim_id = None
        task.claim_session = None      # 「谁在下」的归属指纹，必须与 claim 一起清（否则 /tasks 仍显示旧持有者）
        task.claimed_at = None
        task.heartbeat_at = None
        task.lease_until = None
        db.commit()
        # ⚠️ 只改任务状态不够：全局下载槽（card_download_locks）此刻仍被旧 claim 持有且租约未过期，
        # 插件 30s 后重新 claim 会撞 409「当前卡密已有下载任务进行中」——原来那句「30 秒内重新接管」
        # 因此是空头承诺，用户会以为重置没生效。必须同时释放槽（与 sweep/管理员强制释放同一套语义）。
        released = _release_slot(auth["card_id"], req.task_id)
        return {
            "success": True,
            "slot_released": released,
            "message": ("已成功将僵尸任务打回排队池并释放下载槽。请提示用户打开浏览器并确保插件开启，"
                        "插件会在 30 秒内重新接管并继续下载。" if released else
                        "任务已打回排队池，但下载槽暂时没释放成功：它最长会在一个租约周期（默认 300 秒）"
                        "后自动回收，届时插件自动接管；如需立刻恢复请在后台点「强制释放下载槽」。"),
        }
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()

@router.post("/get_card_info", summary="获取卡密状态信息", description="获取当前使用的卡密的剩余时间、有效状态等基本信息。")
def skill_get_card_info(auth: dict = Depends(get_current_card_skill)):
    from db.session import SessionLocal
    from db.models import Card
    import time
    
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=auth["card_id"]).first()
        if not card:
            return {"success": False, "error": "卡密不存在"}
        
        is_expired = False
        expire_time_str = "永久有效"
        if card.expire_time:
            is_expired = time.time() > card.expire_time
            from datetime import datetime
            expire_time_str = datetime.fromtimestamp(card.expire_time).strftime('%Y-%m-%d %H:%M:%S')
            
        return {
            "success": True,
            "card_code": card.code,
            "is_expired": is_expired,
            "expire_time": expire_time_str,
            "download_mode": card.download_mode or "both"
        }
    finally:
        db.close()

@router.post("/check_accounts", summary="巡检官方账号状态", description="查询当前卡密名下是否绑定了喜马拉雅账号，以及账号是否失效。")
def skill_check_accounts(auth: dict = Depends(get_current_card_skill)):
    accounts = list_accounts(auth["card_id"])
    if not accounts:
        return {
            "success": True, 
            "accounts_count": 0, 
            "suggestion": "当前卡密未绑定任何喜马拉雅官方账号。官方接口下载VIP音频需要有账号支持，请提醒用户去网页版扫码登录。"
        }
    
    return {
        "success": True,
        "accounts_count": len(accounts),
        "accounts_list": [{"nickname": acc.get("nickname"), "is_vip": acc.get("is_vip")} for acc in accounts],
        "suggestion": "账号状态正常。如果有下载VIP音频失败（如签名错误），可能是账号过期或风控，可提醒用户重新扫码。"
    }

from fastapi import Request
from fastapi.responses import StreamingResponse
import io
import zipfile
import json

@router.post("/rotate_token", summary="作废并重签 SKILL 专用凭证", description="网页登录后调用。旧的 OpenAPI 配置立即失效，需重新下载并导入 AI 平台。网页会话不受影响。")
async def rotate_skill_token(auth: dict = Depends(get_current_card)):
    from db.session import SessionLocal
    from db.models import Card
    from api.card_helpers import rotate_card_skill_token

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=auth["card_id"]).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        rotate_card_skill_token(db, card)
        return {"success": True, "message": "已作废旧的 SKILL 配置，请重新下载并导入 AI 平台"}
    finally:
        db.close()


@router.get("/export_openapi", summary="导出 AI Skills 配置", description="需网页登录。下载专属 OpenAPI 规范，内置 SKILL 专用凭证（与网页登录无关）。")
async def export_skills_openapi(request: Request, auth: dict = Depends(get_current_card_download)):
    from db.session import SessionLocal
    from db.models import Card
    from api.card_helpers import ensure_card_skill_token

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=auth["card_id"]).first()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        skill_token = ensure_card_skill_token(db, card)
    finally:
        db.close()

    base_url = str(request.base_url).rstrip("/")
    
    openapi_schema = {
        "openapi": "3.1.0",
        "info": {
            "title": "喜马拉雅下载管家 Skills",
            "version": "1.0.0",
            "description": "专为 AI 助手设计的全流程音频下载控制接口。"
        },
        "servers": [
            {"url": base_url, "description": "您的服务器地址"}
        ],
        "paths": {
            "/api/skills/get_sources": {
                "post": {
                    "summary": "获取所有可用的音源接口",
                    "description": "返回系统中当前可用的音源接口列表。大模型在搜索书籍前，可以先调用此接口让用户选择使用哪个音源，或者直接列出给用户看。",
                    "operationId": "skill_get_sources",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/show_covers": {
                "post": {
                    "summary": "显示搜索结果的书籍封面",
                    "description": "在 search_books 之后调用，把结果里的书籍封面作为图片显示给用户，用于在书名重复时辨认是哪一本。用户说「显示第一本的封面」传 index=1；说「前三本封面」传 count=3。返回的 image_url 请用 Markdown 图片语法 ![书名](image_url) 渲染出来。适用于所有音源接口。",
                    "operationId": "skill_show_covers",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "count": {"type": "integer", "default": 3, "description": "显示前几本书的封面，默认 3"},
                                        "index": {"type": "integer", "description": "只看第几本（从 1 开始）；传了则忽略 count"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/search_books": {
                "post": {
                    "summary": "搜索书籍",
                    "description": "当用户想听某本书但不知道 album_id 时调用此接口。返回相关书籍列表与对应的 album_id。返回中的 has_cover 表示该书有封面可看；若书名重复导致用户难以分辨，可提示用户并调用 show_covers 显示封面图片。",
                    "operationId": "skill_search_books",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "keyword": {"type": "string", "description": "用户想要搜索的书籍名称或作者名称"},
                                        "source": {"type": "string", "default": "official", "description": "使用的搜索接口名称。如果不指定，默认为 official (官方接口)。"}
                                    },
                                    "required": ["keyword"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/get_chapters": {
                "post": {
                    "summary": "获取书籍章节概况",
                    "description": "在用户要下载前，获取此书籍共有多少集。",
                    "operationId": "skill_get_chapters",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "album_id": {"type": "string", "description": "书籍（专辑）的唯一 ID (可能是整数也可能是字符串)"},
                                        "source": {"type": "string", "default": "official", "description": "获取该书籍使用的接口名称。必须与搜索时使用的 source 保持一致！"}
                                    },
                                    "required": ["album_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/submit_download": {
                "post": {
                    "summary": "提交服务器下载任务",
                    "description": "当用户确认书籍和章节范围后调用。如果成功会返回 task_id，务必将 task_id 记住以查询进度。",
                    "operationId": "skill_submit_download",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "album_id": {"type": "string", "description": "书籍（专辑）的唯一 ID"},
                                        "source": {"type": "string", "default": "official", "description": "执行下载的接口名称。必须与搜索时使用的 source 保持一致！"},
                                        "start_episode": {"type": "integer", "default": 1, "description": "起始集数"},
                                        "end_episode": {"type": "integer", "description": "结束集数，如果不提供，代表下载到最新/最后一集"},
                                        "fmt": {"type": "string", "default": "mp3", "description": "格式"}
                                    },
                                    "required": ["album_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/list_active_downloads": {
                "post": {
                    "summary": "列出当前所有正在进行的下载",
                    "description": "不需要 task_id。汇总当前卡密下所有进行中的下载（官方接口、第三方接口、浏览器插件本地下载）。当用户问「现在下载到哪了」但你没有 task_id、或 check_task_status 返回任务不存在时，优先调用此接口。",
                    "operationId": "skill_list_active_downloads",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_task_status": {
                "post": {
                    "summary": "查询下载任务进度",
                    "description": "使用 submit_download 返回的 task_id 查询实时进度、失败情况。自动覆盖官方接口、第三方接口与浏览器插件本地任务。若返回任务不存在，请改调用 list_active_downloads 查看当前所有进行中的下载。",
                    "operationId": "skill_check_task_status",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string", "description": "下载任务的唯一 task_id"}
                                    },
                                    "required": ["task_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/retry_task": {
                "post": {
                    "summary": "重试失败的下载任务",
                    "description": "当 check_task_status 显示有失败项时，调用此接口触发重试。目前仅官方接口任务支持自动重试；第三方接口任务需在网页重新提交。",
                    "operationId": "skill_retry_task",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string", "description": "下载任务的唯一 task_id"}
                                    },
                                    "required": ["task_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/reset_stuck_local_task": {
                "post": {
                    "summary": "重置卡死的本地插件任务",
                    "description": "当 check_local_tasks 发现有任务长时间卡在 running 状态进度不动，或者是由于浏览器崩溃导致的僵尸任务时，调用此接口将其重置为 pending，让插件能重新接管下载。",
                    "operationId": "skill_reset_stuck_local_task",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string", "description": "下载任务的唯一 task_id"}
                                    },
                                    "required": ["task_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_local_tasks": {
                "post": {
                    "summary": "查询浏览器插件本地下载状态",
                    "description": "查询当前卡密下有哪些书籍正在通过浏览器插件（本地电脑）下载，或者在排队等待下载，以及进度和报错信息。",
                    "operationId": "skill_check_local_tasks",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_accounts": {
                "post": {
                    "summary": "巡检官方账号状态",
                    "description": "查询当前卡密名下是否绑定了喜马拉雅账号，以及账号是否失效。",
                    "operationId": "skill_check_accounts",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/get_card_info": {
                "post": {
                    "summary": "获取卡密状态信息",
                    "description": "获取当前使用的卡密的剩余时间、有效状态等基本信息。",
                    "operationId": "skill_get_card_info",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/list_downloaded_books": {
                "post": {
                    "summary": "查看已完成下载的书籍",
                    "description": "列出当前卡密服务器本地下载目录里已经下完的书籍。用户说「看看下完了哪些书」时调用。",
                    "operationId": "skill_list_downloaded_books",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/sync_book_to_quark": {
                "post": {
                    "summary": "把指定已下载书籍同步到夸克网盘",
                    "description": "仅后台开通「夸克同步」的卡密可用。按书名（可模糊）复制到夸克挂载目录，成功后保留本地文件。",
                    "operationId": "skill_sync_book_to_quark",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "book_name": {"type": "string", "description": "已下载书籍名称，支持模糊匹配"}
                                    },
                                    "required": ["book_name"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_quark_sync": {
                "post": {
                    "summary": "查询夸克同步进度",
                    "description": "用 sync_book_to_quark 返回的 job_id 查询进度；不传 job_id 则返回最近任务。",
                    "operationId": "skill_check_quark_sync",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "job_id": {"type": "string", "description": "同步任务 id，可空"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            }
        }
    }

    auth_param = {
        "name": "Authorization",
        "in": "header",
        "description": "SKILL 专用 Token（与网页登录无关）",
        "required": True,
        "schema": {
            "type": "string",
            "default": f"Bearer {skill_token}"
        }
    }

    for path, path_item in openapi_schema["paths"].items():
        for method, operation in path_item.items():
            if "parameters" not in operation:
                operation["parameters"] = []
            operation["parameters"].append(auth_param)

    md_content = f"""# 喜马拉雅 AI 下载管家 - Skills 接入指南

本压缩包为您提供了专门为 LLM（大模型）定制的 OpenAPI 配置文件。

## 什么是这个 JSON 文件？
这个压缩包内的 `ai_skills_config.json` 是符合 OpenAPI 3.1 规范的 API 定义文件。它是传统大模型平台（如 Coze、Dify、FastGPT 等）最通用的“插件/工具”标准。

## 接入步骤 (以 Coze/扣子 为例)
1. 登录 Coze 工作台，进入“插件” -> “创建插件” -> 选择“导入”。
2. 将本压缩包内的 `ai_skills_config.json` 文件上传。
3. 平台会自动识别出全部 API 工具。
4. **⚠️ 重要安全特性：** 该 JSON 已自动内置 SKILL 专用凭证（`Bearer sk_…`），与网页登录/踢下线/换网络无关。卡密过期、被禁用，或在网页端「作废 SKILL 配置」后立即失效。泄露后请到网页使用说明页重新生成。

## 推荐的 AI 提示词 (Prompt)
您可以直接将以下文案复制到您 Bot 的“系统提示词”中：

```text
你是一个专业的喜马拉雅有声书下载管家。你的职责是通过自然语言帮助用户寻找、下载和诊断音频任务。
你拥有以下技能链，必须遵循以下工作流：

1. **绝对禁止向用户索要技术参数**：你必须自己去调接口查 `album_id`、`source`、`task_id`，或者利用自己的对话记忆。
2. **多意图指令解析（极度智能）**：如果用户一句话包含了“搜索词、指定接口、指定第几本、指定集数、指定下载方式”，你要**极其聪明地**在脑海中连续静默调用 API。
    - 例如用户说：“搜索完美世界，使用免密接口A。下载第二本 1到10集，使用服务器下载。”
    - 你的内部操作：静默调用 `skill_search_books` (传入 keyword="完美世界", source="免密接口A") -> 拿到结果中的第二本书的 `album_id` -> 静默调用 `skill_submit_download` (传入 album_id, start_episode=1, end_episode=10, source="免密接口A")。
3. **分步引导（当指令不全时）**：如果用户只说了“帮我搜完美世界”，你就只展示搜索结果，并默默记住所有结果的 `album_id` 和 `source`，然后问用户“要下载哪一本的哪几集？”。
4. **服务器下载与本地插件**：`skill_submit_download` 只能提交到服务器。如果用户明确要求“本地下载/浏览器下载”，请告诉用户：“抱歉，目前通过微信AI只能触发服务器下载。如果要使用本地下载，请打开您的电脑浏览器前往网页端操作。”
5. **状态与修复**：如果用户问进度，调用 `skill_check_task_status`；如果遇到 running 卡死，主动提议调用 `skill_reset_stuck_local_task`。
6. **夸克同步（仅开通该功能的卡密）**：用户说「看看下完了哪些书」时调用 `skill_list_downloaded_books`；说「把某某书同步到夸克」时调用 `skill_sync_book_to_quark`（传入书名，可模糊匹配，多本则列出候选让用户选），然后告诉用户已开始同步，再用 `skill_check_quark_sync` 查进度。未开通会返回错误，如实转告。下载完成不要自动同步。
"""

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        zip_file.writestr("ai_skills_config.json", json.dumps(openapi_schema, indent=2, ensure_ascii=False))
        zip_file.writestr("Skills_接入说明.md", md_content)

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ai_skills_config_{auth["code"][-4:]}.zip"'}
    )

@router.post("/list_downloaded_books", summary="查看已完成下载的书籍", description="列出当前卡密服务器本地下载目录里已经下完的书籍（书名、集数、体积）。用户说「看看下完了哪些书」时调用。")
async def skill_list_downloaded_books(auth: dict = Depends(get_current_card_skill)):
    from core.quark_sync import scan_card_albums
    albums = scan_card_albums(auth["code"])
    if not albums:
        return {"success": True, "books": [], "suggestion": "当前卡密下还没有已完成的服务器下载书籍。"}
    return {
        "success": True,
        "books": albums,
        "suggestion": "需要同步到夸克时，调用 sync_book_to_quark 并传入书名；未开通夸克同步的卡密会被拒绝。",
    }


@router.post("/sync_book_to_quark", summary="把指定已下载书籍同步到夸克网盘", description="仅后台开通「夸克同步」的卡密可用。按书名（可模糊）把服务器本地音频复制到夸克挂载目录 yousheng/{书名}/，成功后保留本地文件。立即返回 job_id，请再用 check_quark_sync 查进度。")
async def skill_sync_book_to_quark(req: BookNameRequest, auth: dict = Depends(get_current_card_skill)):
    from api.deps import ensure_quark_sync_allowed
    from core import quark_sync as qs
    try:
        ensure_quark_sync_allowed(auth)
    except HTTPException as e:
        return {"success": False, "error": e.detail}
    albums = qs.scan_card_albums(auth["code"])
    if not albums:
        return {"success": False, "error": "当前没有已下载完成的书籍。"}
    hit, candidates = qs.match_album(albums, req.book_name)
    if not hit:
        names = [a["name"] for a in candidates[:15]]
        return {
            "success": False,
            "error": "无法唯一确定要同步的书，请让用户从下列书名中选一本。",
            "candidates": names,
        }
    try:
        job = qs.start_sync(auth["card_id"], auth["code"], hit["name"])
        return {
            "success": True,
            **job,
            "suggestion": (
                f"已开始把《{hit['name']}》同步到夸克，请把「已开始同步」告诉用户，"
                f"稍后用 check_quark_sync 查询 job_id={job['job_id']}。本地文件会保留。"
            ),
        }
    except qs.QuarkSyncError as e:
        return {"success": False, "error": str(e)}


@router.post("/check_quark_sync", summary="查询夸克同步进度", description="用 sync_book_to_quark 返回的 job_id 查询复制进度；不传 job_id 则返回该卡密最近的同步任务。")
async def skill_check_quark_sync(req: QuarkJobIdRequest = QuarkJobIdRequest(), auth: dict = Depends(get_current_card_skill)):
    from api.deps import ensure_quark_sync_allowed
    from core import quark_sync as qs
    try:
        ensure_quark_sync_allowed(auth)
    except HTTPException as e:
        return {"success": False, "error": e.detail}
    job_id = (req.job_id if req else "") or ""
    if job_id:
        job = qs.get_job(job_id, auth["card_id"])
        if not job:
            return {"success": False, "error": "同步任务不存在"}
        jobs = [job]
    else:
        running = qs.running_job_for_card(auth["card_id"])
        jobs = [running] if running else qs.list_jobs(auth["card_id"])[:3]
    if not jobs:
        return {"success": True, "message": "当前没有夸克同步任务。"}
    out = jobs[0]
    suggestion = "同步仍在进行，稍后可再查。"
    if out.get("status") == "done":
        suggestion = (
            f"《{out.get('album')}》已同步到夸克（复制 {out.get('copied')}，跳过 {out.get('skipped')}）。"
            "本地文件仍保留。"
        )
    elif out.get("status") == "failed":
        suggestion = f"同步失败：{out.get('error') or '未知错误'}。本地文件未删除，可以重试。"
    return {"success": True, "job": out, "suggestion": suggestion}


@router.post("/check_local_tasks", summary="查询浏览器插件本地下载状态", description="查询当前卡密下有哪些书籍正在通过浏览器插件（本地电脑）下载，或者在排队等待下载，以及进度和报错信息。")
async def skill_check_local_tasks(auth: dict = Depends(get_current_card_skill)):
    resp = await list_local_tasks(auth=auth)
    if not resp.get("success"):
        return resp

    tasks = resp.get("tasks", [])
    if not tasks:
        return {"success": True, "message": "目前没有本地插件下载任务在运行或排队。"}

    summary_list = []
    for t in tasks:
        failed_list = t.get("failed_list", [])
        # 与 check_task_status 共用同一套进度口径（插件写的是 done/failed）
        prog = _parse_local_progress(t.get("progress"), t.get("tracks"), failed_list)
        total = prog["total"]
        completed = prog["completed"]
        skipped = prog["skipped"]
        failed_count = prog["failed_count"]
        failed_details = []
        for f_item in failed_list[:3]:
            if isinstance(f_item, dict):
                failed_details.append(f"集数 {f_item.get('episode_num', '未知')}: {f_item.get('error', '未知错误')}")
            else:
                failed_details.append(str(f_item)[:100])
        
        summary_list.append({
            "task_id": t.get("task_id"),
            "album_title": t.get("album_title"),
            "source": t.get("source"),
            "status": t.get("status"), 
            "total_episodes": total,
            "completed": completed,
            "skipped": skipped,
            "percent": prog["percent"],
            "failed_count": failed_count,
            "failed_details": failed_details,
            "error_msg": t.get("error", "")
        })
        
    return {
        "success": True,
        "active_tasks_count": len(tasks),
        "tasks": summary_list,
        "suggestion": "如有 pending 任务，提醒用户打开浏览器；如果 running 任务进度长时间卡死，可能浏览器已崩溃，建议询问用户是否调用 reset_stuck_local_task 重置；如果 failed_details 包含签名错误/403，提示扫码续期。"
    }
