"""核心单元回测：core/download_slot.py

覆盖「很难发现的问题」类别：
1. 并发抢占原子性（多线程 + 多连接：同一卡密 N 个并发 acquire 至多一个成功）
2. 租约过期抢占（过期后他人可抢；未过期不可抢）
3. 心跳所有权校验（旧持有者不能续约/释放新持有者的锁）
4. 释放所有权校验（complete/cancel 的 release 绝不误删新锁）
5. sweep 与 complete 的竞态（条件写入：complete 先提交 ⇒ sweep 0 行）
6. 启动清理（server 锁清除、local 锁保留）
7. ORM 写入的 datetime 与原始 SQL 比较格式一致性（SQLite 字符串比较坑）

运行：DATA_DIR=/tmp/... pytest tests/test_download_slot_unit.py -v
"""
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta, timezone

import pytest

# 进程内唯一后缀：pytest 合跑时多个测试文件可能共享同一个 db.session 引擎
RUN = uuid.uuid4().hex[:8]
CODE1 = f"XM-UNIT-{RUN}-1"
CODE2 = f"XM-UNIT-{RUN}-2"

# ── 必须在导入 db.* 之前设置 DATA_DIR（db.session 模块级读取）──
_TMP = tempfile.mkdtemp(prefix="slot_unit_")
os.environ["DATA_DIR"] = _TMP
os.environ["DOWNLOAD_SLOT_LOCAL_TTL"] = "300"
os.environ["DOWNLOAD_SLOT_SERVER_TTL"] = "120"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import Base, _engine, SessionLocal  # noqa: E402
from db.models import Card, CardDownloadLock, LocalTask  # noqa: E402
from core import download_slot as ds  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(_engine)
    # 建两张卡
    db = SessionLocal()
    try:
        c1 = Card(code=CODE1, status="active")
        c2 = Card(code=CODE2, status="active")
        db.add_all([c1, c2])
        db.commit()
        CARD1, CARD2 = c1.id, c2.id
    finally:
        db.close()
    globals()["CARD1"], globals()["CARD2"] = CARD1, CARD2
    yield


def _now_utc():
    return datetime_now()


def datetime_now():
    return __import__("datetime").datetime.now(timezone.utc).replace(tzinfo=None)


# ════════════════════════════════════════
#  基础语义
# ════════════════════════════════════════
def test_acquire_release_basic():
    card = CARD1
    ds.release(card, "cleanup")
    ok, lock = ds.acquire(card, "local", "taskA", claim_id="cl1")
    assert ok and lock and lock["task_id"] == "taskA" and lock["holder_type"] == "local"
    assert not lock["expired"]
    # 重复 acquire（不同任务）→ 失败，锁不变
    ok2, lock2 = ds.acquire(card, "server", "taskB")
    assert not ok2 and lock2["task_id"] == "taskA"
    # 同一任务幂等 acquire → 成功（续约）
    ok3, lock3 = ds.acquire(card, "local", "taskA", claim_id="cl1")
    assert ok3 and lock3["task_id"] == "taskA"
    # 释放（所有权正确）
    assert ds.release(card, "taskA") is True
    assert ds.get_lock(card) is None
    # 再释放 → False
    assert ds.release(card, "taskA") is False


def test_lease_expiry_preemption():
    card = CARD2
    ds.release(card, "cleanup")
    ok, _ = ds.acquire(card, "local", "oldTask", ttl_seconds=1)
    assert ok
    time.sleep(1.2)
    lock = ds.get_lock(card)
    assert lock and lock["expired"]
    # 过期 → 可抢占
    ok2, lock2 = ds.acquire(card, "server", "newTask")
    assert ok2 and lock2["task_id"] == "newTask"
    # 未过期 → 不可抢
    ok3, lock3 = ds.acquire(card, "local", "otherTask")
    assert not ok3 and lock3["task_id"] == "newTask"
    ds.release(card, "newTask")


def test_heartbeat_ownership():
    card = CARD1
    ds.release(card, "cleanup")
    ds.acquire(card, "server", "T1", ttl_seconds=60)
    # 正确持有者续约成功
    ok, _ = ds.heartbeat(card, "T1")
    assert ok
    # 其他任务不能续约
    ok2, _ = ds.heartbeat(card, "T2")
    assert not ok2
    # 过期后即使是原持有者也不能续约（必须重新 acquire）
    ds.acquire(card, "server", "T1", ttl_seconds=1)
    time.sleep(1.2)
    ok3, _ = ds.heartbeat(card, "T1")
    assert not ok3
    ds.release(card, "T1")


def test_release_ownership_never_deletes_new_lock():
    """关键回归：旧任务 complete/cancel 的 release 绝不能误删新任务的锁。"""
    card = CARD2
    ds.release(card, "cleanup")
    ds.acquire(card, "local", "oldClaim", ttl_seconds=1)
    time.sleep(1.2)
    # 锁过期被新任务抢占
    ok, _ = ds.acquire(card, "server", "newClaim")
    assert ok
    # 旧任务此时才 release（模拟迟到的 complete）→ 必须空操作
    assert ds.release(card, "oldClaim") is False
    assert ds.get_lock(card)["task_id"] == "newClaim"
    ds.release(card, "newClaim")


def test_concurrent_acquire_single_winner():
    """N 个线程并发抢同一卡密的槽：至多 1 个成功（原子性验证）。"""
    card = CARD1
    ds.release(card, "cleanup")
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker(i):
        barrier.wait()
        ok, _ = ds.acquire(card, "local", f"task{i}")
        with lock:
            results.append((i, ok))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [i for i, ok in results if ok]
    assert len(winners) == 1, f"并发抢占出现多胜者: {results}"
    assert ds.release(card, f"task{winners[0]}") is True


def test_sweep_vs_complete_race():
    """sweep 的条件写入：任务 complete 后（status=done）sweep 不得改回 pending。"""
    card = CARD2
    ds.release(card, "cleanup")
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(card_id=card).delete()
        t = LocalTask(
            task_id="race1", card_id=card, source="official",
            album_id="1", tracks="[]", status="running",
            claim_id="claimX",
            lease_until=datetime_now() - timedelta(seconds=1),  # 已过期
        )
        db.add(t)
        db.commit()
        row_id = t.id
    finally:
        db.close()

    # 场景 1：complete 先提交 → sweep 影响 0 行
    db = SessionLocal()
    try:
        t2 = db.query(LocalTask).filter_by(id=row_id).first()
        t2.status = "done"
        t2.claim_id = None
        t2.lease_until = None
        db.commit()
    finally:
        db.close()
    n = ds.sweep_expired_local_tasks()
    assert n == 0
    db = SessionLocal()
    try:
        t3 = db.query(LocalTask).filter_by(id=row_id).first()
        assert t3.status == "done"
    finally:
        db.close()

    # 场景 2：任务仍 running 且过期 → sweep 生效
    db = SessionLocal()
    try:
        t4 = db.query(LocalTask).filter_by(id=row_id).first()
        t4.status = "running"
        t4.claim_id = "claimX"
        t4.lease_until = datetime_now() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    n2 = ds.sweep_expired_local_tasks()
    assert n2 == 1
    db = SessionLocal()
    try:
        t5 = db.query(LocalTask).filter_by(id=row_id).first()
        assert t5.status == "pending" and t5.claim_id is None and t5.lease_until is None
    finally:
        db.close()


def test_datetime_format_consistency():
    """ORM(DateTime) 写入的存储格式必须与原始 SQL 的字符串比较兼容（字典序=时间序）。"""
    card = CARD1
    db = SessionLocal()
    try:
        t = LocalTask(task_id="fmt1", card_id=card, source="official", album_id="1",
                      tracks="[]", status="pending",
                      lease_until=datetime_now() + timedelta(seconds=300))
        db.add(t)
        db.commit()
        db.refresh(t)
        raw = db.connection().exec_driver_sql(
            "SELECT lease_until FROM local_tasks WHERE task_id = 'fmt1'"
        ).fetchone()[0]
        future_s = (datetime_now() + timedelta(seconds=300)).strftime(ds._FMT)
        assert raw < future_s, f"ORM 存储格式 {raw!r} 与 %s 格式不可比" % ds._FMT
    finally:
        db.close()
    ds.release(card, "x")


def test_cleanup_on_startup_keeps_local_locks():
    card1, card2 = CARD1, CARD2
    ds.release(card1, "cleanup")
    ds.release(card2, "cleanup")
    ds.acquire(card1, "server", "srv1")
    ds.acquire(card2, "local", "loc1")
    n = ds.cleanup_on_startup()
    assert n == 1
    assert ds.get_lock(card1) is None
    assert ds.get_lock(card2) and ds.get_lock(card2)["task_id"] == "loc1"
    ds.release(card2, "loc1")


def test_force_release_and_list():
    card = CARD1
    ds.release(card, "cleanup")
    ok, _ = ds.acquire(card, "local", "adm1", claim_id="c1")
    assert ok
    locks = ds.list_locks()
    mine = [l for l in locks if l["card_id"] == card]
    assert mine and mine[0]["card_code"] == CODE1
    holder = ds.force_release(card)
    assert holder and holder["task_id"] == "adm1"
    assert ds.get_lock(card) is None
    assert ds.force_release(card) is None


def test_busy_response_shape():
    resp = ds.busy_response({"holder_type": "local", "task_id": "x"})
    assert resp.status_code == 409
    import json
    body = json.loads(resp.body)
    assert body["error"] == ds.SLOT_BUSY_DETAIL
    assert body["holder"]["task_id"] == "x"
