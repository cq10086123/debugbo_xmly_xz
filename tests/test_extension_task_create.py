"""`POST /api/extension/task`（网页端推送到插件）的数据完整性回测。

三条都是本轮搭仿真环境跑出来的真问题（服务器返回 200，用户却不知道数据少了）：
- 超过 `_MAX_TRACKS_PER_TASK` 被静默截断：5003 集只进 5000 集，最后 3 集没人知道；
- 幂等去重只比曲目集合 ⇒ 「同一本书换成 m4a 再推一次」被当成重复吞掉，用户等的是旧格式；
- done 任务的 30 分钟去重窗口锚在 created_at ⇒ 下一小时的书刚下完就失去保护，
  双击/重推会生成重复任务，插件落盘成「书名 (1)」副本。
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

RUN = uuid.uuid4().hex[:8]

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="task_create_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.init_db import init_db                      # noqa: E402
from db.session import SessionLocal                 # noqa: E402
from db.models import ApiConfig, Card, LocalTask, Session as CardSession  # noqa: E402


@pytest.fixture(scope="module")
def env():
    init_db()
    db = SessionLocal()
    try:
        card = Card(code=f"TC-{RUN}", status="active")
        db.add(card)
        db.commit()
        db.refresh(card)
        db.add(CardSession(card_id=card.id, token=f"tc-tok-{RUN}", is_active=True,
                           client_type="web", device_id="", ip="127.0.0.1"))
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
    with TestClient(appmod.app) as client:
        yield {"client": client, "card_id": card_id,
               "h": {"Authorization": f"Bearer tc-tok-{RUN}"}}


def _tracks(n, prefix="t"):
    return [{"track_id": f"{prefix}-{i}", "episode_num": i + 1, "title": f"第{i + 1}集"}
            for i in range(n)]


def _push(env, album, n, *, fmt="mp3", quality=0, prefix=None, start=1):
    tracks = [{"track_id": f"{prefix or album}-{i}", "episode_num": start + i,
               "title": f"第{start + i}集"} for i in range(n)]
    r = env["client"].post("/api/extension/task", headers=env["h"],
                           json={"source": "third_party", "album_id": album,
                                 "album_title": f"书{album}", "quality": quality,
                                 "fmt": fmt, "tracks": tracks})
    assert r.status_code == 200, r.text
    return r.json()


def _stored_tracks(env, task_id):
    import json
    db = SessionLocal()
    try:
        row = db.query(LocalTask).filter_by(task_id=task_id).first()
        return json.loads(row.tracks) if row else None
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean(env):
    db = SessionLocal()
    try:
        db.query(LocalTask).filter_by(card_id=env["card_id"]).delete()
        db.commit()
    finally:
        db.close()
    yield


# ════════════════ 截断必须可见 ════════════════
def test_over_cap_is_explicit(env):
    from api.extension import _MAX_TRACKS_PER_TASK as CAP
    n = CAP + 7
    body = _push(env, "ALB-CAP", n)
    assert body["success"] is True
    stored = _stored_tracks(env, body["task_id"])
    assert len(stored) == CAP, f"落库应是上限 {CAP}，实际 {len(stored)}"
    assert body["truncated"] == 7, body
    assert body["requested"] == n
    # 关键：文案必须把"少了 7 集 + 怎么办"说出来（老实现只回 count=5000，用户以为整本在下）
    assert "超出单任务上限" in body["message"] and "7" in body["message"], body["message"]


# ════════════════ 换格式不许被当成重复 ════════════════
def test_format_change_is_not_swallowed_by_dedupe(env):
    first = _push(env, "ALB-F", 5, fmt="mp3")
    again = _push(env, "ALB-F", 5, fmt="mp3")
    assert again["duplicated"] is True and again["task_id"] == first["task_id"], \
        "完全相同的重复推送仍须幂等（防双击产生 (1) 副本）"

    m4a = _push(env, "ALB-F", 5, fmt="m4a")
    assert m4a.get("duplicated") is not True, "同一曲目换格式被误判为重复，用户等的 m4a 永远不会下"
    assert m4a["task_id"] != first["task_id"]
    assert _stored_tracks(env, m4a["task_id"])[0]["fmt"] == "m4a"


def test_tracks_inherit_task_fmt(env):
    """调用方没写每集 fmt 时必须继承任务级 fmt（否则插件按 m4a 解析、按 .mp3 存盘）。"""
    r = env["client"].post("/api/extension/task", headers=env["h"], json={
        "source": "third_party", "album_id": "ALB-N", "album_title": "无 fmt",
        "quality": 0, "fmt": "m4a",
        "tracks": [{"track_id": "n-1", "episode_num": 1, "title": "第一集"}]})
    assert r.status_code == 200, r.text
    assert _stored_tracks(env, r.json()["task_id"])[0]["fmt"] == "m4a"


def test_quality_change_is_explained_not_silent(env):
    _push(env, "ALB-Q", 5, quality=0, fmt="mp3")
    q2 = _push(env, "ALB-Q", 5, quality=2, fmt="mp3")
    # 音质不改变本地文件名 ⇒ 插件会跳过已存在文件。幂等本身没错，但必须解释，
    # 否则用户以为"高音质在重下"，实际拿的还是旧文件。
    assert q2["duplicated"] is True and q2["quality_diff"] is True, q2
    assert "音质" in q2["message"], q2["message"]


# ════════════════ done 去重窗口以完成时间为锚 ════════════════
def test_done_window_anchors_on_finished_at(env):
    body = _push(env, "ALB-D", 5)
    old = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=body["task_id"]).first()
        t.status = "done"
        t.created_at = old - timedelta(minutes=95)     # 一本书下了 90 多分钟
        t.finished_at = old - timedelta(minutes=2)     # 刚下完 2 分钟
        db.commit()
    finally:
        db.close()
    # 旧实现用 created_at 算 age=95min>30min ⇒ 去重失效 ⇒ 重复任务/「书名 (1)」副本
    again = _push(env, "ALB-D", 5)
    assert again.get("duplicated") is True, \
        f"刚完成 2 分钟的任务不该被重新推送一遍：{again}"
    assert "30 分钟" in again["message"]
