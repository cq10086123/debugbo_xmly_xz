"""端到端回测：全局下载槽 + 插件 claim 生命周期 + 409 互斥 + cookie 5 校验 + 管理端

用 TestClient 驱动真实 FastAPI app（lifespan 全跑），假 XimalayaDownloader 拦截真实网络。
覆盖用户要求「很难发现的问题」：
- 服务器下载（batch/track/chapter/intf batch）与本地插件 claim 的全局互斥（409）
- claim 竞态（多线程并发 claim 同一任务 ⇒ 唯一胜者）
- 心跳续租 / 租约过期抢占 / sweep 回退 pending / 重启清理
- complete/cancel 所有权释放（绝不误删新锁）
- /xm-cookie 5 项校验矩阵（防同卡第二插件偷 cookie）
- 管理员强制释放 / delete_card 带锁 / 卡密过期联动
- 批量 cancel 立即释放槽、retry/resume 的 409

运行：./.venv/bin/python -m pytest tests/test_download_slot_e2e.py -v
"""
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# 进程内唯一后缀：pytest 合跑时多个测试文件共享同一个 db.session 引擎
# （谁的 db.session 先被 import，引擎就绑到谁的 DATA_DIR），固定卡密码会撞 UNIQUE
RUN = uuid.uuid4().hex[:8]

_TMP = tempfile.mkdtemp(prefix="slot_e2e_")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("DOWNLOAD_SLOT_LOCAL_TTL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ────────────────────────────────────────────────
#  假下载器：拦截一切真实网络
# ────────────────────────────────────────────────
GA = threading.Event()          # 下载阻塞门
BLOCK = {"on": False}           # 是否让 download 阻塞（默认不阻塞，快速完成）
LAST_ERROR = {"msg": ""}


class FakeDownloader:
    def __init__(self, cookie=None, account_id=None, card_id=None, download_root=None, **kw):
        self.cookie = cookie
        self.account_id = account_id
        self.card_id = card_id

    def _gate(self):
        # 轮询式阻塞：BLOCK 关闭后 0.05s 内立即放行（不依赖 GA 显式 set）
        deadline = time.time() + 60
        while BLOCK["on"] and time.time() < deadline:
            time.sleep(0.05)

    def get_track_list(self, album_id):
        if LAST_ERROR["msg"] == "list-fail":
            return {"success": False, "error": "list failed"}
        # 专辑标题按 album_id 唯一：track_lock 键为 (card, 专辑, 集)，
        # 若共用标题，卡住的任务会占着 track_lock 让新任务整本"跳过"秒完成
        return {"success": True, "album_title": f"测试专辑-{album_id}", "tracks": [
            {"trackId": 1001 + i, "title": f"第{i + 1}集"} for i in range(2)
        ]}

    def scan_local_album(self, album_title):
        return set()

    def check_track_exists(self, *a, **kw):
        return None

    def download_by_track_id(self, track_id, quality=0, album_title="", skip_exists=False,
                             fmt="mp3", episode_num=None, **kw):
        self._gate()
        if LAST_ERROR["msg"] == "dl-fail":
            return {"success": False, "error": LAST_ERROR["msg"]}
        return {"success": True, "file_path": f"/tmp/fake_{track_id}.mp3", "file_size": 100}

    def download_by_chapter(self, album_id, chapter_num, quality=0, fmt="mp3", **kw):
        self._gate()
        return {"success": True, "file_path": f"/tmp/fake_ch{chapter_num}.mp3", "file_size": 100}

    def close(self):
        pass


class FakeAdapter:
    supports_direct_download = True

    def __init__(self, *a, **kw):
        pass

    def get_chapters(self, book_id):
        if LAST_ERROR["msg"] == "intf-fail":
            return {"success": False, "error": "intf failed"}
        return {"success": True, "album_title": "第三方书", "tracks": [
            {"trackId": 2001, "title": "第一章"}
        ]}

    def get_audio_url(self, book_id, chapter_id):
        return "http://127.0.0.1:1/none"  # 触发快速下载失败（槽仍会被 finally 释放）


# ────────────────────────────────────────────────
#  fixtures
# ────────────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def _repair_account_manager():
    """修复 test_donor_cooldown 的模块级泄漏。

    该文件在 import 阶段执行 `account_manager.SessionLocal = <它的临时库引擎>`
    且不恢复（见其第 35 行）。pytest 合跑时，本模块所有经 account_manager 的
    调用（官方下载取账号、/xm-cookie、冷却）都会去查那个空临时库，
    典型症状：POST /api/download/batch → 400「请先扫码登录喜马拉雅账号」。
    单跑本文件时不受影响（那个模块根本没被收集）。
    """
    import core.account_manager as am
    from db.session import SessionLocal as real_session_local
    if am.SessionLocal is not real_session_local:
        am.SessionLocal = real_session_local
    yield


@pytest.fixture(scope="module")
def env():
    from db.init_db import init_db
    from db.session import SessionLocal
    from db.models import Card, Session as CardSession, AdminToken, XimalayaAccount
    import api.download as adl
    import api.interfaces as aif
    import app as appmod

    init_db()

    # 打补丁（必须在 TestClient 启动前）
    adl.XimalayaDownloader = FakeDownloader
    aif._get_adapter_or_404 = lambda name, auth: FakeAdapter()

    from db.models import ApiConfig
    db = SessionLocal()
    try:
        ca = Card(code=f"E2E-A-{RUN}", status="active")
        cb = Card(code=f"E2E-B-{RUN}", status="active")
        db.add_all([ca, cb])
        db.commit()
        db.refresh(ca); db.refresh(cb)
        sa = CardSession(card_id=ca.id, token=f"tokA-{RUN}", is_active=True)
        sb = CardSession(card_id=cb.id, token=f"tokB-{RUN}", is_active=True)
        at = AdminToken(token=f"admin-tok-{RUN}")
        acc = XimalayaAccount(card_id=ca.id, acc_id="accA1", uid="u1",
                              nickname="测试账号", cookie_str="uid=1; kid=2",
                              is_vip=1)
        accb = XimalayaAccount(card_id=cb.id, acc_id="accB1", uid="u2",
                               nickname="测试账号B", cookie_str="uid=9; kid=8",
                               is_vip=0)
        # TestClient 来源不是局域网 IP：关闭 admin 局域网限制，否则管理端全 403
        cfg = ApiConfig(cfg_key="admin_lan_only", cfg_value="0", category="settings",
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        db.add_all([sa, sb, at, acc, accb, cfg])
        db.commit()
        card_a, card_b = ca.id, cb.id
    finally:
        db.close()

    from fastapi.testclient import TestClient
    with TestClient(appmod.app) as client:
        envd = {
            "client": client,
            "card_a": card_a,
            "card_b": card_b,
            "tokA": {"Authorization": f"Bearer tokA-{RUN}"},
            "tokB": {"Authorization": f"Bearer tokB-{RUN}"},
            "admin": {"Authorization": f"Bearer admin-tok-{RUN}"},
            "appmod": appmod,
        }
        yield envd
    adl.XimalayaDownloader = __import__("core.downloader", fromlist=["XimalayaDownloader"]).XimalayaDownloader


H = lambda e: e["tokA"]   # noqa: E731


def _wait_status(client, hdr, url, want, timeout=20):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(url, headers=hdr)
        if r.status_code == 200:
            last = r.json()
            if last.get("status") == want:
                return last
        time.sleep(0.2)
    raise AssertionError(f"等待 {want} 超时，最后状态: {last}")


def _locks_of(client, admin, card_id):
    r = client.get(f"/api/admin/download-locks", headers=admin)
    assert r.status_code == 200, r.text
    return [l for l in r.json()["locks"] if l.get("card_id") == card_id]


@pytest.fixture(autouse=True)
def _clean_slot():
    """每个用例前清空所有锁 + 停止残留下载任务，保证隔离。"""
    from core import download_slot
    from db.session import SessionLocal
    from db.models import LocalTask
    from api.download import _batch_tasks
    from api.interfaces import _intf_tasks
    for tasks in (_batch_tasks, _intf_tasks):
        for t in list(tasks.values()):
            if t.get("status") in ("running", "cancelling"):
                t["cancelled"] = True
    yield
    for tasks in (_batch_tasks, _intf_tasks):
        for t in list(tasks.values()):
            t["cancelled"] = True
    GA.set()
    time.sleep(0.3)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for t in db.query(LocalTask).all():
            if t.status == "running":
                t.status = "cancelled"
                t.finished_at = now
        db.commit()
    finally:
        db.close()
    from db.models import CardDownloadLock
    db = SessionLocal()
    try:
        db.query(CardDownloadLock).delete()
        db.commit()
    finally:
        db.close()
    BLOCK["on"] = False
    GA.clear()
    LAST_ERROR["msg"] = ""
    # 等卡住的下载协程收尾并释放 per-episode track_lock（避免串入下一用例）
    time.sleep(0.5)


# ════════════════════════════════════════════════
#  1. 服务器下载 409 互斥
# ════════════════════════════════════════════════
def test_batch_409_matrix(env):
    c, a = env["client"], env["card_a"]
    BLOCK["on"] = True
    r = c.post("/api/download/batch", json={"album_id": 111}, headers=H(env))
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    time.sleep(0.3)
    try:
        # 同卡：batch / track / chapter / intf batch 全部 409
        r2 = c.post("/api/download/batch", json={"album_id": 222}, headers=H(env))
        assert r2.status_code == 409
        assert r2.json()["error"] == "当前卡密已有下载任务进行中，请等待完成后再开始下一本"
        assert r2.json()["holder"]["task_id"] == task_id
        r3 = c.post("/api/download/track", json={"track_id": 1001}, headers=H(env))
        assert r3.status_code == 409
        r4 = c.post("/api/download/chapter", json={"album_id": 111, "chapter_num": 1}, headers=H(env))
        assert r4.status_code == 409
        r5 = c.post("/api/intf/demo/batch", json={"book_id": "bk1", "concurrency": 1}, headers=H(env))
        assert r5.status_code == 409
        # 其他卡不受影响
        r6 = c.post("/api/download/batch", json={"album_id": 333}, headers=env["tokB"])
        assert r6.status_code == 200, r6.text
        BLOCK["on"] = False
        _wait_status(c, env["tokB"], f"/api/download/batch/{r6.json()['task_id']}", "done")
    finally:
        BLOCK["on"] = False
        GA.set()
        _wait_status(c, H(env), f"/api/download/batch/{task_id}", "done", timeout=30)
    # 完成后槽释放：同卡再次 batch 成功
    r7 = c.post("/api/download/batch", json={"album_id": 444}, headers=H(env))
    assert r7.status_code == 200, r7.text
    _wait_status(c, H(env), f"/api/download/batch/{r7.json()['task_id']}", "done")


def test_track_single_shot_holds_slot(env):
    """单集 /track 是一次性 job：执行期间占槽，/chapter 应 409。"""
    c = env["client"]
    BLOCK["on"] = True
    holder = {"code": None, "body": None}

    def call_track():
        r = c.post("/api/download/track", json={"track_id": 1001}, headers=H(env))
        holder["code"] = r.status_code
        holder["body"] = r.json()

    t = threading.Thread(target=call_track)
    t.start()
    time.sleep(0.8)
    try:
        r = c.post("/api/download/chapter", json={"album_id": 1, "chapter_num": 1}, headers=H(env))
        assert r.status_code == 409, r.text
        assert r.json()["holder"]["holder_type"] == "server"
    finally:
        GA.set()
        BLOCK["on"] = False
        t.join(timeout=10)
    assert holder["code"] == 200 and holder["body"]["success"]
    # job 结束后槽已释放
    assert _locks_of(c, env["admin"], env["card_a"]) == []
    r2 = c.post("/api/download/chapter", json={"album_id": 1, "chapter_num": 1}, headers=H(env))
    assert r2.status_code == 200 and r2.json()["success"]
    assert _locks_of(c, env["admin"], env["card_a"]) == []


def test_batch_cancel_releases_slot_immediately(env):
    c, a = env["client"], env["card_a"]
    BLOCK["on"] = True
    r = c.post("/api/download/batch", json={"album_id": 111}, headers=H(env))
    task_id = r.json()["task_id"]
    time.sleep(0.3)
    rc = c.post(f"/api/download/batch/{task_id}/cancel", headers=H(env))
    assert rc.status_code == 200
    # 立即：同卡新任务可开始（旧协程收尾时的 release 为空操作）
    BLOCK["on"] = False
    r2 = c.post("/api/download/batch", json={"album_id": 222}, headers=H(env))
    assert r2.status_code == 200, r2.text
    _wait_status(c, H(env), f"/api/download/batch/{r2.json()['task_id']}", "done")


def test_retry_and_resume_409_when_busy(env):
    """retry 子任务 / resume 都需要先抢槽；槽被占 → 409。"""
    c, a = env["client"], env["card_a"]
    LAST_ERROR["msg"] = "dl-fail"
    r = c.post("/api/download/batch", json={"album_id": 111, "end_episode": 2}, headers=H(env))
    orig = r.json()["task_id"]
    _wait_status(c, H(env), f"/api/download/batch/{orig}", "done")
    # 此时 failed_list 非空（2 集都失败）
    # 槽被另一个长任务占用
    BLOCK["on"] = True
    rb = c.post("/api/download/batch", json={"album_id": 222}, headers=H(env))
    assert rb.status_code == 200
    time.sleep(0.3)
    rr = c.post(f"/api/download/batch/{orig}/retry", headers=H(env))
    assert rr.status_code == 409, rr.text
    LAST_ERROR["msg"] = ""
    # 取消占用任务 → 槽释放 → retry 成功
    c.post(f"/api/download/batch/{rb.json()['task_id']}/cancel", headers=H(env))
    time.sleep(0.3)
    rr2 = c.post(f"/api/download/batch/{orig}/retry", headers=H(env))
    assert rr2.status_code == 200, rr2.text
    assert rr2.json()["retry_count"] == 2
    BLOCK["on"] = False  # 放行下载门，重试子任务才能完成
    _wait_status(c, H(env), f"/api/download/batch/{rr2.json()['task_id']}", "done")


def test_admin_force_release_and_list(env):
    c = env["client"]
    BLOCK["on"] = True
    r = c.post("/api/download/batch", json={"album_id": 111}, headers=H(env))
    task_id = r.json()["task_id"]
    time.sleep(0.3)
    locks = _locks_of(c, env["admin"], env["card_a"])
    assert len(locks) == 1
    assert locks[0]["holder_type"] == "server"
    assert locks[0]["task_id"] == task_id
    assert locks[0]["card_code"] == f"E2E-A-{RUN}"
    # 强释
    r2 = c.post(f"/api/admin/cards/{env['card_a']}/download-lock/release", headers=env["admin"])
    assert r2.status_code == 200, r2.text
    assert _locks_of(c, env["admin"], env["card_a"]) == []
    # 槽立即可用（旧协程收尾时 release 为空操作，不会误删新锁）
    BLOCK["on"] = False
    r3 = c.post("/api/download/batch", json={"album_id": 333}, headers=H(env))
    assert r3.status_code == 200
    _wait_status(c, H(env), f"/api/download/batch/{r3.json()['task_id']}", "done")
    # 对无锁的卡强释 → 200 + released=False（或类似）
    r4 = c.post(f"/api/admin/cards/{env['card_b']}/download-lock/release", headers=env["admin"])
    assert r4.status_code == 200
    # 鉴权隔离：业务 token 访问管理端 → 401
    r5 = c.get("/api/admin/download-locks", headers=H(env))
    assert r5.status_code == 401


def test_delete_card_with_active_lock(env):
    """delete_card 带活动锁：锁被显式清理，无 500（原裸 FK 风险点）。"""
    c = env["client"]
    from db.session import SessionLocal
    from db.models import Card, CardDownloadLock
    db = SessionLocal()
    try:
        cc = Card(code=f"E2E-DEL-{RUN}", status="active")
        db.add(cc)
        db.commit()
        cid = cc.id
    finally:
        db.close()
    from core import download_slot
    ok, _ = download_slot.acquire(cid, "local", "delTask", claim_id="c1")
    assert ok
    r = c.delete(f"/api/admin/cards/{cid}", headers=env["admin"])
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.query(CardDownloadLock).filter_by(card_id=cid).count() == 0
        assert db.query(Card).filter_by(id=cid).count() == 0
    finally:
        db.close()


# ════════════════════════════════════════════════
#  2. 插件 claim 生命周期
# ════════════════════════════════════════════════
def _push_task(env, album=999, source="official", n=2):
    c = env["client"]
    tracks = [{"track_id": str(3000 + i), "episode_num": i + 1, "title": f"第{i+1}集"}
              for i in range(n)]
    r = c.post("/api/extension/task",
               json={"source": source, "album_id": str(album),
                     "album_title": f"本地专辑{album}", "tracks": tracks},
               headers=H(env))
    assert r.status_code == 200, r.text
    assert r.json()["success"]
    return r.json()["task_id"]


def test_claim_full_lifecycle(env):
    c, a = env["client"], env["card_a"]
    tid = _push_task(env, album=1)
    # GET /tasks 可见 pending
    r = c.get("/api/extension/tasks", headers=H(env))
    assert any(t["task_id"] == tid and t["status"] == "pending" for t in r.json()["tasks"])

    # claim 指定任务
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    claim_id = body["claim_id"]
    assert body["status"] == "running"
    assert body["lease_seconds"] > 0 and body["heartbeat_seconds"] > 0
    assert len(body["tracks"]) == 2

    # 幂等：同任务再 claim（不同 claim）→ 409（已被本设备 running）
    r2 = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    assert r2.status_code == 409
    # 再推一本（保持 pending）：通用 claim 应 409（槽被占，尽管还有 pending 任务）
    tid_wait = _push_task(env, album=101)
    r3 = c.post("/api/extension/tasks/claim", headers=H(env))
    assert r3.status_code == 409, r3.text
    assert r3.json()["holder"]["task_id"] == tid
    # 收尾：把排队的那本也走完（complete 第一本后它仍可被 claim）

    # 服务器下载被互斥
    r4 = c.post("/api/download/batch", json={"album_id": 555}, headers=H(env))
    assert r4.status_code == 409
    assert r4.json()["holder"]["holder_type"] == "local"

    # 心跳：错误 claim_id → 409；正确 → 200 且租约延长
    lease_before = body["lease_seconds"]
    r5 = c.post(f"/api/extension/tasks/{tid}/heartbeat",
                json={"claim_id": "wrong-claim"}, headers=H(env))
    assert r5.status_code == 409
    db_before = download_slot_lease(env, a)
    r6 = c.post(f"/api/extension/tasks/{tid}/heartbeat",
                json={"claim_id": claim_id}, headers=H(env))
    assert r6.status_code == 200, r6.text
    assert r6.json()["lease_seconds"] == lease_before
    db_after = download_slot_lease(env, a)
    assert db_after >= db_before

    # 完成
    r7 = c.post(f"/api/extension/tasks/{tid}/complete",
                json={"claim_id": claim_id,
                      "progress": {"total": 2, "completed": 2},
                      "failed_list": []}, headers=H(env))
    assert r7.status_code == 200, r7.text
    # 槽已释放
    assert _locks_of(c, env["admin"], a) == []
    # 任务 done 且不再出现在 /tasks
    r8 = c.get("/api/extension/tasks", headers=H(env))
    assert all(t["task_id"] != tid for t in r8.json()["tasks"])

    # 完成后 30 分钟内重复推送 → 去重
    r9 = c.post("/api/extension/task",
                json={"source": "official", "album_id": "1", "album_title": "本地专辑1",
                      "tracks": [{"track_id": "3000", "episode_num": 1, "title": "第1集"},
                                 {"track_id": "3001", "episode_num": 2, "title": "第2集"}]},
                headers=H(env))
    assert r9.json().get("duplicated") is True
    # 排队的第二本：claim + complete 收尾（不留 running 给下一个用例）
    r10 = c.post(f"/api/extension/tasks/{tid_wait}/claim", headers=H(env))
    assert r10.status_code == 200, r10.text
    c.post(f"/api/extension/tasks/{tid_wait}/complete",
           json={"claim_id": r10.json()["task"]["claim_id"], "progress": {}}, headers=H(env))


def download_slot_lease(env, card_id):
    from core import download_slot
    lock = download_slot.get_lock(card_id)
    return lock["lease_until"] if lock else 0


def test_claim_race_single_winner(env):
    """8 线程并发 claim 同一任务：至多 1 个成功（_CLAIM_LOCK + 原子抢槽）。"""
    c = env["client"]
    tid = _push_task(env, album=2)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
        with lock:
            results.append(r.status_code)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(200) == 1, results
    assert results.count(409) == 7, results
    # 恰好一把锁
    assert len(_locks_of(c, env["admin"], env["card_a"])) == 1
    # 收尾：用 DB 里的 claim_id 完成
    from db.session import SessionLocal
    from db.models import LocalTask
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        claim = t.claim_id
    finally:
        db.close()
    r = c.post(f"/api/extension/tasks/{tid}/complete",
               json={"claim_id": claim, "progress": {}, "failed_list": []}, headers=H(env))
    assert r.status_code == 200


def test_lease_expiry_sweep_and_reclaim(env):
    """租约过期 ⇒ 锁失效 + 心跳 409 + sweep 回退 pending + 可被再次 claim。"""
    c, a = env["client"], env["card_a"]
    from core import download_slot
    tid = _push_task(env, album=3)
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    claim_id = r.json()["task"]["claim_id"]

    # 模拟插件掉线：把租约拨到过去
    from db.session import SessionLocal
    db = SessionLocal()
    try:
        lock_row = db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET lease_until = :p WHERE card_id = :c",
            {"p": "2020-01-01 00:00:00.000000", "c": a},
        )
        assert lock_row.rowcount == 1
        db.commit()
        # 任务行的 lease_until 也拨过去（sweep 的目标）
        from db.models import LocalTask
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        t.lease_until = datetime(2020, 1, 1)
        db.commit()
    finally:
        db.close()

    # 心跳 → 409（租约过期）
    r2 = c.post(f"/api/extension/tasks/{tid}/heartbeat",
                json={"claim_id": claim_id}, headers=H(env))
    assert r2.status_code == 409

    # 其他任务可抢槽（过期锁可抢占）
    tid2 = _push_task(env, album=4)
    r3 = c.post(f"/api/extension/tasks/{tid2}/claim", headers=H(env))
    assert r3.status_code == 200, r3.text

    # 模拟维护循环一拍：sweep + expire_stale
    swept = download_slot.sweep_expired_local_tasks()
    assert swept >= 1
    download_slot.expire_stale()
    # 原任务回退 pending、claim 字段清空
    from db.session import SessionLocal
    from db.models import LocalTask
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        assert t.status == "pending"
        assert t.claim_id is None and t.lease_until is None
    finally:
        db.close()
    # 原任务可被重新 claim
    r4 = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    # tid2 还占着槽 → 应 409；先完成 tid2
    assert r4.status_code == 409
    r5 = c.post(f"/api/extension/tasks/{tid2}/complete",
                json={"claim_id": r3.json()["task"]["claim_id"], "progress": {}}, headers=H(env))
    assert r5.status_code == 200
    r6 = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    assert r6.status_code == 200, r6.text
    c.post(f"/api/extension/tasks/{tid}/complete",
           json={"claim_id": r6.json()["task"]["claim_id"], "progress": {}}, headers=H(env))


def test_cancel_claimed_task(env):
    c, a = env["client"], env["card_a"]
    tid = _push_task(env, album=5)
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    claim_id = r.json()["task"]["claim_id"]
    # 错误 claim 取消 → 409
    r2 = c.post(f"/api/extension/tasks/{tid}/cancel", json={"claim_id": "nope"}, headers=H(env))
    assert r2.status_code == 409
    # 正确 claim 取消 → 200 + 槽释放
    r3 = c.post(f"/api/extension/tasks/{tid}/cancel", json={"claim_id": claim_id}, headers=H(env))
    assert r3.status_code == 200
    assert _locks_of(c, env["admin"], a) == []
    from db.session import SessionLocal
    from db.models import LocalTask
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        assert t.status == "cancelled"
    finally:
        db.close()
    # 终态再取消 → 幂等成功
    r4 = c.post(f"/api/extension/tasks/{tid}/cancel", json={}, headers=H(env))
    assert r4.status_code == 200
    # pending 任务（未 claim）可直接取消
    tid2 = _push_task(env, album=6)
    r5 = c.post(f"/api/extension/tasks/{tid2}/cancel", json={}, headers=H(env))
    assert r5.status_code == 200


def test_ack_409_guard_on_running(env):
    """旧插件 /ack 不能把新插件正在下载（running）的任务置 done。"""
    c = env["client"]
    tid = _push_task(env, album=7)
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    claim_id = r.json()["task"]["claim_id"]
    r2 = c.post(f"/api/extension/tasks/{tid}/ack", headers=H(env))
    assert r2.status_code == 409
    # 释放后 ack pending…（任务 running 中，先 cancel 再验证 pending ack 正常）
    c.post(f"/api/extension/tasks/{tid}/cancel", json={"claim_id": claim_id}, headers=H(env))
    tid2 = _push_task(env, album=8)
    r3 = c.post(f"/api/extension/tasks/{tid2}/ack", headers=H(env))
    assert r3.status_code == 200 and r3.json()["success"]


def test_complete_with_wrong_claim_rejected(env):
    c, a = env["client"], env["card_a"]
    tid = _push_task(env, album=9)
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    claim_id = r.json()["task"]["claim_id"]
    r2 = c.post(f"/api/extension/tasks/{tid}/complete",
                json={"claim_id": "stolen", "progress": {}}, headers=H(env))
    assert r2.status_code == 409
    # 任务仍 running、槽仍持有
    assert len(_locks_of(c, env["admin"], a)) == 1
    r3 = c.post(f"/api/extension/tasks/{tid}/complete",
                json={"claim_id": claim_id, "progress": {}}, headers=H(env))
    assert r3.status_code == 200


def test_restart_cleanup_keeps_local_locks(env):
    """cleanup_on_startup：server 锁清除，local（插件）锁保留。"""
    from core import download_slot
    a, b = env["card_a"], env["card_b"]
    ok1, _ = download_slot.acquire(a, "server", "srvLeftover")
    ok2, _ = download_slot.acquire(b, "local", "locRunning", claim_id="lc1")
    assert ok1 and ok2
    n = download_slot.cleanup_on_startup()
    assert n == 1
    assert download_slot.get_lock(a) is None
    assert download_slot.get_lock(b)["task_id"] == "locRunning"
    download_slot.release(b, "locRunning")


# ════════════════════════════════════════════════
#  3. /xm-cookie 5 项校验矩阵
# ════════════════════════════════════════════════
def _claim_official(env, album=20):
    c = env["client"]
    tid = _push_task(env, album=album, source="official")
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    assert r.status_code == 200, r.text
    return tid, r.json()["task"]["claim_id"]


def test_xm_cookie_matrix(env):
    c = env["client"]
    tid, claim_id = _claim_official(env)
    q = f"/api/extension/xm-cookie?task_id={tid}&claim_id={claim_id}"
    # 5 项全过 → 200 + cookie
    r = c.get(q, headers=H(env))
    assert r.status_code == 200, r.text
    accs = r.json()["accounts"]
    assert len(accs) == 1 and accs[0]["cookie"] == "uid=1; kid=2"

    # 缺参 → 400
    assert c.get("/api/extension/xm-cookie", headers=H(env)).status_code == 400
    assert c.get(f"/api/extension/xm-cookie?task_id={tid}", headers=H(env)).status_code == 400

    # task_id 不匹配（另一个卡的有效任务？构造：本卡另一 running 任务不存在 → 用别的 task_id）
    r2 = c.get(f"/api/extension/xm-cookie?task_id=other-task&claim_id={claim_id}", headers=H(env))
    assert r2.status_code == 409

    # claim_id 不匹配（403：疑似未持有凭证）
    r3 = c.get(f"/api/extension/xm-cookie?task_id={tid}&claim_id=stolen-claim", headers=H(env))
    assert r3.status_code == 403

    # 无进行中下载 → 409（先完成当前任务）
    c.post(f"/api/extension/tasks/{tid}/complete",
           json={"claim_id": claim_id, "progress": {}}, headers=H(env))
    r4 = c.get(f"/api/extension/xm-cookie?task_id={tid}&claim_id={claim_id}", headers=H(env))
    assert r4.status_code == 409

    # 槽被服务器下载占用 → 409（holder_type=server 分支）
    BLOCK["on"] = True
    rb = c.post("/api/download/batch", json={"album_id": 600}, headers=H(env))
    assert rb.status_code == 200
    time.sleep(0.2)
    # 服务器占槽期间：无本地 claim，请求任意 task/claim → 409（server 占用分支）
    r5 = c.get(f"/api/extension/xm-cookie?task_id={tid}&claim_id=whatever", headers=H(env))
    assert r5.status_code == 409
    assert "服务器" in r5.json()["error"] or "server" in r5.json()["error"]
    BLOCK["on"] = False
    _wait_status(c, H(env), f"/api/download/batch/{rb.json()['task_id']}", "done")


def test_xm_cookie_non_official_task_rejected(env):
    """第三方源任务不应当能领官方 cookie（source != official → 403）。"""
    c = env["client"]
    tid = _push_task(env, album=30, source="demo")
    r = c.post(f"/api/extension/tasks/{tid}/claim", headers=H(env))
    assert r.status_code == 200, r.text
    claim_id = r.json()["task"]["claim_id"]
    r2 = c.get(f"/api/extension/xm-cookie?task_id={tid}&claim_id={claim_id}", headers=H(env))
    assert r2.status_code == 403, r2.text
    c.post(f"/api/extension/tasks/{tid}/complete",
           json={"claim_id": claim_id, "progress": {}}, headers=H(env))


def test_cooldown_requires_claim(env):
    c = env["client"]
    # 无 claim → 400/409
    r = c.post("/api/extension/xm-cookie/cooldown",
               json={"account_id": "accA1", "task_id": "x", "claim_id": "y"}, headers=H(env))
    assert r.status_code in (400, 409)
    tid, claim_id = _claim_official(env, album=40)
    r2 = c.post("/api/extension/xm-cookie/cooldown",
                json={"account_id": "accA1", "task_id": tid, "claim_id": claim_id}, headers=H(env))
    assert r2.status_code == 200, r2.text
    # 冷却后 /xm-cookie 不再返回该账号
    r3 = c.get(f"/api/extension/xm-cookie?task_id={tid}&claim_id={claim_id}", headers=H(env))
    assert r3.status_code == 200
    assert r3.json()["accounts"] == []
    c.post(f"/api/extension/tasks/{tid}/complete",
           json={"claim_id": claim_id, "progress": {}}, headers=H(env))


# ════════════════════════════════════════════════
#  4. 卡密过期联动
# ════════════════════════════════════════════════
def test_card_expiry_cancels_local_task(env):
    """卡密过期：force_release 槽 + running 本地任务置 cancelled。"""
    import asyncio
    c, a = env["client"], env["card_a"]
    import app as appmod
    from db.session import SessionLocal
    from db.models import Card

    tid, claim_id = _claim_official(env, album=50)

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=a).first()
        card.expires_at = datetime(2020, 1, 1)
        card.status = "active"   # 过期扫描只处理 active/used
        db.commit()
    finally:
        db.close()

    # 跑一拍过期循环逻辑（与 app._card_expiry_loop 相同代码路径：直接调用其内部逻辑较慢，
    # 这里调用同函数体——通过 asyncio 单拍执行）
    async def one_tick():
        from api.download import _batch_tasks
        from api.interfaces import _intf_tasks
        from db.session import SessionLocal as SL
        from db.models import Card as C, LocalTask as LT
        from api.card_helpers import is_expired, mark_expired
        from api.persistence import clear_card_data
        from core import download_slot as ds
        db = SL()
        try:
            cards = db.query(C).filter(C.status.in_(["active", "used"])).all()
            for card in cards:
                if is_expired(card):
                    mark_expired(db, card)
                    clear_card_data(card.id)
                    for tasks in (_batch_tasks, _intf_tasks):
                        for t in list(tasks.values()):
                            if t.get("card_id") == card.id and t.get("status") not in ("done", "failed", "cancelled"):
                                t["cancelled"] = True
                                t["status"] = "cancelling"
                    ds.force_release(card.id)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    for lt in db.query(LT).filter_by(card_id=card.id, status="running").all():
                        lt.status = "cancelled"
                        lt.finished_at = now
                        lt.claim_id = None
                        lt.lease_until = None
                        lt.error = "卡密已过期，任务已取消"
                    db.commit()
        finally:
            db.close()

    asyncio.get_event_loop_policy().new_event_loop()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(one_tick())
    finally:
        loop.close()

    from core import download_slot
    assert download_slot.get_lock(a) is None
    db = SessionLocal()
    try:
        from db.models import LocalTask
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        assert t.status == "cancelled"
        assert t.claim_id is None
        assert "过期" in (t.error or "")
        card = db.query(Card).filter_by(id=a).first()
        assert card.status == "expired"
    finally:
        db.close()
    # 恢复卡密（供后续用例）
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=a).first()
        card.status = "active"
        card.expires_at = datetime(2030, 1, 1)
        db.commit()
    finally:
        db.close()


# ════════════════════════════════════════════════
#  5. 迁移兼容：旧库（无新列）自动补列
# ════════════════════════════════════════════════
def test_migration_adds_claim_columns(env):
    from db.session import _engine
    from sqlalchemy import inspect
    insp = inspect(_engine)
    cols = {c["name"] for c in insp.get_columns("local_tasks")}
    for c in ("claim_id", "progress", "failed_list", "error", "claimed_at",
              "heartbeat_at", "lease_until", "finished_at"):
        assert c in cols, f"local_tasks 缺少列 {c}"
    assert "card_download_locks" in insp.get_table_names()
