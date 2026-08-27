"""AI Skills 任务进度查询：必须覆盖官方 / 第三方 / 本地插件三类任务。

回归的 bug：`check_task_status` 只查 `api.download._batch_tasks`（官方引擎内存字典），
第三方接口任务登记在 `api.interfaces._intf_tasks`、插件任务在 `local_tasks` 表，
导致「任务明明在跑，查询却返回任务不存在」。

运行：python -m pytest tests/test_skill_task_status.py -v
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def env():
    from db.init_db import init_db
    from db.session import SessionLocal
    from db.models import Card
    from api.card_helpers import issue_skill_token

    init_db()
    db = SessionLocal()
    try:
        card = Card(
            code=f"TS-{RUN}",
            status="used",
            expiry_type="fixed",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            skill_token=issue_skill_token(),
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        auth = {
            "card_id": card.id,
            "code": card.code,
            "token": card.skill_token,
            "bound": [],
            "download_mode": "both",
            "quark_sync": False,
        }
    finally:
        db.close()
    return auth


def _run(coro):
    return asyncio.run(coro)


def _check(task_id, auth):
    from api.skills import skill_check_task_status, TaskIdRequest
    return _run(skill_check_task_status(TaskIdRequest(task_id=task_id), auth=auth))


# ── ① 官方引擎任务 ──
def test_official_task_found(env):
    import time
    from api.download import _batch_tasks

    tid = f"off{RUN[:5]}"
    _batch_tasks[tid] = {
        "task_id": tid, "card_id": env["card_id"], "status": "running",
        "album_title": "官方测试书", "album_id": 111, "total": 50, "current": 5,
        "completed": 5, "skipped_count": 0, "failed_list": [], "error": "",
        "started_at": time.time(), "engine": "official", "interface_name": "official",
    }
    r = _check(tid, env)
    assert r["success"] is True
    assert r["status"] == "running"
    assert r["completed"] == 5
    assert r["task_type"] == "server"


# ── ② 第三方接口任务（回归重点）──
def test_thirdparty_task_found(env):
    import time
    from api.interfaces import _intf_tasks

    tid = f"tp{RUN[:6]}"
    _intf_tasks[tid] = {
        "task_id": tid, "card_id": env["card_id"], "interface": "tingyou8",
        "status": "running", "album_title": "第三方测试书", "book_id": "abc",
        "total": 100, "current": 7, "completed": 6, "skipped_count": 0,
        "failed_list": [], "error": "", "last_error": "", "started_at": time.time(),
        "fmt": "mp3", "concurrency": 3,
    }
    r = _check(tid, env)
    assert r["success"] is True, f"第三方任务应能查到，实际: {r}"
    assert r["status"] == "running"
    assert r["completed"] == 6
    assert r["percent"] == 6
    assert r["source"] == "tingyou8"


# ── ③ 浏览器插件本地任务 ──
def test_local_task_found_with_progress(env):
    from db.session import SessionLocal
    from db.models import LocalTask

    tid = f"loc{RUN[:5]}"
    db = SessionLocal()
    try:
        db.add(LocalTask(
            task_id=tid, card_id=env["card_id"], source="official",
            album_id="9", album_title="插件测试书",
            tracks=json.dumps([{"track_id": i} for i in range(50)]),
            status="running",
            # 插件写入的键是 done/failed（见 extension/background.js）
            progress=json.dumps({"total": 50, "done": 33, "failed": 2}),
            failed_list=json.dumps([]),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()
    finally:
        db.close()

    r = _check(tid, env)
    assert r["success"] is True, f"本地插件任务应能查到，实际: {r}"
    assert r["task_type"] == "local"
    assert r["completed"] == 33, "必须读插件写入的 done 键，而不是 completed"
    assert r["percent"] == 66
    assert r["failed_count"] == 2


def test_check_local_tasks_reports_real_progress(env):
    """check_local_tasks 曾把 done 读成 completed → 进度恒为 0。"""
    from api.skills import skill_check_local_tasks

    r = _run(skill_check_local_tasks(auth=env))
    assert r["success"] is True
    task = next(t for t in r["tasks"] if t["task_id"].startswith("loc"))
    assert task["completed"] == 33, f"进度不应为 0，实际: {task}"
    assert task["percent"] == 66


# ── ④ 重启兜底：内存清空但 download_tasks 表仍有行 ──
def test_db_fallback_after_restart(env):
    import time
    from api.persistence import persist_task

    tid = f"db{RUN[:6]}"
    persist_task({
        "task_id": tid, "card_id": env["card_id"], "engine": "tingyou8",
        "album_id": "555", "album_title": "重启前的书", "status": "interrupted",
        "total": 80, "completed": 40, "skipped_count": 0, "failed_list": [],
        "started_at": time.time(),
    })
    # 内存里没有它（模拟重启后 _intf_tasks 为空）
    from api.interfaces import _intf_tasks
    assert tid not in _intf_tasks

    r = _check(tid, env)
    assert r["success"] is True, f"重启后应能从数据库兜底查到，实际: {r}"
    assert r["completed"] == 40
    assert r["from_db"] is True


def test_db_fallback_is_readonly(env):
    """查询进度不得有副作用：running 行不能被改写成 interrupted。"""
    import time
    from api.persistence import persist_task, get_task_row

    tid = f"ro{RUN[:6]}"
    persist_task({
        "task_id": tid, "card_id": env["card_id"], "engine": "tingyou8",
        "album_id": "556", "album_title": "只读校验", "status": "running",
        "total": 10, "completed": 1, "skipped_count": 0, "failed_list": [],
        "started_at": time.time(),
    })
    _check(tid, env)
    assert get_task_row(tid, env["card_id"])["status"] == "running"


# ── ⑤ 卡密隔离不能被破坏 ──
def test_cross_card_isolation(env):
    import time
    from api.interfaces import _intf_tasks

    tid = f"iso{RUN[:5]}"
    _intf_tasks[tid] = {
        "task_id": tid, "card_id": env["card_id"] + 99999, "interface": "tingyou8",
        "status": "running", "album_title": "别人的书", "total": 10,
        "completed": 1, "skipped_count": 0, "failed_list": [], "started_at": time.time(),
    }
    r = _check(tid, env)
    assert r["success"] is False, "不得跨卡密泄露他人任务"


# ── ⑥ 真正不存在时，给出可执行的诊断 ──
def test_not_found_hints_active_slot(env):
    from core import download_slot

    ok, _ = download_slot.acquire(
        env["card_id"], "server", f"held{RUN[:4]}",
        ttl_seconds=download_slot.SERVER_TTL_SECONDS,
        source="tingyou8", album_id="777", album_title="正在下载的书",
    )
    assert ok
    try:
        r = _check("deadbeef", env)
        assert r["success"] is False
        assert "正在下载的书" in r["suggestion"], f"应提示实际持槽任务，实际: {r}"
        assert f"held{RUN[:4]}" in r["suggestion"]
    finally:
        download_slot.force_release(env["card_id"])


def test_list_active_downloads_finds_thirdparty(env):
    """没有 task_id 时的兜底发现路径。"""
    from api.skills import skill_list_active_downloads

    r = _run(skill_list_active_downloads(auth=env))
    assert r["success"] is True
    ids = [t["task_id"] for t in r["tasks"]]
    assert f"tp{RUN[:6]}" in ids, f"应列出第三方进行中任务，实际: {r}"
    assert f"off{RUN[:5]}" in ids, "应列出官方进行中任务"


# ── ⑦ 健壮性（审查阶段补充） ──
def test_progress_percent_is_clamped():
    """插件重复计数导致 done>total 时，百分比不得超过 100。"""
    from api.skills import _parse_local_progress
    assert _parse_local_progress({"total": 5, "done": 9}, [], [])["percent"] == 100


def test_progress_handles_non_numeric_and_wrong_types():
    from api.skills import _parse_local_progress
    for bad in ({"total": "abc", "done": None}, [1, 2], None, "x", {"total": -5}):
        out = _parse_local_progress(bad, [], [])
        assert out["percent"] == 0
        assert out["total"] >= 0 and out["completed"] >= 0


def test_corrupt_json_does_not_hide_existing_task(env):
    """progress 字段是脏 JSON 时，任务仍应被查到，而不是误报「任务不存在」。"""
    import json as _json
    from db.session import SessionLocal
    from db.models import LocalTask

    tid = f"crp{RUN[:5]}"
    db = SessionLocal()
    try:
        db.add(LocalTask(
            task_id=tid, card_id=env["card_id"], source="official",
            album_title="脏数据书", tracks="[]", status="running",
            progress="{不是合法json", failed_list="也不是json",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()
    finally:
        db.close()

    r = _check(tid, env)
    assert r["success"] is True, f"脏 JSON 不应导致任务查不到: {r}"
    assert r["album_title"] == "脏数据书"
    assert r["status"] == "running"


# ── ⑧ 重启后第三方任务持久化恢复 ──
def _persist_intf(task_id, card_id, engine, status, completed=0):
    import time
    from api.persistence import persist_task
    persist_task({
        "task_id": task_id, "card_id": card_id, "interface": engine, "engine": engine,
        "book_id": "bk9", "album_id": "bk9", "album_title": f"{engine}的书",
        "status": status, "total": 100, "completed": completed, "skipped_count": 0,
        "failed_list": [], "started_at": time.time(), "fmt": "mp3", "concurrency": 3,
    })


def test_restart_restores_third_party_tasks(env):
    """回归：重启后 _intf_tasks 恒为空，网页「接口任务」整个列表消失。"""
    import api.interfaces as I

    tid = f"ri{RUN[:6]}"
    _persist_intf(tid, env["card_id"], "某小说站", "running", 42)
    I.reload_intf_tasks()

    assert tid in I._intf_tasks, "重启后第三方任务未恢复"
    # 进程内协程已消亡，running 必须降级为 interrupted，不能继续谎报「下载中」
    assert I._intf_tasks[tid]["status"] == "interrupted"

    d = _run(I.get_intf_task(tid, auth=env))
    assert d["success"] is True
    assert d["interface"] == "某小说站"      # 恢复的字典要能喂饱 _task_summary
    assert d["percent"] == 42


def test_restart_does_not_mix_official_and_third_party(env):
    """两张内存表必须互不污染：官方任务不得进 _intf_tasks，反之亦然。"""
    import api.download as D
    import api.interfaces as I

    off, intf = f"ro{RUN[:6]}", f"rt{RUN[:6]}"
    _persist_intf(off, env["card_id"], "official", "running")
    _persist_intf(intf, env["card_id"], "第三方源", "running")

    D.reload_tasks()
    I.reload_intf_tasks()

    assert off in D._batch_tasks and off not in I._intf_tasks
    assert intf in I._intf_tasks and intf not in D._batch_tasks


def test_deleted_third_party_task_does_not_resurrect(env):
    """删除第三方任务必须同时删 DB 行，否则重启后「幽灵复活」。"""
    from db.session import SessionLocal
    from db.models import DownloadTask
    import api.interfaces as I

    tid = f"rg{RUN[:6]}"
    _persist_intf(tid, env["card_id"], "某小说站", "completed", 100)
    I.reload_intf_tasks()
    assert tid in I._intf_tasks

    r = _run(I.delete_intf_task(tid, auth=env))
    assert r["success"] is True

    db = SessionLocal()
    try:
        assert db.query(DownloadTask).filter_by(task_id=tid).first() is None
    finally:
        db.close()

    I.reload_intf_tasks()
    assert tid not in I._intf_tasks, "已删除的任务在重启后复活了"


def test_restored_third_party_task_still_card_scoped(env):
    """恢复的任务同样不能跨卡密泄露。"""
    import api.interfaces as I

    tid = f"rs{RUN[:6]}"
    _persist_intf(tid, env["card_id"], "某小说站", "running", 5)
    I.reload_intf_tasks()

    other = dict(env, card_id=env["card_id"] + 9999)
    assert _run(I.get_intf_task(tid, auth=other))["success"] is False
    assert _run(I.list_intf_tasks(auth=other))["tasks"] == []
