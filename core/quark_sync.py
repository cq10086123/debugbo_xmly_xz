"""把卡密本地下载目录中的已完成专辑，复制到夸克挂载盘。

设计：
- 下载过程只写 DOWNLOAD_DIR，本模块只在用户/AI 主动触发时拷到 QUARK_SYNC_DIR/{书名}/
- 成功后不删本地（避免 FUSE 谎报成功导致丢书）
- 同名同大小跳过；大小不同则覆盖后再核对
- 同一卡密同时只允许一个同步任务（FUSE 不适合并发狂写）
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from core import config as _config

logger = logging.getLogger(__name__)

AUDIO_SUFFIX = (".m4a", ".mp3", ".aac")
# 与 core.downloader.XimalayaDownloader._sanitize_dirname 保持一致
_DIR_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n]')

_jobs: dict[str, dict] = {}
_card_running: dict[int, str] = {}  # card_id -> job_id
_lock = threading.Lock()
_MAX_JOBS = 40


class QuarkSyncError(Exception):
    """可预期的同步失败（挂载不可用 / 专辑不存在 / 正在下载 等）"""


def sanitize_dirname(name: str) -> str:
    """与下载器落盘目录名同一套清洗，避免「任务标题 ≠ 文件夹名」对不上。"""
    return _DIR_UNSAFE.sub("", name or "").strip()


def validate_album_name(name: str) -> str:
    album = (name or "").strip()
    if not album or "/" in album or "\\" in album or album in (".", "..") or "\x00" in album:
        raise QuarkSyncError("非法专辑名")
    return album


def quark_dir() -> Path:
    """返回已配置且存在的夸克挂载目录；否则抛 QuarkSyncError。

    目录不存在时绝不 mkdir：避免把「没挂上的卷」写成容器本地空目录。
    """
    root = _config.QUARK_SYNC_DIR
    if root is None:
        raise QuarkSyncError("未配置夸克挂载目录（QUARK_SYNC_DIR）")
    if not root.is_dir():
        raise QuarkSyncError("夸克挂载目录不可用，请检查 Docker 卷是否已挂载")
    return root


def card_album_dir(card_code: str, album: str) -> Path:
    album = validate_album_name(album)
    card_root = (_config.DOWNLOAD_DIR / card_code).resolve()
    target = (card_root / album).resolve()
    try:
        target.relative_to(card_root)
    except ValueError:
        raise QuarkSyncError("非法路径")
    return target


def list_album_audio(album_dir: Path) -> list[Path]:
    if not album_dir.is_dir():
        return []
    return sorted(
        (f for f in album_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_SUFFIX),
        key=lambda p: p.name,
    )


def scan_card_albums(card_code: str) -> list[dict]:
    """扫描卡密本地下载目录，返回 [{name, count, total_size}]。"""
    card_dir = _config.DOWNLOAD_DIR / card_code
    if not card_dir.is_dir():
        return []
    albums = []
    for child in sorted(card_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        files = list_album_audio(child)
        if not files:
            continue
        albums.append({
            "name": child.name,
            "count": len(files),
            "total_size": sum(f.stat().st_size for f in files),
        })
    return albums


def match_album(albums: list[dict], keyword: str) -> tuple[dict | None, list[dict]]:
    """按书名精确或包含匹配。返回 (唯一命中, 候选列表)。"""
    kw = (keyword or "").strip()
    if not kw:
        return None, albums
    exact = [a for a in albums if a["name"] == kw]
    if len(exact) == 1:
        return exact[0], exact
    contains = [a for a in albums if kw.lower() in a["name"].lower()]
    if len(contains) == 1:
        return contains[0], contains
    return None, contains or albums


def _title_matches_folder(title: str, folder: str) -> bool:
    raw = title or ""
    if raw == folder:
        return True
    return sanitize_dirname(raw) == folder


def album_is_downloading(card_id: int, album: str) -> bool:
    """该书是否仍有「服务器」下载在跑（禁止边下边同步）。

    不看插件本地下载：本地任务不写服务器磁盘，不应挡住往夸克拷贝。
    """
    try:
        from api.download import _batch_tasks
        from api.interfaces import _intf_tasks
        for tasks in (_batch_tasks, _intf_tasks):
            for t in list(tasks.values()):
                if t.get("card_id") != card_id:
                    continue
                if t.get("status") not in ("running", "cancelling"):
                    continue
                if _title_matches_folder(t.get("album_title") or "", album):
                    return True
    except Exception:
        logger.warning("检查服务器下载任务失败，为安全起见禁止同步", exc_info=True)
        return True
    return False


def _copy_one_file(src: Path, dest: Path) -> None:
    """先写 .quarkpart，核对大小后再替换，避免中途失败把夸克上已有文件截断。"""
    src_size = src.stat().st_size
    tmp = dest.with_name(dest.name + ".quarkpart")
    try:
        shutil.copyfile(str(src), str(tmp))
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        if tmp.stat().st_size != src_size:
            raise OSError(f"临时文件大小不一致（源 {src_size} / 临时 {tmp.stat().st_size}）")
        try:
            os.replace(tmp, dest)
        except OSError:
            # 部分 FUSE 不支持原子 replace，退化为覆盖拷贝
            shutil.copyfile(str(tmp), str(dest))
            try:
                tmp.unlink()
            except OSError:
                pass
        if dest.stat().st_size != src_size:
            raise OSError(f"拷贝后大小不一致（源 {src_size} / 目标 {dest.stat().st_size}）")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def copy_album(src_dir: Path, dest_dir: Path, job: dict) -> None:
    files = list_album_audio(src_dir)
    job["total"] = len(files)
    if not files:
        raise QuarkSyncError("专辑内无音频文件")

    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = skipped = failed = 0
    errors: list[str] = []

    for f in files:
        job["current"] = f.name
        dest = dest_dir / f.name
        try:
            src_size = f.stat().st_size
            if dest.exists() and dest.stat().st_size == src_size:
                skipped += 1
                job["skipped"] = skipped
                job["done"] = copied + skipped + failed
                continue
            _copy_one_file(f, dest)
            copied += 1
        except Exception as e:
            failed += 1
            errors.append(f"{f.name}: {e}")
            logger.warning("同步文件失败 %s -> %s: %s", f, dest, e)
        job["copied"] = copied
        job["skipped"] = skipped
        job["failed"] = failed
        job["done"] = copied + skipped + failed
        job["errors"] = errors[-10:]

    job["copied"] = copied
    job["skipped"] = skipped
    job["failed"] = failed
    job["errors"] = errors[-10:]
    if failed:
        raise QuarkSyncError(f"有 {failed} 个文件同步失败，本地文件未删除，可重试")


def _job_summary(job: dict) -> dict:
    total = job.get("total") or 0
    done = job.get("done") or 0
    return {
        "job_id": job.get("job_id"),
        "album": job.get("album"),
        "status": job.get("status"),
        "total": total,
        "done": done,
        "copied": job.get("copied", 0),
        "skipped": job.get("skipped", 0),
        "failed": job.get("failed", 0),
        "current": job.get("current") or "",
        "error": job.get("error") or "",
        "errors": job.get("errors") or [],
        "dest": job.get("dest") or "",
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def get_job(job_id: str, card_id: int) -> dict | None:
    job = _jobs.get(job_id)
    if not job or job.get("card_id") != card_id:
        return None
    return _job_summary(job)


def list_jobs(card_id: int) -> list[dict]:
    items = [j for j in _jobs.values() if j.get("card_id") == card_id]
    items.sort(key=lambda j: j.get("started_at") or 0, reverse=True)
    return [_job_summary(j) for j in items[:20]]


def _prune_jobs_locked() -> None:
    """丢掉已结束的旧任务，避免进程内字典无限涨。调用方必须持有 _lock。"""
    if len(_jobs) <= _MAX_JOBS:
        return
    finished = [
        (jid, j) for jid, j in _jobs.items()
        if j.get("status") != "running"
    ]
    finished.sort(key=lambda x: x[1].get("finished_at") or 0)
    extra = len(_jobs) - _MAX_JOBS
    for jid, _j in finished[:extra]:
        _jobs.pop(jid, None)


def running_job_for_card(card_id: int) -> dict | None:
    with _lock:
        jid = _card_running.get(card_id)
    if not jid:
        return None
    return get_job(jid, card_id)


def _run_job(job: dict, src: Path, dest: Path, card_id: int) -> None:
    try:
        copy_album(src, dest, job)
        job["status"] = "done"
        job["current"] = ""
    except QuarkSyncError as e:
        job["status"] = "failed"
        job["error"] = str(e)
    except Exception as e:
        logger.exception("夸克同步异常")
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        job["finished_at"] = time.time()
        with _lock:
            if _card_running.get(card_id) == job.get("job_id"):
                _card_running.pop(card_id, None)


def start_sync(card_id: int, card_code: str, album: str) -> dict:
    """校验后入队，立即返回 running 任务（复制在后台线程执行）。"""
    album = validate_album_name(album)
    root = quark_dir()
    src = card_album_dir(card_code, album)
    if not src.is_dir():
        raise QuarkSyncError("专辑不存在")
    if not list_album_audio(src):
        raise QuarkSyncError("专辑内无音频文件")
    if album_is_downloading(card_id, album):
        raise QuarkSyncError("该书仍在下载中，请等下载完成后再同步")

    dest = (root / album).resolve()
    try:
        dest.relative_to(root.resolve())
    except ValueError:
        raise QuarkSyncError("非法目标路径")

    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "card_id": card_id,
        "album": album,
        "status": "running",
        "total": 0,
        "done": 0,
        "copied": 0,
        "skipped": 0,
        "failed": 0,
        "current": "",
        "error": "",
        "errors": [],
        "dest": str(dest),
        "started_at": time.time(),
        "finished_at": None,
    }

    with _lock:
        existing = _card_running.get(card_id)
        if existing:
            old = _jobs.get(existing)
            if old and old.get("status") == "running":
                raise QuarkSyncError("已有同步任务进行中，请等待完成后再试")
        _jobs[job_id] = job
        _card_running[card_id] = job_id
        _prune_jobs_locked()

    threading.Thread(
        target=_run_job, args=(job, src, dest, card_id),
        name=f"quark-sync-{job_id}", daemon=True,
    ).start()
    return _job_summary(job)
