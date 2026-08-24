"""全局下载槽 — card_id 唯一锁（本地插件下载与服务器下载共用同一个槽）

语义：
- 同一卡密全局最多只有一个「进行中的下载」：浏览器插件本地任务（holder_type='local'）
  或服务器任务（holder_type='server'：官方批量 / 第三方批量 / 单集 / 章节）。
- 租约（lease）模型：acquire 时写入 lease_until；持有者须用 heartbeat 续约；
  租约过期未续约则锁自动失效，任何人可抢占（支持抢占下载槽）。
- complete / cancel 调 release 主动释放；release / heartbeat 都带所有权校验
  （card_id + task_id），旧任务永远无法释放或续约新任务的锁。
- 服务重启时清理全部 'server' 遗留锁（进程内任务已随进程消亡）；
  'local' 锁保留（插件可能仍存活并在下载，靠租约过期自动释放）。

实现要点（防竞态）：
- 抢占是单条「条件 UPDATE / 条件 INSERT」：WHERE 子句里重查 lease 是否过期、
  持有者是否是自己。两个并发 acquire 至多一个成功，不存在「先查后插」竞态。
- 日期统一按 naive UTC 存取（SQLite 无时区，与全库 _utcnow 语义一致）；
  原始 SQL 中的比较用固定格式字符串，字典序 == 时间序。
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError

from db.session import SessionLocal
from db.models import CardDownloadLock, LocalTask  # noqa: F401  (LocalTask 供 sweep 使用)

logger = logging.getLogger(__name__)

# ── 租约时长（秒），可用环境变量覆盖，便于测试 ──
LOCAL_TTL_SECONDS = max(30, int(os.environ.get("DOWNLOAD_SLOT_LOCAL_TTL", "300")))
SERVER_TTL_SECONDS = max(15, int(os.environ.get("DOWNLOAD_SLOT_SERVER_TTL", "120")))

# 插件侧建议的心跳间隔（下发给插件，实际由插件按 alarm 节奏执行）
LOCAL_HEARTBEAT_SECONDS = 30

# 统一 409 文案（spec 指定）
SLOT_BUSY_DETAIL = "当前卡密已有下载任务进行中，请等待完成后再开始下一本"

_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _now() -> datetime:
    """naive UTC 当前时间（与 SQLite 存储语义一致）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _s(dt: datetime) -> str:
    """datetime → 固定格式字符串（供原始 SQL 绑定/比较）"""
    return dt.strftime(_FMT)


def _p(raw) -> Optional[datetime]:
    """存储字符串/ datetime → naive datetime"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    try:
        return datetime.strptime(str(raw)[:26], _FMT)
    except ValueError:
        return None


def _row_to_dict(row) -> dict:
    """(card_id, holder_type, task_id, claim_id, source, album_id, album_title,
    acquired_at, heartbeat_at, lease_until, card_code) → dict（含 expired 标记）"""
    acquired = _p(row[7])
    heartbeat = _p(row[8])
    lease_until = _p(row[9])
    now = _now()
    return {
        "card_id": row[0],
        "card_code": row[10] if len(row) > 10 else None,
        "holder_type": row[1],
        "task_id": row[2],
        "claim_id": row[3],
        "source": row[4],
        "album_id": row[5],
        "album_title": row[6],
        "acquired_at": acquired.isoformat() if acquired else None,
        "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        "lease_until": lease_until.isoformat() if lease_until else None,
        "expired": lease_until is not None and lease_until <= now,
    }


_COLUMNS = ("card_id, holder_type, task_id, claim_id, source, album_id, "
            "album_title, acquired_at, heartbeat_at, lease_until")


def ttl_for(holder_type: str) -> int:
    return LOCAL_TTL_SECONDS if holder_type == "local" else SERVER_TTL_SECONDS


# ════════════════════════════════════════
#  核心操作
# ════════════════════════════════════════
def acquire(card_id: int, holder_type: str, task_id: str,
            claim_id: Optional[str] = None,
            ttl_seconds: Optional[int] = None,
            source: Optional[str] = None,
            album_id: Optional[str] = None,
            album_title: Optional[str] = None) -> tuple[bool, Optional[dict]]:
    """尝试抢占卡密下载槽。返回 (是否成功, 当前锁快照)。

    成功条件：卡无锁 / 锁已租约过期（抢占）/ 同一 task_id 续约（幂等）。
    失败：锁被其他未过期任务持有。
    """
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else ttl_for(holder_type)
    won = False
    db = SessionLocal()
    try:
        for _ in range(3):  # 竞态下最多重试 2 次
            now = _now()
            lease_until = _s(now + timedelta(seconds=ttl))
            conn = db.connection()
            existing = conn.exec_driver_sql(
                f"SELECT task_id, lease_until FROM card_download_locks WHERE card_id = :cid",
                {"cid": card_id},
            ).fetchone()
            if existing is None:
                try:
                    conn.exec_driver_sql(
                        f"INSERT INTO card_download_locks "
                        f"({_COLUMNS}) VALUES "
                        f"(:cid, :ht, :tid, :clid, :src, :aid, :at, :now, :now, :lu)",
                        {"cid": card_id, "ht": holder_type, "tid": task_id,
                         "clid": claim_id, "src": source, "aid": album_id,
                         "at": album_title, "now": _s(now), "lu": lease_until},
                    )
                    won = True
                    break
                except IntegrityError:
                    # 常见原因：并发插入竞争（唯一约束）→ 转条件 UPDATE 路径重试。
                    # 也可能是外键失败（卡密已被删除）等异常：必须留痕，
                    # 否则 acquire 静默失败，调用方只会看到"槽被占用"的 409，无法排障。
                    logger.warning(f"下载槽 INSERT 失败（IntegrityError，将重试）: card={card_id} task={task_id}",
                                   exc_info=True)
                    db.rollback()
                    continue
            else:
                cur_tid, cur_lu = existing[0], _p(existing[1])
                expired = cur_lu is None or cur_lu <= now
                if not expired and cur_tid != task_id:
                    break  # 被其他未过期任务持有 → 失败
                res = conn.exec_driver_sql(
                    f"UPDATE card_download_locks SET "
                    f"holder_type = :ht, task_id = :tid, claim_id = :clid, "
                    f"source = :src, album_id = :aid, album_title = :at, "
                    f"acquired_at = :now, heartbeat_at = :now, lease_until = :lu "
                    f"WHERE card_id = :cid AND "
                    f"(task_id = :tid OR lease_until IS NULL OR lease_until < :now_s)",
                    {"cid": card_id, "ht": holder_type, "tid": task_id,
                     "clid": claim_id, "src": source, "aid": album_id,
                     "at": album_title, "now": _s(now), "now_s": _s(now),
                     "lu": lease_until},
                )
                if res.rowcount == 1:
                    won = True
                    break
                # rowcount=0：SELECT 与 UPDATE 之间被并发者抢先 → 重试
                db.rollback()
                continue
        db.commit()
    except Exception:
        logger.exception(f"下载槽 acquire 异常: card={card_id} task={task_id}")
        try:
            db.rollback()
        except Exception:
            pass
        return False, get_lock(card_id)
    finally:
        db.close()

    current = get_lock(card_id)
    ok = won and current is not None and current["task_id"] == task_id
    if not ok and won:
        # 理论上不该发生（commit 后被抢占）：安全起见回滚为失败
        logger.warning(f"下载槽 acquire 后校验失败: card={card_id} task={task_id}")
    return ok, current


def heartbeat(card_id: int, task_id: str, ttl_seconds: Optional[int] = None) -> tuple[bool, Optional[dict]]:
    """续约租约。仅当锁存在、持有者为 task_id 且租约未过期时成功（所有权+活性双校验）。"""
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else SERVER_TTL_SECONDS
    now = _now()
    lease_until = _s(now + timedelta(seconds=ttl))
    db = SessionLocal()
    try:
        res = db.connection().exec_driver_sql(
            f"UPDATE card_download_locks SET heartbeat_at = :now, lease_until = :lu "
            f"WHERE card_id = :cid AND task_id = :tid "
            f"AND lease_until IS NOT NULL AND lease_until > :now_s",
            {"now": _s(now), "lu": lease_until, "cid": card_id, "tid": task_id,
             "now_s": _s(now)},
        )
        ok = res.rowcount == 1
        db.commit()
        return ok, get_lock(card_id)
    except Exception:
        logger.exception(f"下载槽 heartbeat 异常: card={card_id} task={task_id}")
        try:
            db.rollback()
        except Exception:
            pass
        return False, get_lock(card_id)
    finally:
        db.close()


def release(card_id: int, task_id: str) -> bool:
    """释放锁（带所有权校验：只有当前持有者能释放）。返回是否真的删掉了锁。"""
    db = SessionLocal()
    try:
        res = db.connection().exec_driver_sql(
            f"DELETE FROM card_download_locks WHERE card_id = :cid AND task_id = :tid",
            {"cid": card_id, "tid": task_id},
        )
        db.commit()
        return res.rowcount == 1
    except Exception:
        logger.exception(f"下载槽 release 异常: card={card_id} task={task_id}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()


def get_lock(card_id: int) -> Optional[dict]:
    """读当前锁（含已过期行；调用方用 expired 字段判断）。无锁返回 None。"""
    db = SessionLocal()
    try:
        row = db.connection().exec_driver_sql(
            "SELECT l.card_id, l.holder_type, l.task_id, l.claim_id, l.source, "
            "l.album_id, l.album_title, l.acquired_at, l.heartbeat_at, l.lease_until "
            "FROM card_download_locks l WHERE l.card_id = :cid",
            {"cid": card_id},
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(tuple(row) + (None,))
    except Exception:
        logger.exception(f"下载槽 get_lock 异常: card={card_id}")
        return None
    finally:
        db.close()


def list_locks() -> list[dict]:
    """管理端：列出全部下载锁（含过期标记）"""
    db = SessionLocal()
    try:
        rows = db.connection().exec_driver_sql(
            "SELECT l.card_id, l.holder_type, l.task_id, l.claim_id, l.source, "
            "l.album_id, l.album_title, l.acquired_at, l.heartbeat_at, l.lease_until, c.code "
            "FROM card_download_locks l LEFT JOIN cards c ON c.id = l.card_id "
            "ORDER BY l.id"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.exception("下载锁 list_locks 异常")
        return []
    finally:
        db.close()


def force_release(card_id: int) -> Optional[dict]:
    """管理端强制释放：删除该卡密的锁（不看持有者/租约）。返回被删锁快照（无锁为 None）。"""
    db = SessionLocal()
    try:
        row = db.connection().exec_driver_sql(
            "SELECT l.card_id, l.holder_type, l.task_id, l.claim_id, l.source, "
            "l.album_id, l.album_title, l.acquired_at, l.heartbeat_at, l.lease_until, c.code "
            "FROM card_download_locks l LEFT JOIN cards c ON c.id = l.card_id "
            "WHERE l.card_id = :cid",
            {"cid": card_id},
        ).fetchone()
        snapshot = _row_to_dict(row) if row else None
        if row:
            db.connection().exec_driver_sql(
                "DELETE FROM card_download_locks WHERE card_id = :cid",
                {"cid": card_id},
            )
            db.commit()
        return snapshot
    except Exception:
        logger.exception(f"下载锁 force_release 异常: card={card_id}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def expire_stale() -> int:
    """物理删除所有已过期锁行（抢占本身不依赖它，仅为表卫生/管理端可读性）。"""
    now = _s(_now())
    db = SessionLocal()
    try:
        res = db.connection().exec_driver_sql(
            "DELETE FROM card_download_locks WHERE lease_until IS NOT NULL AND lease_until < :now",
            {"now": now},
        )
        n = res.rowcount
        db.commit()
        return n
    except Exception:
        logger.exception("下载锁 expire_stale 异常")
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()


def cleanup_on_startup() -> int:
    """服务启动清理：服务器重启 ⇒ 进程内任务全部消亡 ⇒ 所有 'server' 遗留锁一律清除。

    'local' 锁不清：插件可能仍存活并持有下载，其租约会自然过期释放。
    """
    db = SessionLocal()
    try:
        res = db.connection().exec_driver_sql(
            "DELETE FROM card_download_locks WHERE holder_type = 'server'"
        )
        n = res.rowcount
        db.commit()
        if n:
            logger.info(f"启动清理：移除 {n} 条服务器下载遗留锁")
        return n
    except Exception:
        logger.exception("启动清理下载锁异常")
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()


def sweep_expired_local_tasks() -> int:
    """租约过期的本地任务回退为 pending（插件侧已停止下载，任务重新排队）。

    只处理 status='running' 且 lease_until 已过期的任务：
    - 插件还在正常心跳 → lease_until 被持续续期，不会命中；
    - 插件死亡/浏览器关闭 → 心跳停止 → 租约过期 → 回退，卡密可被再次 claim。
    claim_id 一并清空，防止旧插件残留的 heartbeat/complete 用旧 claim 匹配成功。

    实现要点：**条件写入**（WHERE 里带 status 与 claim_id 双重校验），不做读-改-写。
    若插件的 complete 在「读取后、写入前」抢先提交（done），本 UPDATE 影响 0 行，
    绝不可能把已完成的任务改回 pending 造成重复下载。
    """
    now = _now()
    db = SessionLocal()
    try:
        # 先取候选（Python 侧比较时间，避免跨写入格式的 SQL datetime 比较）
        rows = (
            db.query(LocalTask)
            .filter(LocalTask.status == "running")
            .filter(LocalTask.lease_until.isnot(None))
            .all()
        )
        n = 0
        for r in rows:
            lu = r.lease_until
            if lu is not None:
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                if lu > now.replace(tzinfo=timezone.utc):
                    continue
            # 条件 UPDATE：写入时再校验 status 仍是 running、claim 未变
            res = db.connection().exec_driver_sql(
                "UPDATE local_tasks SET status = 'pending', claim_id = NULL, "
                "claimed_at = NULL, heartbeat_at = NULL, lease_until = NULL, "
                "error = '下载租约过期，任务已重新排队' "
                "WHERE id = :id AND status = 'running' AND claim_id IS :cid",
                {"id": r.id, "cid": r.claim_id},
            )
            if res.rowcount:
                n += 1
        if n:
            db.commit()
            logger.info(f"{n} 个本地下载任务因租约过期回退为 pending")
        return n
    except Exception:
        logger.exception("本地任务租约清扫异常")
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()


def busy_response(holder: Optional[dict]):
    """统一 409 响应（spec 文案 + 占用者信息，便于前端/插件提示）"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "error": SLOT_BUSY_DETAIL,
            "detail": SLOT_BUSY_DETAIL,
            "holder": holder,
        },
    )


async def server_task_heartbeat_loop(
    get_running: Callable[[], list[tuple[int, str]]],
    interval: int = 30,
) -> None:
    """服务器任务心跳主循环（app 启动时 create_task 一次）。

    get_running(): 返回 [(card_id, task_id)]，即当前所有 running/cancelling
    的服务器下载任务（批量/单集等）。

    关键语义：**只续约、绝不重新 acquire** —— 管理员强制释放后，
    循环不能把槽抢回来（被取消的任务正在收尾，槽已归新持有者）。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            for card_id, task_id in get_running():
                if not card_id or not task_id:
                    continue
                ok, _ = heartbeat(card_id, task_id)
                if not ok:
                    logger.debug(f"下载槽心跳未生效（可能已被释放）: card={card_id} task={task_id}")
            expire_stale()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("下载槽心跳循环异常")
