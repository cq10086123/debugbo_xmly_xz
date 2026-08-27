"""下载任务 / 记录的数据库持久化（官方与第三方引擎共用）

设计：内存字典 `_batch_tasks` 仍是实时状态源（保证进度/ETA 逻辑不变），
本模块负责把任务与「每集完成记录」按需要落库（启动时恢复、断点续传、卡密隔离查询）。
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from db.session import SessionLocal
from db.models import DownloadTask, DownloadRecord, XimalayaAccount, Card
from core import config as _config

logger = logging.getLogger(__name__)


def _dt(ts: Optional[float]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ts(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return dt.timestamp()


def persist_task(task: dict):
    """把内存任务字典 upsert 进 download_tasks 表（线程/协程安全）"""
    task_id = task.get("task_id")
    if not task_id:
        return
    db = SessionLocal()
    try:
        row = db.query(DownloadTask).filter_by(task_id=task_id).first()
        if row is None:
            row = DownloadTask(task_id=task_id)
            db.add(row)
        row.card_id = task.get("card_id")
        row.engine = task.get("engine", "official")
        # 第三方接口任务的字典里只有 book_id（见 interfaces.intf_batch），没有 album_id。
        # 不兜底的话 album_id 落库为 NULL，重启恢复后书籍标识丢失。
        _album_id = task.get("album_id")
        if _album_id is None:
            _album_id = task.get("book_id")
        row.album_id = str(_album_id) if _album_id is not None else None
        album_title = task.get("album_title", "")
        row.album_title = album_title or None
        row.start_episode = task.get("start_episode", 1) or 1
        row.end_episode = task.get("end_episode")
        row.fmt = task.get("fmt", "mp3") or "mp3"
        row.quality = task.get("quality", 0) or 0
        row.concurrency = task.get("concurrency", 1) or 1
        row.status = task.get("status", "running")
        row.total = task.get("total", 0) or 0
        row.current = task.get("current", 0) or 0
        row.completed = task.get("completed", 0) or 0
        row.skipped_count = task.get("skipped_count", 0) or 0
        row.current_title = task.get("current_title")
        row.failed_list = json.dumps(task.get("failed_list", []), ensure_ascii=False)
        row.completed_files = json.dumps(task.get("completed_files", []), ensure_ascii=False)
        row.error = task.get("error")
        row.retry_episodes = json.dumps(task.get("retry_episodes")) if task.get("retry_episodes") else None
        row.parent_task_id = task.get("parent_task_id")
        row.retry_started = bool(task.get("retry_started", False))
        row.auto_retry_round = task.get("auto_retry_round", 0) or 0
        row.auto_retry_next_at = _dt(task.get("auto_retry_next_at"))
        row.cancelled = bool(task.get("cancelled", False))
        row.created_at = _dt(task.get("started_at")) or row.created_at
        row.finished_at = _dt(task.get("finished_at"))
        db.commit()
    except Exception as e:
        logger.exception(f"persist_task 失败: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def persist_record(card_id, task_id, album_id, album_title, track_id,
                   title, episode_num, file_path, file_size):
    """插入一条下载完成记录（按 file_path 去重，避免重复计录）"""
    if not file_path:
        return
    db = SessionLocal()
    try:
        exists = db.query(DownloadRecord).filter_by(file_path=file_path).first()
        if exists:
            return
        db.add(DownloadRecord(
            card_id=card_id,
            task_id=task_id,
            album_id=str(album_id) if album_id is not None else None,
            album_title=album_title or None,
            track_id=str(track_id) if track_id is not None else None,
            title=title,
            episode_num=episode_num,
            file_path=file_path,
            file_size=file_size or 0,
        ))
        db.commit()
    except Exception as e:
        logger.exception(f"persist_record 失败: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def get_task_row(task_id: str, card_id: Optional[int] = None) -> Optional[dict]:
    """只读查询单个任务行（任意 engine），不修改任何状态。

    与 load_tasks_from_db 的区别：后者是「启动恢复」语义，会把 running 改写成
    interrupted；查询进度这类读路径绝不能有副作用，故单列此函数。

    card_id 传入时做归属校验（跨卡查询一律视为不存在）。
    """
    if not task_id:
        return None
    db = SessionLocal()
    try:
        row = db.query(DownloadTask).filter_by(task_id=task_id).first()
        if row is None:
            return None
        if card_id is not None and row.card_id != card_id:
            return None
        failed_list = json.loads(row.failed_list) if row.failed_list else []
        total = row.total or 0
        done = (row.completed or 0) + (row.skipped_count or 0)
        return {
            "task_id": row.task_id,
            "card_id": row.card_id,
            "engine": row.engine or "official",
            "interface_name": row.engine or "official",
            "album_id": row.album_id,
            "album_title": row.album_title or "",
            "status": row.status or "",
            "total": total,
            "current": row.current or 0,
            "current_title": row.current_title or "",
            "completed": row.completed or 0,
            "skipped_count": row.skipped_count or 0,
            "failed_list": failed_list,
            "failed_count": len(failed_list),
            "error": row.error or "",
            "percent": round(done / total * 100) if total > 0 else 0,
            "fmt": row.fmt or "mp3",
            "started_at": _ts(row.created_at),
            "finished_at": _ts(row.finished_at),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"get_task_row({task_id}) 失败：{e}")
        return None
    finally:
        db.close()


def load_tasks_from_db(engine: str = "official", *, exclude_engines=None) -> dict:
    """启动时从数据库恢复任务，将 running/cancelling 标记为 interrupted

    engine         —— 精确匹配某个引擎（官方为 "official"）。
    exclude_engines—— 反向选择：取「不属于这些引擎」的全部任务。第三方接口的
                      engine 存的是接口名（用户自定义、数量不定），无法枚举，
                      故用 exclude_engines={"official"} 一次性捞出所有第三方任务。
                      两个参数互斥，传了 exclude_engines 时 engine 被忽略。

    若表尚未创建（首次导入时 init_db 还未运行），安全返回空字典。
    """
    result: dict = {}
    db = SessionLocal()
    try:
        if exclude_engines:
            q = db.query(DownloadTask).filter(
                DownloadTask.engine.notin_(list(exclude_engines))
            )
            rows = q.all()
        else:
            rows = db.query(DownloadTask).filter_by(engine=engine).all()
        # 预加载 卡密ID -> 卡号，用于重建 per-card 下载目录（restart 后任务字典需含 download_root）
        card_codes = {c.id: c.code for c in db.query(Card).all()}
        for row in rows:
            if row.status in ("running", "cancelling"):
                row.status = "interrupted"
                row.retry_started = False
                row.auto_retry_next_at = None
                db.commit()
            failed_list = json.loads(row.failed_list) if row.failed_list else []
            completed_files = json.loads(row.completed_files) if row.completed_files else []
            retry_episodes = json.loads(row.retry_episodes) if row.retry_episodes else None
            result[row.task_id] = {
                "task_id": row.task_id,
                "card_id": row.card_id,
                "album_id": int(row.album_id) if row.album_id and row.album_id.isdigit() else row.album_id,
                "album_title": row.album_title or "",
                "start_episode": row.start_episode,
                "end_episode": row.end_episode,
                "status": row.status,
                "total": row.total,
                "current": row.current,
                "current_title": row.current_title or "",
                "completed": row.completed,
                "skipped_count": row.skipped_count,
                "completed_files": completed_files,
                "failed_list": failed_list,
                "error": row.error or "",
                "started_at": _ts(row.created_at) or time.time(),
                "finished_at": _ts(row.finished_at),
                "cancelled": bool(row.cancelled),
                "fmt": row.fmt or "mp3",
                "quality": row.quality or 0,
                "concurrency": row.concurrency or 1,
                "account_id": "",
                "account_nickname": "",
                "retry_episodes": retry_episodes,
                "parent_task_id": row.parent_task_id,
                "retry_started": bool(row.retry_started),
                "auto_retry_round": row.auto_retry_round or 0,
                "auto_retry_next_at": _ts(row.auto_retry_next_at),
                # 重建 per-card 下载目录：restart 后 _run_batch_task / resume / retry 都依赖此字段，
                # 缺失会触发 KeyError 导致任务永久卡死
                "download_root": str(_config.DOWNLOAD_DIR / card_codes.get(row.card_id, "")),
                "engine": row.engine or "official",
                "interface_name": row.engine or "official",
                # 第三方接口任务（_intf_tasks）额外依赖的字段，见 interfaces._task_summary。
                # 官方任务多带这几个键无副作用（_batch_tasks 的读取方按需取值）。
                "interface": row.engine or "official",
                "book_id": row.album_id or "",
                "last_error": "",
            }
    except Exception as e:
        # 表不存在等异常：安全返回空（init_db 尚未运行）
        logger.warning(f"load_tasks_from_db({engine}) 跳过：{e}")
    finally:
        db.close()
    return result


def clear_card_data(card_id: int):
    """卡密过期清零：删除该卡密的全部任务、记录以及登录的喜马拉雅账号

    磁盘下载文件保留；登录数据（cookie 等）一并销毁，符合「卡密过期后登录数据销毁」需求。
    """
    db = SessionLocal()
    try:
        db.query(DownloadRecord).filter_by(card_id=card_id).delete()
        db.query(DownloadTask).filter_by(card_id=card_id).delete()
        db.query(XimalayaAccount).filter_by(card_id=card_id).delete()
        db.commit()
    finally:
        db.close()
