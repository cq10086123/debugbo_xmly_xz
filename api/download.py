"""官方引擎下载接口 — /api/download（卡密隔离版）

保留原有逻辑：Selenium 生成 xm-sign、AES 解密、VIP 账号限流自动切换 + 24h 冷却、
失败自动重试（按分钟轮次）、重试子任务回写父任务、断点恢复。
新增：所有任务按 card_id 隔离；下载目录按卡片号分区；任务/记录落 SQLite；
SSE 仅推送当前卡密的任务；全部接口要求 Bearer 鉴权。
"""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.downloader import XimalayaDownloader
from core import config as _config
from core import track_lock
from core import account_manager as _am
from api.deps import get_current_card, ensure_interface_allowed, ensure_download_mode_allowed
from api.persistence import persist_task, persist_record, load_tasks_from_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/download", tags=["下载（官方引擎）"])

# 启动从数据库恢复任务（标记 interrupted）
_batch_tasks: dict[str, dict] = load_tasks_from_db("official")
_sse_queues: set[asyncio.Queue] = set()


def reload_tasks():
    """init_db 之后调用：重新从数据库加载任务（恢复上次运行的任务）"""
    global _batch_tasks
    _batch_tasks = load_tasks_from_db("official")


def _make_downloader(account_id=None, card_id=None, download_root: Path | None = None) -> XimalayaDownloader:
    return XimalayaDownloader(account_id=account_id, card_id=card_id, download_root=download_root)


# 鉴权失败关键词：命中说明 cookie 失效/未登录，应终止任务提示重新登录，而非逐集重试
_AUTH_FAIL_KEYWORDS = ("未登录", "请登录", "登录后再", "登录后下载", "未授权", "请先登录", "账号失效", "登录已失效")


def _is_auth_failed(error_msg: str) -> bool:
    return any(kw in (error_msg or "") for kw in _AUTH_FAIL_KEYWORDS)


async def _broadcast_task_update():
    for q in list(_sse_queues):
        try:
            q.put_nowait(True)
        except asyncio.QueueFull:
            pass


# ── 请求模型 ──
class TrackRequest(BaseModel):
    track_id: int
    quality: int = 0
    album_title: str | None = None
    fmt: str = "mp3"
    episode_num: int | None = None


class ChapterRequest(BaseModel):
    album_id: int
    chapter_num: int
    quality: int = 0
    fmt: str = "mp3"


class AlbumListRequest(BaseModel):
    album_id: int


class BatchDownloadRequest(BaseModel):
    album_id: int
    quality: int = 0
    start_episode: int = 1
    end_episode: int | None = None
    fmt: str = "mp3"
    account_id: str | None = None


# ════════════════════════════════════════
#  单集 / 章节 / 列表
# ════════════════════════════════════════
@router.post("/track")
async def download_track(req: TrackRequest, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    try:
        dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
        result = await asyncio.to_thread(
            dl.download_by_track_id, req.track_id, req.quality,
            album_title=req.album_title, fmt=req.fmt, episode_num=req.episode_num
        )
        return result
    except Exception as e:
        logger.exception(f"{e}")
        return {"success": False, "error": str(e)}


@router.post("/chapter")
async def download_chapter(req: ChapterRequest, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    try:
        dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
        result = await asyncio.to_thread(
            dl.download_by_chapter, req.album_id, req.chapter_num, req.quality, fmt=req.fmt
        )
        return result
    except Exception as e:
        logger.exception(f"{e}")
        return {"success": False, "error": str(e)}


@router.post("/album-list")
async def get_album_list(req: AlbumListRequest, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    try:
        dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
        result = await asyncio.to_thread(dl.get_track_list, req.album_id)
        return result
    except Exception as e:
        logger.exception(f"{e}")
        return {"success": False, "error": str(e)}


# ════════════════════════════════════════
#  批量下载
# ════════════════════════════════════════
async def _run_batch_task(task_id: str, album_id: int, quality: int,
                          start_episode: int, end_episode: int, fmt: str = "mp3",
                          account_id: str | None = None):
    task = _batch_tasks[task_id]
    card_id = task["card_id"]
    download_root = task["download_root"]
    engine = "official"

    def _is_rate_limited(error_msg: str) -> bool:
        return any(kw in error_msg for kw in ("网络繁忙", "明天再试", "访问过于频繁", "请求过于频繁"))

    all_accounts = _am.list_accounts(card_id)
    active_ids = _am.get_active_account_ids(card_id)
    vip_ids = [a["id"] for a in all_accounts if a.get("is_vip") and a["id"] in active_ids]
    non_vip_ids = [a["id"] for a in all_accounts if not a.get("is_vip") and a["id"] in active_ids]

    if account_id:
        account_queue = [account_id] if account_id in active_ids else []
        for aid in vip_ids:
            if aid != account_id:
                account_queue.append(aid)
        for aid in non_vip_ids:
            if aid != account_id:
                account_queue.append(aid)
    else:
        # 优先 VIP，其次普通账号：免费专辑普通账号也能下，绝不能因为没有 VIP 就退回空 cookie
        account_queue = vip_ids + non_vip_ids
    if not account_queue:
        account_queue = [""]

    # 预检：没有任何可用账号时直接失败并给出清晰提示，避免空 cookie 下载刷一堆"未登录"
    if account_queue == [""]:
        if all_accounts:
            task["status"] = "failed"
            task["error"] = "所有喜马拉雅账号都处于限流冷却期（约24小时自动恢复），请稍后再试，或到「账号」页重新扫码登录"
        else:
            task["status"] = "failed"
            task["error"] = "卡密下没有任何已登录的喜马拉雅账号，请先在「账号」页扫码登录后再下载"
        await _broadcast_task_update()
        persist_task(task)
        return

    current_queue_idx = 0

    def _get_account_cookie_and_nickname(aid: str):
        if not aid:
            return None, "默认账号"
        acc = _am.get_account_info(card_id, aid)
        if not acc:
            return None, "未知账号"
        return acc.get("cookie_str", ""), acc.get("nickname", "未命名")

    def _switch_downloader():
        nonlocal current_queue_idx, dl
        old_aid = account_queue[current_queue_idx] if current_queue_idx < len(account_queue) else ""
        if old_aid and old_aid != "":
            _am.set_rate_limited(card_id, old_aid)
        if current_queue_idx + 1 >= len(account_queue):
            return False, None
        current_queue_idx += 1
        new_aid = account_queue[current_queue_idx]
        cookie, nickname = _get_account_cookie_and_nickname(new_aid)
        try:
            dl.close()
        except Exception:
            pass
        dl = XimalayaDownloader(account_id=new_aid if new_aid else None, card_id=card_id, download_root=download_root)
        task["account_id"] = new_aid or ""
        task["account_nickname"] = nickname
        return True, new_aid

    _, first_nick = _get_account_cookie_and_nickname(account_queue[0])
    task["account_id"] = account_queue[0] or ""
    task["account_nickname"] = first_nick

    try:
        dl = XimalayaDownloader(account_id=account_queue[0] if account_queue[0] else None,
                                card_id=card_id, download_root=download_root)
        try:
            list_result = await asyncio.to_thread(dl.get_track_list, album_id)
        except Exception as e:
            task["status"] = "failed"
            task["error"] = f"获取章节列表失败: {e}"
            await _broadcast_task_update()
            persist_task(task)
            return

        if not list_result["success"]:
            task["status"] = "failed"
            task["error"] = list_result.get("error", "获取章节列表失败")
            await _broadcast_task_update()
            persist_task(task)
            return

        album_title = list_result["album_title"]
        tracks = list_result["tracks"]
        total = len(tracks)
        start = max(1, start_episode)
        end = min(total, end_episode)
        task["album_title"] = album_title

        retry_episodes = task.get("retry_episodes")
        if retry_episodes:
            target_episodes = [ep for ep in retry_episodes if 1 <= ep <= total]
            if not target_episodes:
                task["status"] = "failed"
                task["error"] = "重试集数均超出范围"
                await _broadcast_task_update()
                persist_task(task)
                return
            task["total"] = len(target_episodes)
        else:
            task["total"] = end - start + 1

        existing_files = await asyncio.to_thread(dl.scan_local_album, album_title)
        task["skipped_count"] = 0

        if retry_episodes:
            download_queue = []
            for ep in target_episodes:
                idx = ep - 1
                if idx < len(tracks):
                    download_queue.append((idx, tracks[idx]))
        else:
            download_queue = [(i, tracks[i]) for i in range(start - 1, end)]

        MAX_AUTO_RETRY = 2

        for seq, (i, track) in enumerate(download_queue):
            if task.get("cancelled"):
                task["status"] = "cancelled"
                await _broadcast_task_update()
                persist_task(task)
                return

            track_id = track["trackId"]
            title = track.get("title", f"第{i+1}集")
            episode_num = i + 1

            episode_name = f"第{episode_num}集".lower()
            safe_title = re.sub(r'[\\/:*?"<>|\r\n]', '', title).strip().lower()
            if episode_name in existing_files or safe_title in existing_files:
                task["skipped_count"] += 1
                task["current"] = seq + 1
                task["current_title"] = f"{title} （已存在，跳过）"
                try:
                    existing = dl.check_track_exists(f"第{episode_num}集", album_title, fmt)
                    if existing:
                        task["completed_files"].append({
                            "title": title, "file_path": existing,
                            "file_size": Path(existing).stat().st_size,
                        })
                        persist_record(card_id, task_id, album_id, album_title, track_id,
                                       title, episode_num, existing, Path(existing).stat().st_size)
                except OSError:
                    pass
                if seq < len(download_queue) - 1 and not task.get("cancelled"):
                    await asyncio.sleep(0.5)
                continue

            lock_key = track_lock.make_key(card_id, album_title, episode_num)
            if not track_lock.try_acquire(lock_key):
                task["skipped_count"] += 1
                task["current"] = seq + 1
                task["current_title"] = f"{title} （其他任务下载中，跳过）"
                continue

            try:
                task["current"] = seq + 1
                task["current_title"] = title

                download_ok = False
                last_error = ""
                for attempt in range(1, MAX_AUTO_RETRY + 2):
                    if task.get("cancelled"):
                        task["status"] = "cancelled"
                        await _broadcast_task_update()
                        persist_task(task)
                        return
                    if attempt > 1:
                        task["current_title"] = f"{title} （自动重试第{attempt-1}次）"
                        await asyncio.sleep(random.randint(1, 3))
                    try:
                        result = await asyncio.to_thread(
                            dl.download_by_track_id, track_id, quality, album_title, False, fmt, episode_num
                        )
                        if result["success"]:
                            existing_files.add(episode_name)
                            existing_files.add(safe_title)
                            task["completed_files"].append({
                                "title": title, "file_path": result.get("file_path", ""),
                                "file_size": result.get("file_size", 0),
                            })
                            persist_record(card_id, task_id, album_id, album_title, track_id,
                                           title, episode_num, result.get("file_path", ""),
                                           result.get("file_size", 0))
                            if result.get("skipped"):
                                task["skipped_count"] += 1
                            else:
                                task["completed"] += 1
                            download_ok = True
                            break
                        else:
                            last_error = result.get("error", "未知错误")
                    except Exception as e:
                        last_error = str(e)

                while not download_ok and _is_rate_limited(last_error):
                    switched, new_aid = _switch_downloader()
                    if not switched:
                        break
                    task["current_title"] = f"{title} （切换至 {task['account_nickname']}）"
                    await _broadcast_task_update()
                    try:
                        result = await asyncio.to_thread(
                            dl.download_by_track_id, track_id, quality, album_title, False, fmt, episode_num
                        )
                        if result["success"]:
                            existing_files.add(episode_name)
                            existing_files.add(safe_title)
                            task["completed_files"].append({
                                "title": title, "file_path": result.get("file_path", ""),
                                "file_size": result.get("file_size", 0),
                            })
                            persist_record(card_id, task_id, album_id, album_title, track_id,
                                           title, episode_num, result.get("file_path", ""),
                                           result.get("file_size", 0))
                            if result.get("skipped"):
                                task["skipped_count"] += 1
                            else:
                                task["completed"] += 1
                            download_ok = True
                        else:
                            last_error = result.get("error", "未知错误")
                    except Exception as e:
                        last_error = str(e)

                # 鉴权失败：cookie 失效/未登录，终止任务并给出明确指引，避免逐集空转重试
                if not download_ok and _is_auth_failed(last_error):
                    task["status"] = "failed"
                    task["error"] = ("账号登录态已失效（cookie 可能已过期或被踢下线），"
                                     "请到「账号」页重新扫码登录后再下载")
                    await _broadcast_task_update()
                    persist_task(task)
                    dl.close()
                    return

                if not download_ok:
                    task["failed_list"].append({
                        "episode": episode_num, "title": title, "error": last_error,
                        "auto_retried": MAX_AUTO_RETRY,
                        "account_id": task.get("account_id", ""),
                        "account_nickname": task.get("account_nickname", ""),
                    })

                if seq < len(download_queue) - 1 and not task.get("cancelled"):
                    await _broadcast_task_update()
                    delay = random.randint(100, 300) / 1000
                    await asyncio.sleep(delay)
            finally:
                track_lock.release(lock_key)

        task["status"] = "done"
        task["finished_at"] = time.time()
        await _broadcast_task_update()

        parent_task_id = task.get("parent_task_id")
        if parent_task_id and parent_task_id in _batch_tasks:
            parent = _batch_tasks[parent_task_id]
            retry_ok_episodes = {f["title"] for f in task.get("completed_files", [])}
            old_failed = parent.get("failed_list", [])
            new_failed = []
            for f in old_failed:
                if f["title"] not in retry_ok_episodes:
                    new_failed.append(f)
                else:
                    parent["completed"] = parent.get("completed", 0) + 1
            parent["failed_list"] = new_failed
            parent["retry_started"] = False
            if not new_failed:
                parent["retry_result"] = "all_ok"
            else:
                parent["retry_result"] = f"部分成功，仍有 {len(new_failed)} 集失败"
            await _broadcast_task_update()
            if new_failed:
                maybe_start_auto_retry(parent_task_id)
    finally:
        ptid = task.get("parent_task_id")
        if ptid and ptid in _batch_tasks:
            parent = _batch_tasks[ptid]
            if parent.get("retry_started") and task.get("status") != "done":
                parent["retry_started"] = False
        if task.get("status") == "done" and not task.get("parent_task_id"):
            maybe_start_auto_retry(task_id)
        if task.get("status") in ("done", "failed", "cancelled"):
            try:
                persist_task(task)
            except Exception:
                pass
        dl.close()


@router.post("/batch")
async def start_batch_download(req: BatchDownloadRequest, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    ensure_download_mode_allowed(auth, "server")
    task_id = str(uuid.uuid4())[:8]
    card_id = auth["card_id"]
    download_root = str(_config.DOWNLOAD_DIR / auth["code"])

    account_nickname = ""
    accounts = _am.list_accounts(card_id)
    if not accounts:
        # 官方接口下载必须该卡密先扫码登录账号，否则无法使用
        raise HTTPException(
            status_code=400,
            detail="请先在「账号」页扫码登录喜马拉雅账号后再使用官方下载",
        )
    if req.account_id:
        _acc = _am.get_account_info(card_id, req.account_id)
        if _acc:
            account_nickname = _acc.get("nickname", "")

    _batch_tasks[task_id] = {
        "task_id": task_id,
        "card_id": card_id,
        "download_root": download_root,
        "engine": "official",
        "interface_name": "official",
        "album_id": req.album_id,
        "quality": req.quality,
        "start_episode": req.start_episode,
        "end_episode": req.end_episode,
        "status": "running",
        "album_title": "",
        "total": 0,
        "current": 0,
        "current_title": "",
        "completed": 0,
        "skipped_count": 0,
        "completed_files": [],
        "failed_list": [],
        "error": "",
        "started_at": time.time(),
        "finished_at": None,
        "cancelled": False,
        "fmt": req.fmt,
        "account_id": req.account_id or "",
        "account_nickname": account_nickname,
    }

    persist_task(_batch_tasks[task_id])
    asyncio.create_task(_run_batch_task(task_id, req.album_id, req.quality, req.start_episode,
                                        req.end_episode or 999999, req.fmt, account_id=req.account_id))
    return {"success": True, "task_id": task_id}


@router.get("/batch")
async def list_batch_tasks(auth: dict = Depends(get_current_card)):
    card_id = auth["card_id"]
    tasks = sorted(
        (t for t in _batch_tasks.values() if t.get("card_id") == card_id),
        key=lambda t: t.get("started_at", 0), reverse=True
    )
    return {
        "success": True,
        "tasks": [_task_summary(t) for t in tasks],
        "active_count": sum(1 for t in tasks if t.get("status") == "running"),
    }


@router.get("/batch/stream")
async def stream_batch_tasks(auth: dict = Depends(get_current_card)):
    card_id = auth["card_id"]
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _sse_queues.add(queue)

    async def event_generator():
        try:
            tasks = sorted((t for t in _batch_tasks.values() if t.get("card_id") == card_id),
                           key=lambda t: t.get("started_at", 0), reverse=True)
            active_count = sum(1 for t in tasks if t.get("status") == "running")
            payload = json.dumps({"tasks": [_task_summary(t) for t in tasks], "active_count": active_count},
                                 ensure_ascii=False)
            yield f"data: {payload}\n\n"

            heartbeat_interval = 30
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    tasks = sorted((t for t in _batch_tasks.values() if t.get("card_id") == card_id),
                                   key=lambda t: t.get("started_at", 0), reverse=True)
                    active_count = sum(1 for t in tasks if t.get("status") == "running")
                    payload = json.dumps({"tasks": [_task_summary(t) for t in tasks], "active_count": active_count},
                                         ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_queues.discard(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.get("/batch/{task_id}")
async def get_batch_status(task_id: str, auth: dict = Depends(get_current_card)):
    task = _batch_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    return {"success": True, **_task_summary(task)}


def _task_summary(task: dict) -> dict:
    total = task.get("total", 0) or 1
    completed = task.get("completed", 0)
    skipped = task.get("skipped_count", 0)
    done_count = completed + skipped
    percent = round(done_count / total * 100) if total > 0 else 0
    eta_seconds = 0
    eta_text = ""
    if task.get("status") == "running" and done_count > 0:
        elapsed = time.time() - task.get("started_at", time.time())
        avg_per_item = elapsed / done_count
        remaining = total - done_count
        eta_seconds = int(avg_per_item * remaining)
        if eta_seconds < 60:
            eta_text = f"{eta_seconds}秒"
        elif eta_seconds < 3600:
            eta_text = f"{eta_seconds // 60}分{eta_seconds % 60}秒"
        else:
            eta_text = f"{eta_seconds // 3600}小时{(eta_seconds % 3600) // 60}分"
    return {
        "task_id": task.get("task_id", ""),
        "status": task.get("status", ""),
        "album_title": task.get("album_title", ""),
        "album_id": task.get("album_id", 0),
        "quality": task.get("quality", 2),
        "total": task.get("total", 0),
        "current": task.get("current", 0),
        "current_title": task.get("current_title", ""),
        "completed": completed,
        "skipped_count": skipped,
        "failed_count": len(task.get("failed_list", [])),
        "failed_list": task.get("failed_list", []),
        "error": task.get("error", ""),
        "percent": percent,
        "eta_seconds": eta_seconds,
        "eta_text": eta_text,
        "account_id": task.get("account_id", ""),
        "account_nickname": task.get("account_nickname", ""),
        "auto_retry_round": task.get("auto_retry_round", 0),
        "auto_retry_next_at": task.get("auto_retry_next_at"),
        "interface_name": task.get("interface_name", "official"),
    }


@router.delete("/batch/{task_id}")
async def delete_batch_task(task_id: str, auth: dict = Depends(get_current_card)):
    task = _batch_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    # running/cancelling 都不可删：协程仍持有任务 dict，删除后 persist_task 会把行重新 upsert 回来（幽灵复活）
    if task.get("status") in ("running", "cancelling"):
        return {"success": False, "error": "运行中的任务无法删除，请先取消并等待其停止"}
    del _batch_tasks[task_id]
    # 物理删除数据库行
    from db.session import SessionLocal
    from db.models import DownloadTask
    db = SessionLocal()
    try:
        db.query(DownloadTask).filter_by(task_id=task_id).delete()
        db.commit()
    finally:
        db.close()
    return {"success": True, "message": "已删除"}


@router.post("/batch/{task_id}/cancel")
async def cancel_batch_download(task_id: str, auth: dict = Depends(get_current_card)):
    task = _batch_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    task["cancelled"] = True
    task["status"] = "cancelling"
    return {"success": True, "message": "正在取消..."}


@router.post("/batch/{task_id}/resume")
async def resume_batch_download(task_id: str, auth: dict = Depends(get_current_card)):
    task = _batch_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    status = task.get("status", "")
    # running/cancelling 都不可恢复：cancelling 时旧协程还在收尾，再起一个会双协程并发写同一 dict
    if status in ("running", "cancelling"):
        return {"success": False, "error": "任务正在运行中"}

    task["status"] = "running"
    task["error"] = ""
    task["cancelled"] = False
    task["finished_at"] = None
    task["started_at"] = time.time()
    task["failed_list"] = []

    dl = XimalayaDownloader(account_id=task.get("account_id") or None,
                            card_id=task.get("card_id"), download_root=task["download_root"])
    try:
        album_title = task.get("album_title", "")
        if album_title:
            existing_files = await asyncio.to_thread(dl.scan_local_album, album_title)
            task["completed"] = len(existing_files)
            task["skipped_count"] = 0
    finally:
        dl.close()

    persist_task(task)
    end_ep = task.get("end_episode") or 999999
    asyncio.create_task(_run_batch_task(task_id, task["album_id"], task.get("quality", 0),
                                        task.get("start_episode", 1), end_ep, task.get("fmt", "mp3"),
                                        account_id=task.get("account_id")))
    return {"success": True, "task_id": task_id, "message": "已恢复下载"}


def _start_retry_task(task: dict) -> str | None:
    failed_list = task.get("failed_list", [])
    if not failed_list or task.get("retry_started"):
        return None
    retry_episodes = sorted({f["episode"] for f in failed_list})
    task["retry_started"] = True

    new_task_id = str(uuid.uuid4())[:8]
    _batch_tasks[new_task_id] = {
        "task_id": new_task_id,
        "card_id": task["card_id"],
        "download_root": task["download_root"],
        "engine": "official",
        "interface_name": task.get("interface_name", "official"),
        "album_id": task["album_id"],
        "quality": task["quality"],
        "start_episode": retry_episodes[0],
        "end_episode": retry_episodes[-1],
        "status": "running",
        "album_title": "",
        "total": 0,
        "current": 0,
        "current_title": "",
        "completed": 0,
        "skipped_count": 0,
        "completed_files": [],
        "failed_list": [],
        "error": "",
        "started_at": time.time(),
        "finished_at": None,
        "cancelled": False,
        "retry_episodes": retry_episodes,
        "fmt": task.get("fmt", "mp3"),
        "parent_task_id": task["task_id"],
        "account_id": task.get("account_id", ""),
        "account_nickname": task.get("account_nickname", ""),
    }
    persist_task(_batch_tasks[new_task_id])
    asyncio.create_task(_run_batch_task(new_task_id, task["album_id"], task["quality"],
                                        retry_episodes[0], retry_episodes[-1], task.get("fmt", "mp3"),
                                        account_id=task.get("account_id")))
    return new_task_id


_auto_retry_loops: set[str] = set()


def maybe_start_auto_retry(task_id: str):
    task = _batch_tasks.get(task_id)
    if not task or task.get("status") != "done" or not task.get("failed_list"):
        return
    if task_id in _auto_retry_loops:
        return
    cfg = _config.get_auto_retry_config()
    if not cfg["enabled"]:
        return
    _auto_retry_loops.add(task_id)
    asyncio.create_task(_auto_retry_loop(task_id))


async def _auto_retry_loop(task_id: str):
    try:
        round_no = 0
        while True:
            task = _batch_tasks.get(task_id)
            if not task or task.get("cancelled") or not task.get("failed_list"):
                break
            cfg = _config.get_auto_retry_config()
            if not cfg["enabled"] or round_no >= cfg["max_rounds"]:
                break
            round_no += 1
            interval_s = cfg["interval_minutes"] * 60
            task["auto_retry_round"] = round_no
            task["auto_retry_next_at"] = time.time() + interval_s
            await _broadcast_task_update()
            wait_until = task["auto_retry_next_at"]
            aborted = False
            while time.time() < wait_until:
                await asyncio.sleep(min(5, max(0.5, wait_until - time.time())))
                t = _batch_tasks.get(task_id)
                if not t or t.get("cancelled"):
                    aborted = True
                    break
            if aborted:
                break
            task = _batch_tasks.get(task_id)
            while task and task.get("retry_started"):
                await asyncio.sleep(3)
                task = _batch_tasks.get(task_id)
            if not task or task.get("cancelled") or not task.get("failed_list"):
                break
            child_id = _start_retry_task(task)
            if not child_id:
                break
            logger.info(f"任务 {task_id} 自动重试第 {round_no} 轮，子任务 {child_id}，"
                        f"失败 {len(task.get('failed_list', []))} 集")
            persist_task(task)
            while True:
                await asyncio.sleep(3)
                child = _batch_tasks.get(child_id)
                if not child or child.get("status") not in ("running", "cancelling"):
                    break
            task = _batch_tasks.get(task_id)
            if not task:
                break
            if not task.get("failed_list"):
                break
        task = _batch_tasks.get(task_id)
        if task is not None:
            task["auto_retry_next_at"] = None
            failed_left = len(task.get("failed_list", []))
            if failed_left and task.get("status") == "done":
                task["retry_result"] = f"自动重试已结束，仍有 {failed_left} 集失败"
            await _broadcast_task_update()
            try:
                persist_task(task)
            except Exception:
                pass
    finally:
        _auto_retry_loops.discard(task_id)


def resume_interrupted_auto_retries():
    """服务启动时调用：恢复重启前「已结束但仍有失败集」的任务的自动重试"""
    for tid, t in list(_batch_tasks.items()):
        if t.get("parent_task_id"):
            continue
        if t.get("status") == "done" and t.get("failed_list"):
            maybe_start_auto_retry(tid)


@router.post("/batch/{task_id}/retry")
async def retry_batch_failed(task_id: str, auth: dict = Depends(get_current_card)):
    task = _batch_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "原任务不存在"}
    failed_list = task.get("failed_list", [])
    if not failed_list:
        return {"success": False, "error": "没有失败项需要重试"}
    if task.get("retry_started"):
        return {"success": False, "error": "已有重试任务在进行中，请等待完成"}
    new_task_id = _start_retry_task(task)
    if not new_task_id:
        return {"success": False, "error": "无法创建重试任务"}
    persist_task(task)
    return {"success": True, "task_id": new_task_id, "retry_count": len(failed_list)}
