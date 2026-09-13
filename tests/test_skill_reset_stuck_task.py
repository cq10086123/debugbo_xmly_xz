"""SKILL `reset_stuck_local_task` 必须连下载槽一起释放（修"重置了但插件 5 分钟接不上"）。

背景：该接口是给 SKILL 助手用来救"僵尸任务"的——浏览器崩溃后任务停在 running。
旧实现只把 local_tasks 打回 pending，没碰 card_download_locks：槽仍被旧 claim 持有且租约
未过期，于是插件 30 秒后重新 claim 一律 409「当前卡密已有下载任务进行中」，要白等到租约
过期（默认 300s）。而接口文案承诺的是"30 秒内重新接管" ⇒ 用户看到的现象是"重置没用"。
顺带把 claim_session（归属指纹）一起清掉，否则 /tasks 仍会显示一个已经不存在的持有者。
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

RUN = uuid.uuid4().hex[:8]

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="skill_reset_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.init_db import init_db                                  # noqa: E402
from db.session import SessionLocal                             # noqa: E402
from db.models import ApiConfig, Card, LocalTask, CardDownloadLock  # noqa: E402
from core import download_slot                                  # noqa: E402


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


@pytest.fixture(scope="module")
def env():
    init_db()
    from api.card_helpers import issue_skill_token
    db = SessionLocal()
    try:
        card = Card(code=f"SR-{RUN}", status="active", skill_token=issue_skill_token())
        db.add(card)
        db.commit()
        db.refresh(card)
        tok, card_id = card.skill_token, card.id
        row = db.query(ApiConfig).filter_by(cfg_key="admin_lan_only").first()
        if row is None:
            db.add(ApiConfig(cfg_key="admin_lan_only", cfg_value="0", category="settings",
                             updated_at=_naive(datetime.now(timezone.utc))))
        else:
            row.cfg_value = "0"
        db.commit()
    finally:
        db.close()
    from fastapi.testclient import TestClient
    import app as appmod
    with TestClient(appmod.app) as client:
        yield {"client": client, "card_id": card_id, "h": {"Authorization": f"Bearer {tok}"}}


def _mk_running_task(env, task_id, claim_id, *, with_slot=True, lease_seconds=300):
    """造一个"插件已死但任务还挂着 running"的现场（任务侧 + 槽侧都写）。"""
    db = SessionLocal()
    try:
        db.add(LocalTask(task_id=task_id, card_id=env["card_id"], source="third_party",
                         album_id="ALB-SR", album_title="僵尸任务", quality=0, fmt="mp3",
                         tracks='[{"track_id":"a-1","episode_num":1,"title":"t","fmt":"mp3"}]',
                         status="running", claim_id=claim_id,
                         claim_session="deadbeefdeadbeef",
                         claimed_at=_naive(datetime.now(timezone.utc) - timedelta(minutes=20)),
                         heartbeat_at=_naive(datetime.now(timezone.utc) - timedelta(minutes=20)),
                         lease_until=_naive(datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))))
        db.commit()
    finally:
        db.close()
    if with_slot:
        download_slot.acquire(env["card_id"], "local", task_id, claim_id=claim_id,
                              ttl_seconds=lease_seconds, source="third_party",
                              album_id="ALB-SR", album_title="僵尸任务")


def _lock_row(card_id):
    db = SessionLocal()
    try:
        return db.query(CardDownloadLock).filter_by(card_id=card_id).first()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean(env):
    yield
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(card_id=env["card_id"]).delete(synchronize_session=False)
        db.query(CardDownloadLock).filter_by(card_id=env["card_id"]).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_reset_releases_slot_so_plugin_can_reclaim_immediately(env):
    tid, cid = f"srt{RUN}a", f"clm{RUN}a"
    _mk_running_task(env, tid, cid)
    assert _lock_row(env["card_id"]) is not None, "前置：槽应被旧 claim 占着"

    r = env["client"].post("/api/skills/reset_stuck_local_task", headers=env["h"],
                           json={"task_id": tid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body.get("slot_released") is True, body
    assert _lock_row(env["card_id"]) is None, "槽必须被释放，否则插件重新 claim 会撞 409"

    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=tid).first()
        assert t.status == "pending"
        assert t.claim_id is None, "claim 凭证要一起清（旧插件不许再用旧 claim 心跳）"
        assert t.claim_session is None, "归属指纹残留会让 /tasks 显示不存在的持有者"
        assert t.lease_until is None
    finally:
        db.close()

    # 立刻可被重新 claim（这是这条接口的全部意义）
    won, _ = download_slot.acquire(env["card_id"], "local", tid, claim_id="new-claim")
    assert won is True, "释放后同一秒就该能 claim，不该等租约自然过期"


def test_reset_does_not_steal_another_tasks_slot(env):
    """槽已被别的任务持有时，重置只能改自己的任务状态，绝不越权删别人的锁。"""
    mine, other = f"srt{RUN}b", f"srt{RUN}c"
    _mk_running_task(env, mine, f"clm{RUN}b", with_slot=False)
    download_slot.acquire(env["card_id"], "local", other, claim_id=f"clm{RUN}c",
                          ttl_seconds=300, source="third_party", album_id="O", album_title="O")

    r = env["client"].post("/api/skills/reset_stuck_local_task", headers=env["h"],
                           json={"task_id": mine})
    assert r.status_code == 200, r.text
    assert _lock_row(env["card_id"]).task_id == other, "别人正在下载的槽不能被这次重置带走"

    db = SessionLocal()
    try:
        assert db.query(LocalTask).filter_by(task_id=mine).first().status == "pending"
    finally:
        db.close()


def test_reset_message_honest_when_slot_gone(env):
    """没有槽（历史遗留/已被清扫）时也应成功，且 slot_released 走"目标状态已达成"分支。"""
    tid = f"srt{RUN}d"
    _mk_running_task(env, tid, f"clm{RUN}d", with_slot=False)
    r = env["client"].post("/api/skills/reset_stuck_local_task", headers=env["h"],
                           json={"task_id": tid})
    body = r.json()
    assert body["success"] is True and body["slot_released"] is True, body
    assert "下载槽暂时没释放成功" not in body["message"]


# ════════════════════════════════════════
#  D4：get_card_info 必须真能用（旧实现读不存在的 card.expire_time ⇒ 500）
# ════════════════════════════════════════
def _card_with(**kw):
    from db.models import Card
    from api.card_helpers import issue_skill_token
    db = SessionLocal()
    try:
        c = Card(code=f"GI-{RUN}-{uuid.uuid4().hex[:5]}", status="active",
                 skill_token=issue_skill_token(), **kw)
        db.add(c)
        db.commit()
        tok = c.skill_token
    finally:
        db.close()
    return tok


def test_get_card_info_fixed_expiry(env):
    from datetime import timedelta
    tok = _card_with(expiry_type="fixed",
                     expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3))
    r = env["client"].post("/api/skills/get_card_info", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text          # 旧实现在这里 500
    b = r.json()
    assert b["success"] is True and "error" not in b, b
    assert b["is_expired"] is False
    assert "(UTC)" in b["expire_time"], b["expire_time"]
    assert 2 * 86400 < b["remaining_seconds"] <= 3 * 86400, b
    assert b["remaining_text"] and "天" in b["remaining_text"], b


def test_get_card_info_days_based_and_permanent(env):
    from datetime import timedelta
    tok = _card_with(expiry_type="days", valid_days=30,
                     activated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=29))
    b = env["client"].post("/api/skills/get_card_info",
                           headers={"Authorization": f"Bearer {tok}"}).json()
    assert b["success"] is True and b["expire_time"] == "激活日起 30 天", b
    assert b["remaining_seconds"] is not None and b["remaining_seconds"] <= 86400, b

    perm = _card_with(expiry_type="fixed", expires_at=None)
    b2 = env["client"].post("/api/skills/get_card_info",
                            headers={"Authorization": f"Bearer {perm}"}).json()
    assert b2["success"] is True and b2["expire_time"] == "永久有效", b2
    assert b2["is_expired"] is False and b2["remaining_seconds"] is None, b2

    # 边界：expires_at 已过但 status 仍 active ⇒ 鉴权会拦；这里只保证不 500（见下一个用例）


def test_get_card_info_expired_card_is_rejected_not_500(env):
    """过期卡密的 SKILL 凭证应在鉴权层就被拒（而不是接口 500 或被判成"永久有效"）。

    旧实现里 `card.expire_time` 抛 AttributeError ⇒ 任何卡密调用都是 500；修完后必须
    区分两种情况：鉴权拒绝（401，设计如此）与正常返回（200 + is_expired 口径正确）。
    """
    from datetime import timedelta
    tok = _card_with(expiry_type="fixed",
                     expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
    r = env["client"].post("/api/skills/get_card_info", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401, (r.status_code, r.text)
    assert "expire_time" not in r.text, "不许把 AttributeError 当成响应返回"
