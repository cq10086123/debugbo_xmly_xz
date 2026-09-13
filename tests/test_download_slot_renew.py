"""core/download_slot.renew() / 租约脏值 / SlotUnavailable 的单元回测（修复方案 §6 用例 1–7）

针对的 bug：
- A1「心跳晚一拍即被判租约失效」→ 过期但仍是本持有者时必须能原地续命；
- A2「锁行被 expire_stale 物理删除后无法自愈」→ allow_reacquire 用同一条条件 SQL 重新登记；
- A3「DB 抖动被翻译成 409」→ 必须抛 SlotUnavailable，由调用方转可重试语义；
- 脏 lease_until 值不得被当成「已过期」而把锁白送给人（fail-closed）。

同时锁死**不能被放宽**的两条不变式：
- I1：绝不抢走未过期持有者的锁（并发 reacquire 至多一个胜者）；
- I3/I4：claim 被他人改写后本机续约必须失败；heartbeat() 的严格语义必须保留
         （服务器任务/管理员强制释放后不能靠心跳复活）。

运行：./.venv-xz/bin/python -m pytest tests/test_download_slot_renew.py -v
"""
import os
import sys
import tempfile
import time
import uuid
from datetime import timedelta

import pytest

RUN = uuid.uuid4().hex[:8]
CODE1 = f"RNW-{RUN}-1"
CODE2 = f"RNW-{RUN}-2"

_TMP = tempfile.mkdtemp(prefix="slot_renew_")
os.environ["DATA_DIR"] = _TMP
os.environ["DOWNLOAD_SLOT_LOCAL_TTL"] = "300"
os.environ["DOWNLOAD_SLOT_SERVER_TTL"] = "120"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import Base, _engine, SessionLocal  # noqa: E402
from db.models import Card, LocalTask as LocalTaskQuery  # noqa: E402
from core import download_slot as ds  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(_engine)
    db = SessionLocal()
    try:
        c1 = Card(code=CODE1, status="active")
        c2 = Card(code=CODE2, status="active")
        db.add_all([c1, c2])
        db.commit()
        globals()["CARD1"], globals()["CARD2"] = c1.id, c2.id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean():
    db = SessionLocal()
    try:
        for cid in (CARD1, CARD2):
            db.connection().exec_driver_sql(
                "DELETE FROM card_download_locks WHERE card_id = :c", {"c": cid})
        db.commit()
    finally:
        db.close()
    yield


def _force_lease(card_id, when):
    """把锁行的 lease_until 改成指定时间/字符串（模拟「心跳断了很久」或脏值）。"""
    val = when if isinstance(when, str) else ds._s(when)
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET lease_until = :v WHERE card_id = :c",
            {"v": val, "c": card_id})
        db.commit()
    finally:
        db.close()


# ════════════════════════════════════════
#  用例 1：租约已过期，但仍是我持有 ⇒ 原地续命（不再中断下载）
# ════════════════════════════════════════
def test_renew_survives_expired_lease_when_still_owner():
    ds.acquire(CARD1, "local", "T1", claim_id="cl1")
    _force_lease(CARD1, ds._now() - timedelta(seconds=30))   # 过期 30s，无人抢占
    assert ds.get_lock(CARD1)["expired"] is True

    state, lock = ds.renew(CARD1, "T1", claim_id="cl1", require_live=False)
    assert state == "renewed", state
    assert lock and lock["task_id"] == "T1" and lock["expired"] is False

    # 严格模式（老行为）必须仍然拒绝 —— 证明放宽完全由调用方按需选择
    _force_lease(CARD1, ds._now() - timedelta(seconds=30))
    state2, _ = ds.renew(CARD1, "T1", claim_id="cl1", require_live=True)
    assert state2 == "lost"
    ds.release(CARD1, "T1")


# ════════════════════════════════════════
#  用例 2：过期期间已被他人抢占 ⇒ 绝不夺回（不变式 I1/I3）
# ════════════════════════════════════════
def test_renew_never_steals_from_new_holder():
    ds.acquire(CARD1, "local", "T_slow", claim_id="clSlow")
    _force_lease(CARD1, ds._now() - timedelta(seconds=1))
    ok, _ = ds.acquire(CARD1, "local", "T_new", claim_id="clNew")   # 别的设备抢到了
    assert ok

    for kwargs in ({}, {"allow_reacquire": True}):
        state, lock = ds.renew(CARD1, "T_slow", claim_id="clSlow",
                               require_live=False, **kwargs)
        assert state == "lost", f"过期持有者不得复活（{kwargs}）"
        assert lock["task_id"] == "T_new" and lock["claim_id"] == "clNew"
    ds.release(CARD1, "T_new")


# ════════════════════════════════════════
#  用例 3：他人 claim 过同一任务（claim_id 被改写）⇒ 旧凭证续约必须失败
# ════════════════════════════════════════
def test_renew_rejects_when_claim_superseded():
    ds.acquire(CARD1, "local", "T1", claim_id="clOld")
    # 另一设备 claim 同一任务（同 task_id 的幂等路径会改写 claim_id）
    ok, _ = ds.acquire(CARD1, "local", "T1", claim_id="clNew")
    assert ok
    state, lock = ds.renew(CARD1, "T1", claim_id="clOld", require_live=False)
    assert state == "lost"
    assert lock["claim_id"] == "clNew"
    # 新凭证续约成功
    state2, _ = ds.renew(CARD1, "T1", claim_id="clNew", require_live=False)
    assert state2 == "renewed"
    ds.release(CARD1, "T1")


# ════════════════════════════════════════
#  用例 4：锁行已被 expire_stale 删除 ⇒ allow_reacquire 重新登记；他人不能同时得手
# ════════════════════════════════════════
def test_reacquire_after_expire_stale_is_exclusive():
    ds.acquire(CARD1, "local", "T1", claim_id="cl1", ttl_seconds=1)
    time.sleep(1.2)
    assert ds.expire_stale() >= 1
    assert ds.get_lock(CARD1) is None

    # 无人的情况下：本机心跳带 allow_reacquire 能把自己重新登记回去
    state, lock = ds.renew(CARD1, "T1", claim_id="cl1", allow_reacquire=True)
    assert state == "renewed"
    assert lock["task_id"] == "T1"
    ds.release(CARD1, "T1")

    # 空槽时两个设备并发 reacquire 同一任务 ⇒ 至多一个胜者（同一条条件 SQL 保证）
    import threading
    results = []

    def beat(tag):
        st, lk = ds.renew(CARD1, f"T{tag}", claim_id=f"cl{tag}",
                          require_live=False, allow_reacquire=True)
        results.append((st, lk["task_id"]))

    ts = [threading.Thread(target=beat, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    winners = [r for r in results if r[0] == "renewed"]
    assert len(winners) == 1, f"并发续约必须唯一胜者：{results}"
    ds.release(CARD1, winners[0][1])


# ════════════════════════════════════════
#  用例 5：heartbeat() 的严格语义原样保留（服务器任务 / 管理员强释，I4）
# ════════════════════════════════════════
def test_heartbeat_strict_semantics_unchanged():
    ok, _ = ds.acquire(CARD1, "server", "SVR1", ttl_seconds=1)
    assert ok
    # 注意 ttl 显式传 1：不传则 heartbeat 会按 SERVER_TTL(120s) 续上，下一步就造不出「已过期」
    assert ds.heartbeat(CARD1, "SVR1", ttl_seconds=1)[0] is True   # 未过期：可续
    time.sleep(1.3)
    assert ds.heartbeat(CARD1, "SVR1", ttl_seconds=1)[0] is False  # 已过期：不能复活
    # 管理员强制释放后，旧持有者心跳必须失败
    ds.release(CARD1, "SVR1")
    assert ds.heartbeat(CARD1, "SVR1")[0] is False
    # 无锁时 heartbeat 也不许把槽抢回来
    assert ds.get_lock(CARD1) is None


# ════════════════════════════════════════
#  用例 6：DB 异常 ⇒ 抛 SlotUnavailable，绝不返回「lost/False」
# ════════════════════════════════════════
def test_db_error_raises_slot_unavailable(monkeypatch):
    ds.acquire(CARD1, "local", "T1", claim_id="cl1")
    from sqlalchemy.exc import OperationalError

    def boom(*a, **kw):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    from sqlalchemy.orm import Session as _S
    monkeypatch.setattr(_S, "connection", boom)
    with pytest.raises(ds.SlotUnavailable):
        ds.renew(CARD1, "T1", claim_id="cl1", require_live=False)
    with pytest.raises(ds.SlotUnavailable):
        ds.get_lock(CARD1, raise_on_error=True)
    with pytest.raises(ds.SlotUnavailable):
        ds.acquire(CARD1, "local", "T2", raise_on_error=True)

    # 但默认（best-effort）路径必须保持旧行为：不抛异常
    assert ds.get_lock(CARD1) is None
    assert ds.release(CARD1, "T1") is False
    ok, _snap = ds.acquire(CARD1, "local", "T2")
    assert ok is False
    # 薄封装 heartbeat 必须吞掉异常（老调用点 app.py/admin 不能因此崩）
    monkeypatch.undo()
    assert ds.heartbeat(CARD1, "T1")[0] is True
    ds.release(CARD1, "T1")


# ════════════════════════════════════════
#  用例 7：租约脏值 fail-closed —— 读不懂不许当过期、不许被抢、不许被清扫
# ════════════════════════════════════════
def test_unparsable_lease_is_fail_closed():
    ds.acquire(CARD1, "local", "T1", claim_id="cl1")
    _force_lease(CARD1, "not-a-timestamp")

    snap = ds.get_lock(CARD1)
    assert snap["lease_until"] is None and snap["lease_unparsable"] is True
    assert snap["expired"] is False, "读不懂的租约绝不能被标记为已过期"

    # 别的设备不能借「读不懂 = 过期」抢锁
    ok, lock = ds.acquire(CARD2, "local", "OTHER")   # 另一张卡，正常可写（对照组）
    assert ok
    ds.release(CARD2, "OTHER")
    ok2, _ = ds.acquire(CARD1, "server", "T_steal")
    assert ok2 is False, "脏值行必须不可被抢占"

    # 脏值行也不许被 expire_stale 顺手抹掉（保留事故现场）
    assert ds.expire_stale() == 0
    assert ds.get_lock(CARD1)["task_id"] == "T1"

    # 本持有者续约同样不去覆盖脏值行（谓词要求 lease_until 是可信格式或 NULL）
    state, _ = ds.renew(CARD1, "T1", claim_id="cl1", require_live=False, allow_reacquire=False)
    assert state == "lost"

    # 恢复正常格式后即可续
    _force_lease(CARD1, ds._s(ds._now() + timedelta(seconds=60)))
    assert ds.renew(CARD1, "T1", claim_id="cl1", require_live=False)[0] == "renewed"
    ds.release(CARD1, "T1")


# ════════════════════════════════════════
#  附：_lease_of 单元语义（NULL / datetime / 脏值 / 兼容格式）
# ════════════════════════════════════════
def test_lease_of_parsing():
    from datetime import datetime
    assert ds._lease_of(None) == (None, True)
    dt = datetime(2026, 9, 12, 1, 2, 3, 456)
    assert ds._lease_of(dt) == (dt, True)
    assert ds._lease_of(dt.astimezone()) == (dt, True), "带时区的值必须归一为 naive"
    assert ds._lease_of(ds._s(dt)) == (dt, True)
    assert ds._lease_of("2026-09-12 01:02:03") == (datetime(2026, 9, 12, 1, 2, 3), True)
    assert ds._lease_of("2026-09-12T01:02:03.000456") == (dt, True)
    assert ds._lease_of("garbage") == (None, False)
    assert ds._lease_of("") == (None, False)


# ════════════════════════════════════════
#  用例 9：脏租约的「永久占槽」死角 —— reap_orphan_locks
# ════════════════════════════════════════
def _mk_local_task(card_id, task_id, status="pending"):
    from db.models import LocalTask
    import json
    db = SessionLocal()
    try:
        db.add(LocalTask(task_id=task_id, card_id=card_id, source="third_party",
                         album_id="ALB-R", album_title="孤儿槽测试", quality=0, fmt="mp3",
                         tracks=json.dumps([{"track_id": "r-1", "episode_num": 1,
                                             "title": "t", "fmt": "mp3"}]),
                         status=status))
        db.commit()
    finally:
        db.close()


def _count_lock(card_id):
    db = SessionLocal()
    try:
        return db.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM card_download_locks WHERE card_id = :c", {"c": card_id}
        ).fetchone()[0]
    finally:
        db.close()


def test_reap_orphan_lock_only_when_task_is_gone():
    """脏 lease 的锁行既抢不走也扫不掉 ⇒ 必须有"任务已不存在"这条出路，但绝不清正在下载的锁。"""
    import time as _t
    tid = f"orph{RUN}"
    _mk_local_task(CARD1, tid, status="running")
    assert ds.acquire(CARD1, "local", tid, claim_id="c-orph")
    _force_lease(CARD1, "2020-01-01 00:00")          # 长度 <19 ⇒ 读不懂（fail-closed）
    assert _count_lock(CARD1) == 1

    # ① 任务还在下载中 ⇒ 即使心跳很旧也不许清（否则双设备同时落盘）
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET heartbeat_at = :h WHERE card_id = :c",
            {"h": ds._s(_t.time() and ds._now() - timedelta(days=2)), "c": CARD1})
        db.commit()
    finally:
        db.close()
    assert ds.reap_orphan_locks() == 0, "任务仍 running ⇒ 不能清锁"
    assert _count_lock(CARD1) == 1

    # ② 任务已终态（等价于"没人再持有它"）⇒ 允许回收，卡密才不会被永久 409
    db = SessionLocal()
    try:
        db.query(LocalTaskQuery).filter_by(task_id=tid).update({"status": "done"})
        db.commit()
    finally:
        db.close()
    assert ds.reap_orphan_locks() == 1, "脏租约 + 无进行中任务 ⇒ 必须能被回收"
    assert _count_lock(CARD1) == 0
    assert ds.acquire(CARD1, "local", "brand-new-task", claim_id="c-new")[0] is True, \
        "回收后该卡密必须能重新 claim（否则就是永久卡死）"


def test_reap_orphan_lock_is_noop_for_healthy_rows():
    """健康行（可解析租约）无论任务在不在都不该被这条 SQL 碰到 —— 那是 expire_stale 的活。"""
    tid = f"orph{RUN}b"
    _mk_local_task(CARD2, tid, status="done")
    assert ds.acquire(CARD2, "local", tid, claim_id="c-ok")
    assert ds.reap_orphan_locks() == 0
    assert _count_lock(CARD2) == 1
