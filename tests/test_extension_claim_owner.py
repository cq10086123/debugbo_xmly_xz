"""扩展 API 的「归属可见 + 心跳三态」回测（修复方案 §6 用例 8–15）

盯的是两个用户可见症状的服务端半边：
- 「其他设备正在下载此任务」误报：插件此前只能靠「这条 running 任务不在我的 claimState 里」
  瞎猜；服务端补 mine / claimed / owner 后，插件有权威依据（F3）。
- 「下载中断（租约失效）」：心跳必须区分【已过期但仍是我】/【被清扫重新排队】/
  【真被接管】/【服务器读不到状态】四件事，后两件才允许中断下载（F1）。

同时锁死兼容性：老插件（0.7.1）只认 HTTP 状态码，所以
- 基础设施异常默认必须仍是 200（degraded），不能是 503；
- 既有响应字段（success/lease_seconds/lease_until/message）一个都不能少；
- 绝不能在 /tasks 响应里泄露 claim_id（否则同卡第二插件可拿它偷 cookie）。

运行：./.venv-xz/bin/python -m pytest tests/test_extension_claim_owner.py -v
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

RUN = uuid.uuid4().hex[:8]

_TMP = tempfile.mkdtemp(prefix="claim_owner_")
os.environ["DATA_DIR"] = _TMP
os.environ["DOWNLOAD_SLOT_LOCAL_TTL"] = "300"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def env():
    """只装 extension 生命周期所需的端点，不碰真实网络（本组用例不需要下载器）。"""
    from db.init_db import init_db
    from db.session import SessionLocal
    from db.models import Card, Session as CardSession, ApiConfig

    init_db()

    db = SessionLocal()
    try:
        card = Card(code=f"OWN-{RUN}", status="active")
        db.add(card)
        db.commit()
        db.refresh(card)
        # 同一张卡密下的两个会话（= 同局域网两台设备用同一卡密；另一张卡密作对照组）
        s1 = CardSession(card_id=card.id, token=f"tokA-{RUN}", is_active=True,
                         client_type="extension", device_id="", ip="192.168.1.11")
        s2 = CardSession(card_id=card.id, token=f"tokB-{RUN}", is_active=True,
                         client_type="web", device_id="", ip="192.168.1.12")
        db.add_all([s1, s2])
        # 合跑时 pytest 只有一份 DATA_DIR 库（见 tests/conftest.py 的说明），
        # admin_lan_only 可能已被其他用例建过 ⇒ 有则改、无则插，不能直接 INSERT 撞 UNIQUE
        cfg = db.query(ApiConfig).filter_by(cfg_key="admin_lan_only").first()
        if cfg is None:
            db.add(ApiConfig(cfg_key="admin_lan_only", cfg_value="0", category="settings",
                             updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        else:
            cfg.cfg_value = "0"
        db.commit()
        card_id = card.id
    finally:
        db.close()

    from fastapi.testclient import TestClient
    import app as appmod
    # 其他用例可能把全局下载槽 TTL 改过（合跑共享进程），本组用例只依赖「同一进程内一致」
    with TestClient(appmod.app) as client:
        yield {
            "client": client,
            "card_id": card_id,
            "A": {"Authorization": f"Bearer tokA-{RUN}"},
            "B": {"Authorization": f"Bearer tokB-{RUN}"},
        }


def _mk_task(env, task_id, source="third_party", n_tracks=2, status="pending"):
    """直接落库一条本地任务（走 create_local_task 会牵进接口/账号解析，本组用例不需要）。"""
    import json
    from db.session import SessionLocal
    from db.models import LocalTask
    db = SessionLocal()
    try:
        db.add(LocalTask(
            task_id=task_id, card_id=env["card_id"], source=source,
            album_id=f"alb-{task_id}", album_title=f"书名-{task_id}",
            quality=0, fmt="mp3", status=status,
            tracks=json.dumps([{"ep": i + 1, "track_id": 1000 + i, "title": f"第{i + 1}集"}
                               for i in range(n_tracks)]),
        ))
        db.commit()
    finally:
        db.close()


def _cleanup_locks(env):
    from db.session import SessionLocal
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql(
            "DELETE FROM card_download_locks WHERE card_id = :c", {"c": env["card_id"]})
        db.connection().exec_driver_sql(
            "DELETE FROM local_tasks WHERE card_id = :c", {"c": env["card_id"]})
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean(env):
    _cleanup_locks(env)
    yield
    _cleanup_locks(env)


def _hash(tok):
    import hashlib
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


# ════════════════════════════════════════
#  用例 8：claim 写归属 + /tasks 回 mine=true，且不泄露 claim_id
# ════════════════════════════════════════
def test_claim_records_session_and_marks_mine(env):
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T8")
    r = env["client"].post("/api/extension/tasks/T8/claim", headers=env["A"])
    assert r.status_code == 200, r.text
    body = r.json()
    claim_id = body["task"]["claim_id"]
    # 下发给插件的新增字段：心跳节奏（老插件忽略未知字段即可）
    assert body["task"]["mine"] is True
    assert 5 <= body["task"]["beat_seconds"] <= body["task"]["lease_seconds"] // 4, "心跳节奏必须与租约联动"
    assert body["task"]["grace_seconds"] >= 60
    # 既有字段必须原样在（老插件依赖）
    assert body["task"]["lease_seconds"] == ds.LOCAL_TTL_SECONDS
    assert body["task"]["tracks"] and body["task"]["status"] == "running"

    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T8").first()
        assert t.claim_session == _hash(f"tokA-{RUN}"), "claim 必须记下会话指纹（只存哈希，不存 token）"
    finally:
        db.close()

    items = env["client"].get("/api/extension/tasks", headers=env["A"]).json()["tasks"]
    t8 = [x for x in items if x["task_id"] == "T8"][0]
    assert t8["mine"] is True and t8["claimed"] is True
    assert t8["owner"]["client"] == "extension"
    assert "192.168.1.11" in t8["owner"]["label"]
    assert "claim_id" not in t8, "/tasks 绝不能把 claim_id 发给非持有者（可被用于偷 cookie）"


# ════════════════════════════════════════
#  用例 9：同卡另一会话看到的必须是 mine=false（这才是「别的设备」的权威依据）
# ════════════════════════════════════════
def test_other_session_sees_not_mine(env):
    _mk_task(env, "T9")
    r = env["client"].post("/api/extension/tasks/T9/claim", headers=env["A"])
    assert r.status_code == 200, r.text

    items = env["client"].get("/api/extension/tasks", headers=env["B"]).json()["tasks"]
    t9 = [x for x in items if x["task_id"] == "T9"][0]
    assert t9["claimed"] is True and t9["mine"] is False
    assert t9["owner"]["client"] == "extension" and t9["owner"]["label"] != "网页"
    assert "claim_id" not in t9

    # 另一会话尝试取消 → 必须被拒（所有权校验不许放宽），但文案不再谎报「其他设备在下载」
    rc = env["client"].post("/api/extension/tasks/T9/cancel", headers=env["B"],
                            json={"claim_id": "whatever"})
    assert rc.status_code == 409 and rc.json()["code"] == "claim_invalid"


# ════════════════════════════════════════
#  用例 10：存量数据（claim_session 为 NULL）必须 fail-open 成 mine=true
#  —— 否则升级那一刻，正在下载的设备会立刻把自己的书判成「别人在下」
# ════════════════════════════════════════
def test_legacy_row_without_session_is_fail_open(env):
    from db.session import SessionLocal
    from db.models import LocalTask
    _mk_task(env, "T10")
    env["client"].post("/api/extension/tasks/T10/claim", headers=env["A"])
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T10").first()
        t.claim_session = None       # 模拟升级前已 claim 的存量行
        db.commit()
    finally:
        db.close()

    for hdr in (env["A"], env["B"]):
        items = env["client"].get("/api/extension/tasks", headers=hdr).json()["tasks"]
        t10 = [x for x in items if x["task_id"] == "T10"][0]
        assert t10["claimed"] is True and t10["mine"] is True, "归属未知 ⇒ 一律按「是本机」，不许冻结"
        assert t10["owner"] is None


# ════════════════════════════════════════
#  用例 11：租约已过期但 claim 未变 ⇒ 心跳原地续命（不再「下载中断（租约失效）」）
# ════════════════════════════════════════
def test_heartbeat_recovers_expired_lease_for_same_owner(env):
    from datetime import timezone as tz
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T11")
    r = env["client"].post("/api/extension/tasks/T11/claim", headers=env["A"])
    claim = r.json()["task"]["claim_id"]

    # 模拟「插件被挂起 / 心跳迟到一个租约周期」：任务侧与槽侧的 lease_until 都推到过去
    past = datetime.now(tz.utc).replace(tzinfo=None) - timedelta(seconds=90)
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(task_id="T11").update({"lease_until": past})
        db.commit()
        db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET lease_until = :v WHERE card_id = :c",
            {"v": ds._s(past), "c": env["card_id"]})
        db.commit()
    finally:
        db.close()
    assert ds.get_lock(env["card_id"])["expired"] is True

    hb = env["client"].post("/api/extension/tasks/T11/heartbeat", headers=env["A"],
                            json={"claim_id": claim})
    assert hb.status_code == 200, hb.text
    body = hb.json()
    assert body["success"] is True and body.get("degraded") is None
    assert body["mine"] is True
    assert body["renewed_after_expiry"] is True, "应记录「过期后原地续租」用于观测放宽是否够用"
    assert body["lease_until"], "既有字段必须保留（老插件读它）"
    assert ds.get_lock(env["card_id"])["expired"] is False
    assert ds.get_lock(env["card_id"])["task_id"] == "T11"


# ════════════════════════════════════════
#  用例 12：真被别的会话接管（claim_id 已改写）⇒ 409 code=lost，插件才允许中断
# ════════════════════════════════════════
def test_heartbeat_lost_after_real_takeover(env):
    from datetime import timezone as tz
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T12")
    old_claim = env["client"].post("/api/extension/tasks/T12/claim", headers=env["A"]
                                   ).json()["task"]["claim_id"]

    # 真实接管路径：A 掉线 → 租约过期被清扫打回 pending → B claim 到同一本
    from datetime import timezone as tz
    from db.session import SessionLocal as _SL
    from db.models import LocalTask as _LT
    from core import download_slot as _ds
    past = datetime.now(tz.utc).replace(tzinfo=None) - timedelta(seconds=600)
    db = _SL()
    try:
        db.query(_LT).filter_by(task_id="T12").update({"lease_until": past})
        db.commit()
    finally:
        db.close()
    assert _ds.sweep_expired_local_tasks() >= 1
    new_claim = env["client"].post("/api/extension/tasks/T12/claim", headers=env["B"]
                                   ).json()["task"]["claim_id"]
    assert new_claim != old_claim

    # 旧设备即使带着自己的凭证来心跳（且槽确实活着），也必须被判 lost —— 不许放宽这一条
    hb = env["client"].post("/api/extension/tasks/T12/heartbeat", headers=env["A"],
                            json={"claim_id": old_claim})
    assert hb.status_code == 409 and hb.json()["code"] == "lost"

    # 新持有者续约正常
    assert env["client"].post("/api/extension/tasks/T12/heartbeat", headers=env["B"],
                              json={"claim_id": new_claim}).json()["success"] is True
    assert ds.get_lock(env["card_id"])["task_id"] == "T12"


# ════════════════════════════════════════
#  用例 13：DB 抖动 ⇒ 200 degraded（老插件因此不会中断下载）；显式开 http_503 才转 busy
# ════════════════════════════════════════
def test_heartbeat_infra_error_is_not_claim_loss(env, monkeypatch):
    import api.extension as ext
    from core import download_slot as ds
    _mk_task(env, "T13")
    claim = env["client"].post("/api/extension/tasks/T13/claim", headers=env["A"]
                               ).json()["task"]["claim_id"]

    def boom(*a, **kw):
        raise ds.SlotUnavailable("database is locked")

    monkeypatch.setattr(ds, "renew", boom)
    r = env["client"].post("/api/extension/tasks/T13/heartbeat", headers=env["A"],
                           json={"claim_id": claim})
    assert r.status_code == 200, f"基础设施异常绝不能回 409（老插件会立刻掐断下载）: {r.text}"
    assert r.json()["degraded"] is True and r.json()["success"] is True
    assert "lease_seconds" in r.json()
    # 降级轮次不许动任务侧租约（保持原样，下一拍自然重试）
    from db.session import SessionLocal
    from db.models import LocalTask
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T13").first()
        assert t.claim_id == claim, "claim 未被改写 ⇒ 所有权判定仍然有效（安全性来源）"
    finally:
        db.close()

    # 显式切到 http_503（插件 0.7.2+ 才能安全使用）⇒ 必须带 Retry-After
    monkeypatch.setattr(ext, "_infra_error_mode", lambda: "http_503")
    r2 = env["client"].post("/api/extension/tasks/T13/heartbeat", headers=env["A"],
                            json={"claim_id": claim})
    assert r2.status_code == 503 and r2.json()["code"] == "busy"
    assert r2.headers.get("retry-after") == "15"


# ════════════════════════════════════════
#  用例 14：被租约清扫打回 pending ⇒ 409 code=requeued（可自愈），不是 lost
# ════════════════════════════════════════
def test_heartbeat_requeued_after_sweep(env):
    from datetime import timezone as tz
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T14")
    claim = env["client"].post("/api/extension/tasks/T14/claim", headers=env["A"]
                               ).json()["task"]["claim_id"]
    past = datetime.now(tz.utc).replace(tzinfo=None) - timedelta(seconds=600)
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(task_id="T14").update({"lease_until": past})
        db.commit()
    finally:
        db.close()
    assert ds.sweep_expired_local_tasks() >= 1

    hb = env["client"].post("/api/extension/tasks/T14/heartbeat", headers=env["A"],
                            json={"claim_id": claim})
    assert hb.status_code == 409 and hb.json()["code"] == "requeued", \
        "「重新排队」与「被接管」必须可区分：前者插件应当立即重 claim，不该冻结"

    # 立即重 claim 同一本必须成功（这是 requeued 的自愈闭环）
    r = env["client"].post("/api/extension/tasks/T14/claim", headers=env["A"])
    assert r.status_code == 200, r.text
    assert r.json()["task"]["mine"] is True
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T14").first()
        assert t.status == "running" and t.claim_session == _hash(f"tokA-{RUN}")
    finally:
        db.close()


# ════════════════════════════════════════
#  用例 15：complete / cancel 必须确认释放，并清掉归属（不留下 mine=false 的僵尸标记）
# ════════════════════════════════════════
def test_complete_and_cancel_release_slot_and_clear_ownership(env):
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T15a")
    _mk_task(env, "T15b")
    ca = env["client"].post("/api/extension/tasks/T15a/claim", headers=env["A"]
                            ).json()["task"]["claim_id"]
    assert ds.get_lock(env["card_id"])["task_id"] == "T15a"

    ok = env["client"].post("/api/extension/tasks/T15a/complete", headers=env["A"],
                            json={"claim_id": ca, "progress": {"total": 2, "done": 2, "failed": 0}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["slot_released"] is True
    assert ds.get_lock(env["card_id"]) is None, "complete 后槽必须真空出来（否则下一本白等一个租约周期）"

    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T15a").first()
        assert t.status == "done" and t.claim_id is None and t.claim_session is None
    finally:
        db.close()

    # 同一张卡立刻 claim 下一本 ⇒ 必须成功（槽已真正释放）
    cb = env["client"].post("/api/extension/tasks/T15b/claim", headers=env["A"])
    assert cb.status_code == 200, f"上一本已 complete，下一本不该被挡: {cb.text}"
    r = env["client"].post("/api/extension/tasks/T15b/cancel", headers=env["A"],
                           json={"claim_id": cb.json()["task"]["claim_id"]})
    assert r.status_code == 200 and r.json()["slot_released"] is True
    assert ds.get_lock(env["card_id"]) is None

    # 重复 complete（幂等路径）⇒ 409 already_done，不再与「被接管」共用一句文案
    again = env["client"].post("/api/extension/tasks/T15a/complete", headers=env["A"],
                               json={"claim_id": ca})
    assert again.status_code == 409 and again.json()["code"] == "already_done"


# ════════════════════════════════════════
#  附加：slot_renew_relaxed 开关关掉 ⇒ 完整回到旧行为（可回退性）
# ════════════════════════════════════════
def test_relaxed_switch_off_restores_old_behavior(env, monkeypatch):
    import api.extension as ext
    from datetime import timezone as tz
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T16")
    claim = env["client"].post("/api/extension/tasks/T16/claim", headers=env["A"]
                               ).json()["task"]["claim_id"]
    past = datetime.now(tz.utc).replace(tzinfo=None) - timedelta(seconds=90)
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(task_id="T16").update({"lease_until": past})
        db.commit()
        db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET lease_until = :v WHERE card_id = :c",
            {"v": ds._s(past), "c": env["card_id"]})
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(ext, "_renew_relaxed", lambda: False)   # 开关关闭 ⇒ 过期即拒
    hb = env["client"].post("/api/extension/tasks/T16/heartbeat", headers=env["A"],
                            json={"claim_id": claim})
    assert hb.status_code == 409 and hb.json()["code"] == "lost", "关掉开关必须可回退到 0.7.1 的语义"

    # 未过期时仍然正常续约（开关只影响「已过期」这一条路径）
    db = SessionLocal()
    try:
        future = datetime.now(tz.utc).replace(tzinfo=None) + timedelta(seconds=120)
        db.query(LocalTask).filter_by(task_id="T16").update({"lease_until": future})
        db.commit()
        db.connection().exec_driver_sql(
            "UPDATE card_download_locks SET lease_until = :v WHERE card_id = :c",
            {"v": ds._s(future), "c": env["card_id"]})
        db.commit()
    finally:
        db.close()
    assert env["client"].post("/api/extension/tasks/T16/heartbeat", headers=env["A"],
                              json={"claim_id": claim}).json()["success"] is True


# ════════════════════════════════════════
#  附加：管理员强制释放后，插件心跳绝不能把槽悄悄抢回来（I4 不被宽松续约破坏）
# ════════════════════════════════════════
def test_admin_force_release_is_not_resurrected_by_heartbeat(env):
    from db.session import SessionLocal
    from db.models import LocalTask
    from core import download_slot as ds

    _mk_task(env, "T17")
    claim = env["client"].post("/api/extension/tasks/T17/claim", headers=env["A"]
                               ).json()["task"]["claim_id"]
    assert ds.get_lock(env["card_id"])["task_id"] == "T17"

    # 管理员点「强制释放」：删锁 + 任务回 pending（见 api/admin.py 的副作用）
    holder = ds.force_release(env["card_id"])
    assert holder and holder["task_id"] == "T17"
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id="T17").first()
        t.status = "pending"
        t.claim_id = None
        t.claim_session = None
        t.lease_until = None
        db.commit()
    finally:
        db.close()

    r = env["client"].post("/api/extension/tasks/T17/heartbeat", headers=env["A"],
                           json={"claim_id": claim})
    assert r.status_code == 409 and r.json()["code"] == "requeued", \
        "强释必须让插件停下来（可重 claim），但不许由心跳自己把槽抢回去"
    assert ds.get_lock(env["card_id"]) is None, "心跳绝不允许复活被管理员释放的槽"
