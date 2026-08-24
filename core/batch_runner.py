"""泛型批量下载器 — 适配任意 BaseInterfaceAdapter（http / itingshu 等直链型接口）

沿用第三方下载的落盘与重试约定：.part 临时文件、Content-Length 校验、本地已存在跳过、
track_lock 防并发重复、单集多次重试、记录落库。与第三方批量逻辑独立，互不干扰。
"""

import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path

import requests
import urllib3

from api.persistence import persist_task, persist_record
from core import config as _config
from core import track_lock

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_session = requests.Session()
_session.verify = False
_session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
_session.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=32))


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]', "", name).strip()


def _scan_local(album_title: str | None, download_root: Path) -> set[str]:
    existing = set()
    if not download_root.exists():
        return existing
    scan_dir = download_root
    if album_title:
        safe = _sanitize(album_title)
        if safe:
            scan_dir = download_root / safe
    if not scan_dir.exists():
        return existing
    for f in scan_dir.iterdir():
        if f.is_file() and f.suffix in (".m4a", ".mp3", ".aac") and f.stat().st_size > 0:
            existing.add(f.stem.lower())
    return existing


def _episode_target_path(album_title: str | None, file_name: str, fmt: str, download_root: Path):
    save_dir = download_root
    if album_title:
        subdir = _sanitize(album_title)
        if subdir:
            save_dir = download_root / subdir
    return save_dir, save_dir / f"{file_name}.{fmt}"


def _download_file(url: str, file_name: str, album_title: str | None, fmt: str, download_root: Path) -> tuple[str, int]:
    save_dir, save_path = _episode_target_path(album_title, file_name, fmt, download_root)
    save_dir.mkdir(parents=True, exist_ok=True)
    if save_path.exists() and save_path.stat().st_size > 0:
        return str(save_path), save_path.stat().st_size
    resp = _session.get(url, stream=True, verify=False, timeout=120)
    resp.raise_for_status()
    expected = int(resp.headers.get("Content-Length") or 0)
    tmp_path = save_path.with_name(save_path.name + ".part")
    total = 0
    error: Exception | None = None
    try:
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                total += len(chunk)
    except Exception as e:  # noqa: BLE001
        error = e
    if error is not None and not (expected and total == expected and total > 0):
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise error
    if expected and total != expected:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise IOError(f"下载不完整: 期望 {expected} 字节，实际收到 {total} 字节")
    os.replace(tmp_path, save_path)
    return str(save_path), total


async def run_generic_batch(
    adapter,
    task: dict,
    card_id: int,
    book_id: str,
    start_episode: int,
    end_episode: int,
    fmt: str = "mp3",
    concurrency: int = 1,
    download_root: str | None = None,
    broadcast=None,
):
    """运行泛型批量下载（更新 task 字典并广播进度）"""
    # 兜底必须用绝对路径（DOWNLOAD_DIR），避免调用方漏传时使用相对 cwd 的 "downloads"
    # 造成不可预测落盘位置；正常路径下调用方（intf_batch 等）均传入 per-card 绝对路径。
    download_root = Path(download_root or _config.DOWNLOAD_DIR)
    sem = asyncio.Semaphore(max(1, min(30, concurrency)))

    try:
        try:
            data = await asyncio.to_thread(adapter.get_chapters, book_id)
        except Exception as e:  # noqa: BLE001
            task["status"] = "failed"
            task["error"] = f"获取章节列表失败: {e}"
            if broadcast:
                await broadcast()
            persist_task(task)
            return

        if not data.get("success"):
            task["status"] = "failed"
            task["error"] = data.get("error", "未获取到章节列表")
            if broadcast:
                await broadcast()
            persist_task(task)
            return

        album_title = data.get("album_title", "")
        tracks = data.get("tracks", [])
        if not tracks:
            task["status"] = "failed"
            task["error"] = "未获取到章节列表"
            if broadcast:
                await broadcast()
            persist_task(task)
            return

        total = len(tracks)
        start = max(1, start_episode)
        end = min(total, end_episode)
        task["album_title"] = album_title
        task["total"] = end - start + 1

        existing_files = await asyncio.to_thread(_scan_local, album_title, download_root)
        task["skipped_count"] = 0
        download_queue = [(i, tracks[i]) for i in range(start - 1, end)]

        MAX_RETRY = 4

        async def _download_one(i: int, track: dict):
            if task.get("cancelled"):
                return
            chapter_id = track["trackId"]
            title = track.get("title", f"第{i+1}集")
            episode_num = i + 1

            ep_name = f"第{episode_num}集".lower()
            safe_title = _sanitize(title).lower()
            if ep_name in existing_files or safe_title in existing_files:
                task["skipped_count"] += 1
                task["current"] = max(task.get("current", 0), i + 1)
                task["current_title"] = f"{title} （已存在，跳过）"
                _, skip_path = _episode_target_path(album_title, f"第{episode_num}集", fmt, download_root)
                try:
                    if skip_path.exists() and skip_path.stat().st_size > 0:
                        task["completed_files"].append({
                            "title": title, "file_path": str(skip_path),
                            "file_size": skip_path.stat().st_size,
                        })
                        persist_record(card_id, task["task_id"], book_id, album_title, chapter_id,
                                       title, episode_num, str(skip_path), skip_path.stat().st_size)
                except OSError:
                    pass
                if broadcast:
                    await broadcast()
                return

            lock_key = track_lock.make_key(card_id, album_title or str(book_id), episode_num)
            if not track_lock.try_acquire(lock_key):
                task["skipped_count"] += 1
                task["current"] = max(task.get("current", 0), i + 1)
                task["current_title"] = f"{title} （其他任务下载中，跳过）"
                if broadcast:
                    await broadcast()
                return

            try:
                async with sem:
                    task["current"] = max(task.get("current", 0), i + 1)
                    task["current_title"] = title
                    if broadcast:
                        await broadcast()

                    file_name = f"第{episode_num}集"
                    _, target_path = _episode_target_path(album_title, file_name, fmt, download_root)
                    if target_path.exists() and target_path.stat().st_size > 0:
                        existing_files.add(ep_name)
                        task["completed"] += 1
                        task["completed_files"].append({
                            "title": title, "file_path": str(target_path),
                            "file_size": target_path.stat().st_size,
                        })
                        persist_record(card_id, task["task_id"], book_id, album_title, chapter_id,
                                       title, episode_num, str(target_path), target_path.stat().st_size)
                        return

                    ok = False
                    last_err = ""
                    for attempt in range(1, MAX_RETRY + 2):
                        if task.get("cancelled"):
                            return
                        if attempt > 1:
                            task["current_title"] = f"{title} （自动重试第{attempt-1}次）"
                            if broadcast:
                                await broadcast()
                            await asyncio.sleep(random.randint(2, 5))
                        try:
                            audio_url = await asyncio.to_thread(adapter.get_audio_url, book_id, str(chapter_id))
                        except Exception as e:  # noqa: BLE001
                            last_err = f"获取音频链接失败: {e}"
                            task["last_error"] = last_err
                            if broadcast:
                                await broadcast()
                            continue
                        if not audio_url:
                            last_err = "未获取到下载链接"
                            task["last_error"] = last_err
                            continue
                        try:
                            file_path, file_size = await asyncio.to_thread(
                                _download_file, audio_url, file_name, album_title, fmt, download_root
                            )
                            existing_files.add(ep_name)
                            existing_files.add(safe_title)
                            task["completed"] += 1
                            task["completed_files"].append({
                                "title": title, "file_path": file_path, "file_size": file_size,
                            })
                            persist_record(card_id, task["task_id"], book_id, album_title, chapter_id,
                                           title, episode_num, file_path, file_size)
                            ok = True
                            break
                        except Exception as e:  # noqa: BLE001
                            msg = str(e)
                            if any(k in msg for k in ("SSL", "SSLError", "ConnectionError",
                                                      "RemoteDisconnected", "ReadTimeout", "Max retries",
                                                      "EOF occurred", "ConnectionReset", "Connection aborted",
                                                      "Timeout", "不完整")):
                                last_err = "音频源站连接中断（网络抖动），正在自动重试"
                            else:
                                last_err = f"下载失败: {msg}"
                            task["last_error"] = last_err
                            if broadcast:
                                await broadcast()
                    if not ok:
                        task["failed_list"].append({
                            "episode": episode_num, "title": title,
                            "error": last_err, "auto_retried": MAX_RETRY,
                        })
            finally:
                track_lock.release(lock_key)

        coros = [_download_one(i, track) for i, track in download_queue]
        await asyncio.gather(*coros)

        if task.get("cancelled"):
            task["status"] = "cancelled"
        else:
            task["status"] = "done"
            if task.get("failed_list"):
                task["error"] = (f"共 {len(task['failed_list'])} 集下载失败："
                                 f"{task['failed_list'][0].get('error','')} 等。"
                                 f"可稍后重试失败集。")
        task["finished_at"] = time.time()
        if broadcast:
            await broadcast()
        try:
            persist_task(task)
        except Exception:  # noqa: BLE001
            pass
    finally:
        # 全局下载槽：任务终止时释放（所有权校验——槽若已被管理员强释或他人抢占，
        # 此处为空操作，绝不误删新锁）
        try:
            from core import download_slot
            download_slot.release(card_id, task.get("task_id"))
        except Exception:  # noqa: BLE001
            logger.exception("释放下载槽失败")
