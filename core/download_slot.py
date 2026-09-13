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
- 语义分层（2026-09-12 修订）：**基础设施异常 ≠ 所有权丢失**。
  `renew()` 返回三态并在 DB 异常时抛 SlotUnavailable，由调用方翻译成可重试的 5xx；
  `heartbeat()/release()/get_lock()` 保持既有「best-effort 不抛异常」签名，老调用点零改动。
- 租约脏值 fail-closed：无法解析的 lease_until 一律当作「仍然有效」，
  绝不因为读不懂就把锁判成过期而被人抢走（见 _lease_of）。
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError

from db.session import SessionLocal
from db.models import CardDownloadLock, LocalTask  # noqa: F401  (LocalTask 供 sweep 使用)

logger = logging.getLogger(__name__)


class SlotUnavailable(RuntimeError):
    """下载槽基础设施异常（DB 锁/超时/IO/连接失败）。

    必须与「租约被别人持有」严格区分：
    - 前者：客户端应退避重试（老插件会当错误处理，但语义上不允许当成所有权失效）；
    - 后者：才是真的 claim 丢失，客户端必须停止本地下载。
    历史实现把两者统一返回 False/None，导致「服务器抖一下 / database is locked」
    被插件解读成「别的设备抢走了下载槽」，直接掐断正在下的文件。
    """

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
    """存储字符串/ datetime → naive datetime（解析失败返回 None）。"""
    dt, _ok = _lease_of(raw)
    return dt


# 合法存储格式：_s() 写出的 26 字符（含 6 位微秒）；另兼容手工修库/备份恢复常见的
# 「无微秒」和「ISO with T」两种形态，避免把正常数据误判成脏数据。
_PARSE_FORMATS = (_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")
# 字符串短于该长度必然不是可用时间戳（expire_stale / 抢占谓词据此排除脏行）
_MIN_LEASE_LEN = 19


def _lease_of(raw) -> tuple[Optional[datetime], bool]:
    """租约原值 → (naive datetime | None, 是否可解析)。

    (None, True)  列为 NULL —— 「没有租约」，调用方按既有语义视为已过期（可被抢占）
    (dt,   True)  正常
    (None, False) 值存在但读不懂（手工改库 / 跨库恢复 / 半截写入）
                  ⇒ **fail-closed**：调用方必须当成「仍然有效」。早期实现把这种行当过期，
                    结果是任何一次 acquire 都能把锁抢走，表现为「两台设备同时下同一本书」。
    """
    if raw is None:
        return None, True
    if isinstance(raw, datetime):
        return (raw.replace(tzinfo=None) if raw.tzinfo else raw), True
    txt = str(raw)
    for fmt in _PARSE_FORMATS:
        try:
            return datetime.strptime(txt[:26], fmt), True
        except ValueError:
            continue
    logger.error(f"下载槽租约字段无法解析，按「仍然有效」处理（fail-closed，请检查该卡密的锁行）: {raw!r}")
    return None, False


def lease_of(raw) -> Optional[datetime]:
    """对外只读的租约解析入口（NULL/脏值 → None）。调用方不必关心 fail-closed 细节。"""
    return _lease_of(raw)[0]


def _row_to_dict(row) -> dict:
    """(card_id, holder_type, task_id, claim_id, source, album_id, album_title,
    acquired_at, heartbeat_at, lease_until, card_code) → dict（含 expired 标记）"""
    acquired = _p(row[7])
    heartbeat = _p(row[8])
    lease_until, lease_ok = _lease_of(row[9])
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
        # 管理端排障用：租约值读不懂时不再静默当过期，而是显式标记
        "lease_unparsable": not lease_ok,
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
            album_title: Optional[str] = None,
            *,
            db=None,
            raise_on_error: bool = False) -> tuple[bool, Optional[dict]]:
    """尝试抢占卡密下载槽。返回 (是否成功, 当前锁快照)。

    成功条件：卡无锁 / 锁已租约过期（抢占）/ 同一 task_id 续约（幂等）。
    失败：锁被其他未过期任务持有。

    db：传入则复用调用方会话（同事务，不 commit / 不 close / 不回滚），用于把
        「槽 + 任务」两处写入合成一次提交；不传则自开会话并自行提交（既有行为）。
    raise_on_error：True 时 DB 异常抛 SlotUnavailable（供 renew 区分基础设施故障）；
        默认 False ⇒ 与历史行为一致，异常返回 (False, 快照)。
    """
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else ttl_for(holder_type)
    won = False
    borrowed = db is not None
    own = db if borrowed else SessionLocal()
    try:
        for _ in range(3):  # 竞态下最多重试 2 次
            now = _now()
            lease_until = _s(now + timedelta(seconds=ttl))
            conn = own.connection()
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
                    if not borrowed:
                        own.rollback()   # 借用会话时不回滚：会连带丢掉调用方同事务内的其它写入
                    continue
            else:
                cur_tid = existing[0]
                cur_lu, lease_ok = _lease_of(existing[1])
                # lease_ok=False（脏值）⇒ 不视为过期 ⇒ 不可被抢（fail-closed，见 _lease_of）
                expired = lease_ok and (cur_lu is None or cur_lu <= now)
                if not expired and cur_tid != task_id:
                    break  # 被其他未过期任务持有 → 失败
                res = conn.exec_driver_sql(
                    f"UPDATE card_download_locks SET "
                    f"holder_type = :ht, task_id = :tid, claim_id = :clid, "
                    f"source = :src, album_id = :aid, album_title = :at, "
                    f"acquired_at = :now, heartbeat_at = :now, lease_until = :lu "
                    f"WHERE card_id = :cid AND "
                    f"(task_id = :tid OR lease_until IS NULL "
                    f" OR (length(lease_until) >= {_MIN_LEASE_LEN} AND lease_until < :now_s))",
                    {"cid": card_id, "ht": holder_type, "tid": task_id,
                     "clid": claim_id, "src": source, "aid": album_id,
                     "at": album_title, "now": _s(now), "now_s": _s(now),
                     "lu": lease_until},
                )
                if res.rowcount == 1:
                    won = True
                    break
                # rowcount=0：SELECT 与 UPDATE 之间被并发者抢先 → 重试
                if not borrowed:
                    own.rollback()
                continue
        if not borrowed:
            own.commit()
    except Exception as e:
        logger.exception(f"下载槽 acquire 异常: card={card_id} task={task_id}")
        if not borrowed:
            try:
                own.rollback()
            except Exception:
                pass
        if raise_on_error:
            raise SlotUnavailable(
                f"下载槽 acquire 失败（基础设施异常）: card={card_id} task={task_id}") from e
        return False, get_lock(card_id)
    finally:
        if not borrowed:
            own.close()

    current = _read_lock(own, card_id) if borrowed else get_lock(card_id)
    ok = won and current is not None and current["task_id"] == task_id
    if not ok and won:
        # 理论上不该发生（commit 后被抢占）：安全起见回滚为失败
        logger.warning(f"下载槽 acquire 后校验失败: card={card_id} task={task_id}")
    return ok, current


def _read_lock(own, card_id: int) -> Optional[dict]:
    """在指定会话（可能与调用方同一事务）内读锁快照。异常向上抛，由调用方决定语义。"""
    row = own.connection().exec_driver_sql(
        "SELECT l.card_id, l.holder_type, l.task_id, l.claim_id, l.source, "
        "l.album_id, l.album_title, l.acquired_at, l.heartbeat_at, l.lease_until "
        "FROM card_download_locks l WHERE l.card_id = :cid",
        {"cid": card_id},
    ).fetchone()
    return None if row is None else _row_to_dict(tuple(row) + (None,))


def renew(card_id: int, task_id: str,
          *,
          claim_id: Optional[str] = None,
          holder_type: str = "local",
          ttl_seconds: Optional[int] = None,
          allow_reacquire: bool = False,
          require_live: bool = True,
          source: Optional[str] = None,
          album_id: Optional[str] = None,
          album_title: Optional[str] = None,
          db=None) -> tuple[str, Optional[dict]]:
    """续约租约。返回 (状态, 锁快照)，状态 ∈ 'renewed' | 'lost'。

    与 `heartbeat()` 的差别（修 A2「心跳晚一拍即判死」）：
    - `require_live=False` 时不再要求「租约尚未过期」，改为要求「锁行仍属于本次持有者」：
      task_id 必须等于本任务，且 claim_id 一致（服务器持有者的 claim 为 NULL ⇒ 跳过该校验）。
      于是「无人抢占、只是续约迟到」可以原地续命；而任何一次别的 claim 都会改写
      local_tasks.claim_id ⇒ 本机下次心跳带着旧凭证 ⇒ 依旧被拒（不变式 I3）。
    - `allow_reacquire=True`：锁行已被 expire_stale() 物理删除时，用 acquire() 的同一条
      条件 SQL 重新登记 —— 并发下仍至多一个胜者，**不构成抢占**（不变式 I1）。
    - DB 异常抛 SlotUnavailable，由调用方翻译成可重试的 5xx / 降级响应，
      绝不许再翻译成「租约失效」（修 A3）。
    - 只续约、绝不抢别人的锁：谓词里从不出现「无条件覆盖他人持有者」。
    """
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else ttl_for(holder_type)
    borrowed = db is not None
    own = db if borrowed else SessionLocal()
    now = _now()
    lease_until = _s(now + timedelta(seconds=ttl))
    try:
        sql = ("UPDATE card_download_locks SET heartbeat_at = :now, lease_until = :lu "
               "WHERE card_id = :cid AND task_id = :tid "
               "  AND (:claim IS NULL OR claim_id IS NULL OR claim_id = :claim)")
        params = {"now": _s(now), "lu": lease_until, "cid": card_id, "tid": task_id,
                  "claim": claim_id, "now_s": _s(now)}
        if require_live:
            # 严格模式（服务器任务心跳）：过期即不可续 ⇒ 管理员强释/租约到点后不会复活
            sql += " AND lease_until IS NOT NULL AND lease_until > :now_s"
        else:
            # 宽松模式：过期但仍是本持有者 ⇒ 原地续租。脏值行（读不懂）不覆盖，
            # 留给管理端排障，避免「续租顺手改写事故现场」。
            sql += (f" AND (lease_until IS NULL OR length(lease_until) >= {_MIN_LEASE_LEN})")
        res = own.connection().exec_driver_sql(sql, params)
        if res.rowcount == 1:
            if not borrowed:
                own.commit()
            return "renewed", _read_lock(own, card_id)

        # rowcount=0：锁行不存在 / 属于别的任务 / 凭证不匹配 /（严格模式）已过期
        if not allow_reacquire:
            return "lost", _read_lock(own, card_id)
        ok, lock = acquire(
            card_id, holder_type, task_id, claim_id=claim_id, ttl_seconds=ttl,
            source=source, album_id=album_id, album_title=album_title,
            db=own, raise_on_error=True,
        )
        if ok:
            # acquire 被要求「不自取 commit」（会话归 renew 管），这里必须补上，
            # 否则自持会话场景下重新登记的 INSERT 会随 close 一起回滚掉。
            if not borrowed:
                own.commit()
            return "renewed", _read_lock(own, card_id)
        return "lost", lock
    except SlotUnavailable:
        raise
    except Exception as e:
        logger.exception(f"下载槽 renew 异常: card={card_id} task={task_id}")
        if not borrowed:
            try:
                own.rollback()
            except Exception:
                pass
        raise SlotUnavailable(
            f"下载槽续约不可用（基础设施异常）: card={card_id} task={task_id}") from e
    finally:
        if not borrowed:
            own.close()


def heartbeat(card_id: int, task_id: str, ttl_seconds: Optional[int] = None) -> tuple[bool, Optional[dict]]:
    """续约租约（历史签名：best-effort，**绝不抛异常**）。

    仅当锁存在、持有者为 task_id 且租约未过期时成功（所有权+活性双校验）——
    服务器任务心跳循环用它即可：管理员强制释放后绝不能把槽抢回来（不变式 I4）。
    插件侧「可恢复续约」请用 renew(require_live=False, allow_reacquire=True)。
    """
    try:
        state, lock = renew(card_id, task_id, ttl_seconds=ttl_seconds,
                            holder_type="server", require_live=True, allow_reacquire=False)
        return state == "renewed", lock
    except SlotUnavailable:
        # 与历史行为一致：DB 抖动 ⇒ 本轮续约无效（服务器任务循环下一拍自会重试）
        logger.warning(f"下载槽 heartbeat 基础设施异常（本轮跳过）: card={card_id} task={task_id}")
        return False, None


def release(card_id: int, task_id: str, *, db=None, raise_on_error: bool = False) -> bool:
    """释放锁（带所有权校验：只有当前持有者能释放）。返回是否真的删掉了锁。

    db：传入则只执行 DELETE，不 commit（与调用方的任务状态写入同事务）。
    raise_on_error：True 时 DB 异常抛 SlotUnavailable（默认 False，保持旧行为）。
    """
    borrowed = db is not None
    own = db if borrowed else SessionLocal()
    try:
        res = own.connection().exec_driver_sql(
            f"DELETE FROM card_download_locks WHERE card_id = :cid AND task_id = :tid",
            {"cid": card_id, "tid": task_id},
        )
        if not borrowed:
            own.commit()
        return res.rowcount == 1
    except Exception as e:
        logger.exception(f"下载槽 release 异常: card={card_id} task={task_id}")
        if not borrowed:
            try:
                own.rollback()
            except Exception:
                pass
        if raise_on_error:
            raise SlotUnavailable(
                f"下载槽释放失败（基础设施异常）: card={card_id} task={task_id}") from e
        return False
    finally:
        if not borrowed:
            own.close()


def get_lock(card_id: int, *, raise_on_error: bool = False) -> Optional[dict]:
    """读当前锁（含已过期行；调用方用 expired 字段判断）。无锁返回 None。

    raise_on_error=True：读不出锁时抛 SlotUnavailable —— 用于「读不到 ⇒ 绝不能当成
    『没有下载在进行』或『租约已失效』」的鉴权路径（修 A3）。
    """
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
    except Exception as e:
        logger.exception(f"下载槽 get_lock 异常: card={card_id}")
        if raise_on_error:
            raise SlotUnavailable(f"下载槽状态不可读（基础设施异常）: card={card_id}") from e
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
        # length(lease_until) >= 19：读不懂的脏值一律不当「已过期」删除，
        # 留给管理端定位（与 _lease_of 的 fail-closed 一致，避免顺手抹掉事故现场）
        res = db.connection().exec_driver_sql(
            "DELETE FROM card_download_locks WHERE lease_until IS NOT NULL "
            f"AND length(lease_until) >= {_MIN_LEASE_LEN} AND lease_until < :now",
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


def reap_orphan_locks(stale_seconds: Optional[int] = None) -> int:
    """回收「租约读不懂 + 长时间没心跳 + 对应任务已不在下载中」的孤儿锁行。

    为什么必须有：脏 `lease_until`（手工改库 / 跨库恢复 / 半截写入）被 `_lease_of`
    判为「仍然有效」⇒ fail-closed ⇒ 既不能被 acquire 抢占、也不会被 `expire_stale`
    删除（那是刻意保留事故现场）。如果它指向的任务其实早就不在了，这个卡密就会
    **永久** 409「已有下载任务进行中」——比它想防的双下载更糟。

    三重条件同时成立才动手，因此绝不碰正在下载的锁：
      holder_type='local' ∧ length(lease_until) < 19 ∧ heartbeat_at 早于 3×TTL ∧ 无 pending/running 任务。
    MySQL/PG 的 DATETIME 列 length() 恒 ≥19 ⇒ 本函数天然是 no-op（方言安全）。
    """
    ttl = stale_seconds if (stale_seconds and stale_seconds > 0) else max(600, LOCAL_TTL_SECONDS * 3)
    cutoff = _s(_now() - timedelta(seconds=ttl))
    db = SessionLocal()
    try:
        res = db.connection().exec_driver_sql(
            "DELETE FROM card_download_locks "
            "WHERE holder_type = 'local' AND lease_until IS NOT NULL "
            f"AND length(lease_until) < {_MIN_LEASE_LEN} "
            "AND heartbeat_at < :cutoff "
            "AND NOT EXISTS (SELECT 1 FROM local_tasks t "
            "                WHERE t.task_id = card_download_locks.task_id "
            "                  AND t.status IN ('pending','running'))",
            {"cutoff": cutoff},
        )
        n = res.rowcount
        db.commit()
        if n:
            logger.warning(f"清理 {n} 条「租约不可解析且已无对应任务」的孤儿下载槽"
                           f"（该库是否被手工改过 / 从别处恢复过？）")
        return n
    except Exception:
        # 表结构差异（如某些库无 local_tasks 同库视图）⇒ 只做保守跳过，不影响主流程
        logger.debug("reap_orphan_locks 跳过（谓词在该方言下不可用）", exc_info=True)
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
                "claim_session = NULL, claimed_at = NULL, heartbeat_at = NULL, lease_until = NULL, "
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
            # code：让客户端能区分「槽真的被别人占着」与「任务状态不可 claim」。
            # 历史上只有 409 + 一句中文，插件只能一刀切地中断下载。
            "code": "busy",
            "error": SLOT_BUSY_DETAIL,
            "detail": SLOT_BUSY_DETAIL,
            "holder": holder,
        },
    )
