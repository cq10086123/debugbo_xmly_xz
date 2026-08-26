"""SKILL 专用 token：与网页会话解耦，跳过网络绑定 / IP 风控，仍受卡密过期与下载槽约束。

运行：python -m pytest tests/test_skill_token.py -v
"""
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

import pytest

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def env():
    from db.init_db import init_db
    from db.session import SessionLocal
    from db.models import Card, Session as CardSession, ApiConfig, XimalayaAccount, DeviceBinding
    from api.card_helpers import issue_skill_token
    from fastapi.testclient import TestClient
    import app as appmod

    init_db()
    db = SessionLocal()
    try:
        card = Card(
            code=f"SK-{RUN}",
            status="used",
            expiry_type="fixed",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            skill_token=issue_skill_token(),
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        web_tok = f"web-{RUN}"
        now = datetime.now(timezone.utc)
        db.add(CardSession(
            token=web_tok, card_id=card.id, is_active=True,
            client_type="web", device_id="lan",
            last_active_at=now, ip="192.168.1.100",
        ))
        db.add(DeviceBinding(
            card_id=card.id, client_type="web", device_id="lan",
            bound_at=now, last_active_at=now, last_ip="192.168.1.100",
        ))
        db.add(XimalayaAccount(
            card_id=card.id, acc_id="accSk", uid="u-sk",
            nickname="sk", cookie_str="uid=1", is_vip=True,
        ))
        row = db.query(ApiConfig).filter_by(cfg_key="device_binding_enabled").first()
        if row:
            row.cfg_value = "1"
        else:
            db.add(ApiConfig(cfg_key="device_binding_enabled", cfg_value="1", category="settings"))
        row = db.query(ApiConfig).filter_by(cfg_key="trust_proxy_header").first()
        if row:
            row.cfg_value = "1"
        else:
            db.add(ApiConfig(cfg_key="trust_proxy_header", cfg_value="1", category="settings"))
        lan = db.query(ApiConfig).filter_by(cfg_key="admin_lan_only").first()
        if lan:
            lan.cfg_value = "0"
        else:
            db.add(ApiConfig(cfg_key="admin_lan_only", cfg_value="0", category="settings"))
        db.commit()
        from core import device_binding as dvb
        dvb.invalidate_cfg_cache()
        payload = {
            "card_id": card.id,
            "code": card.code,
            "skill": card.skill_token,
            "web": web_tok,
        }
    finally:
        db.close()

    with TestClient(appmod.app, client=("192.168.1.100", 54321)) as client:
        payload["client"] = client
        yield payload


def _sk(env):
    return {"Authorization": f"Bearer {env['skill']}"}


def _web(env, extra=None):
    h = {"Authorization": f"Bearer {env['web']}"}
    if extra:
        h.update(extra)
    return h


def test_skill_ignores_foreign_network(env):
    """绑定开启时：网页 token 换网 401；SKILL token 换网仍可用。"""
    c = env["client"]
    other = {"X-Forwarded-For": "98.7.6.5"}

    r = c.get("/api/auth/me", headers=_web(env))
    assert r.status_code == 200, r.text

    r = c.get("/api/auth/me", headers=_web(env, other))
    assert r.status_code == 401
    assert "网络环境已变更" in r.json().get("detail", "")

    r = c.post("/api/skills/get_sources", headers={**_sk(env), **other})
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True


def test_web_session_rejected_on_skill_routes(env):
    """网页 session 不能当 SKILL token 用。"""
    c = env["client"]
    r = c.post("/api/skills/get_sources", headers=_web(env))
    assert r.status_code == 401
    assert "SKILL" in r.json().get("detail", "")


def test_export_embeds_skill_token_not_web_token(env):
    c = env["client"]
    r = c.get(f"/api/skills/export_openapi?token={env['web']}")
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    cfg = json.loads(z.read("ai_skills_config.json"))
    blob = json.dumps(cfg)
    assert env["skill"] in blob
    assert env["web"] not in blob
    assert "sk_" in blob


def test_export_requires_web_login(env):
    c = env["client"]
    r = c.get("/api/skills/export_openapi")
    assert r.status_code == 401
    r = c.get(f"/api/skills/export_openapi?token={env['skill']}")
    assert r.status_code == 401  # skill token 不是网页 session


def test_rotate_invalidates_old_skill_token(env):
    """独立卡密：避免改写共享 fixture 的 skill token 影响其他用例。"""
    from db.session import SessionLocal
    from db.models import Card, Session as CardSession, DeviceBinding
    from api.card_helpers import issue_skill_token

    c = env["client"]
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        old = issue_skill_token()
        card = Card(
            code=f"SK-ROT-{RUN}", status="used", expiry_type="fixed",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc), skill_token=old,
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        web = f"web-rot-{RUN}"
        db.add(CardSession(
            token=web, card_id=card.id, is_active=True,
            client_type="web", device_id="lan", last_active_at=now, ip="192.168.1.100",
        ))
        db.add(DeviceBinding(
            card_id=card.id, client_type="web", device_id="lan",
            bound_at=now, last_active_at=now, last_ip="192.168.1.100",
        ))
        db.commit()
        cid = card.id
    finally:
        db.close()

    r = c.post("/api/skills/rotate_token", headers={"Authorization": f"Bearer {web}"})
    assert r.status_code == 200 and r.json().get("success"), r.text

    r = c.post("/api/skills/get_sources", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=cid).first()
        new = card.skill_token
        assert new and new != old
    finally:
        db.close()

    r = c.post("/api/skills/get_sources", headers={"Authorization": f"Bearer {new}"})
    assert r.status_code == 200, r.text


def test_expired_card_blocks_skill(env):
    from db.session import SessionLocal
    from db.models import Card
    c = env["client"]
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=env["card_id"]).first()
        card.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        card.status = "used"
        db.commit()
    finally:
        db.close()

    r = c.post("/api/skills/get_card_info", headers=_sk(env))
    assert r.status_code == 401
    assert "过期" in r.json().get("detail", "")

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=env["card_id"]).first()
        card.expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        card.status = "used"
        db.commit()
    finally:
        db.close()


def test_disabled_card_blocks_skill(env):
    from db.session import SessionLocal
    from db.models import Card
    c = env["client"]
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=env["card_id"]).first()
        card.status = "disabled"
        db.commit()
    finally:
        db.close()

    r = c.post("/api/skills/get_sources", headers=_sk(env))
    assert r.status_code == 403

    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=env["card_id"]).first()
        card.status = "used"
        db.commit()
    finally:
        db.close()


def test_skill_submit_still_409_when_slot_busy(env):
    from core import download_slot
    c = env["client"]
    cid = env["card_id"]
    ok, _ = download_slot.acquire(cid, "server", "busy-task")
    assert ok
    try:
        r = c.post(
            "/api/skills/submit_download",
            headers=_sk(env),
            json={"album_id": "1", "source": "official", "start_episode": 1, "end_episode": 1},
        )
        assert r.status_code == 409, r.text
        assert "已有下载任务进行中" in (r.json().get("error") or r.json().get("detail") or "")
    finally:
        download_slot.release(cid, "busy-task")
