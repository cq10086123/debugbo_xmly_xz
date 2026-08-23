"""回归测试：设备绑定（席位模型 + 顶号换绑 + 会话设备校验）。

覆盖：
- 纯函数：normalize_client_type / get_max_devices / check_session_pure
- 集成（临时 SQLite）：bind_device_on_login 席位绑定、顶号淘汰、unbind、kick_all

运行：python tests/test_device_binding.py（或 pytest tests/test_device_binding.py）
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 集成部分：指向临时 DATA_DIR 再 import db（必须在 import db.session 前）──
_TMP = tempfile.mkdtemp(prefix="dvb_test_")
os.environ["DATA_DIR"] = _TMP

from fastapi import HTTPException  # noqa: E402

from core import device_binding as dvb  # noqa: E402


# ════════════════════════════════════════
#  纯函数
# ════════════════════════════════════════
def test_normalize_client_type():
    assert dvb.normalize_client_type(None) == "web"        # 缺省按 web
    assert dvb.normalize_client_type("") == "web"
    assert dvb.normalize_client_type("Web") == "web"
    assert dvb.normalize_client_type("EXTENSION") == "extension"
    assert dvb.normalize_client_type("plugin") is None     # 非法值
    assert dvb.normalize_client_type("hacker") is None


class _FakeCard:
    def __init__(self, max_devices=None):
        self.max_devices = max_devices


def test_get_max_devices():
    assert dvb.get_max_devices(_FakeCard(None)) == 1       # NULL → 1
    assert dvb.get_max_devices(_FakeCard(1)) == 1
    assert dvb.get_max_devices(_FakeCard(3)) == 3
    assert dvb.get_max_devices(_FakeCard(99)) == 10        # 钳制上限
    assert dvb.get_max_devices(_FakeCard(0)) == 1          # 钳制下限
    assert dvb.get_max_devices(_FakeCard("abc")) == 1      # 非法 → 默认


def test_check_session_pure():
    now = datetime.now(timezone.utc)
    # 历史会话（device_id 为空）→ 放行（平滑过渡）
    ok, code, _ = dvb.check_session_pure(None, "web", now, None, set(), 7, now)
    assert ok and not code

    # 正常：设备匹配 + 在绑定集合内 + 未空闲超时
    ok, code, _ = dvb.check_session_pure("devA", "web", now, "devA", {"devA"}, 7, now)
    assert ok and not code

    # device_mismatch：token 拷到别的浏览器（请求头设备 ID 不一致）
    ok, code, _ = dvb.check_session_pure("devA", "web", now, "devB", {"devA"}, 7, now)
    assert not ok and code == "device_mismatch"

    # 请求未带设备头（老客户端）但会话有设备 → 也算不匹配（防删头绕过）
    ok, code, _ = dvb.check_session_pure("devA", "web", now, None, {"devA"}, 7, now)
    assert not ok and code == "device_mismatch"

    # kicked：设备已被顶号换绑（不在绑定集合）
    ok, code, _ = dvb.check_session_pure("devA", "web", now, "devA", {"devB"}, 7, now)
    assert not ok and code == "kicked"

    # session_expired：空闲超时
    old = now - timedelta(days=8)
    ok, code, _ = dvb.check_session_pure("devA", "web", old, "devA", {"devA"}, 7, now)
    assert not ok and code == "session_expired"

    # client_type 为空（开关关闭期登录）→ 只做设备匹配，不做席位校验
    ok, code, _ = dvb.check_session_pure("devA", None, now, "devA", set(), 7, now)
    assert ok and not code


# ════════════════════════════════════════
#  集成（临时 SQLite）
# ════════════════════════════════════════
def _setup_db():
    from db.session import Base, _engine, SessionLocal
    from db import models as _  # noqa: F401 注册全部模型
    Base.metadata.create_all(_engine)
    return SessionLocal


def _mk_card(db, code="XM-TEST-0001", max_devices=None):
    from db.models import Card
    card = Card(code=code, status="used", expiry_type="fixed", max_devices=max_devices)
    db.add(card)
    db.commit()
    return card


def _mk_session(db, card, device_id, client_type="web", token=None):
    from db.models import Session as CardSession
    token = token or f"tok-{device_id}-{client_type}"
    db.add(CardSession(token=token, card_id=card.id, is_active=True,
                       client_type=client_type, device_id=device_id,
                       last_active_at=datetime.now(timezone.utc)))
    db.commit()
    return token


def _active_tokens(db, card):
    from db.models import Session as CardSession
    rows = db.query(CardSession).filter_by(card_id=card.id, is_active=True).all()
    return {r.device_id for r in rows}


def test_bind_and_evict_integration():
    SessionLocal = _setup_db()
    from db.models import Card, DeviceBinding
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0001", max_devices=1)

        # 1) 首台设备绑定 web 席位
        r1 = dvb.bind_device_on_login(db, card, "web", "device-AAAA", "1.1.1.1")
        db.commit()
        assert r1["evicted"] == []
        assert dvb.bound_device_ids(db, card.id, "web") == {"device-AAAA"}

        # 2) 插件席位独立：不影响 web 席位（同机双端共存）
        dvb.bind_device_on_login(db, card, "extension", "device-BBBB", "1.1.1.1")
        db.commit()
        assert dvb.bound_device_ids(db, card.id, "web") == {"device-AAAA"}
        assert dvb.bound_device_ids(db, card.id, "extension") == {"device-BBBB"}

        # 3) 顶号换绑：web 席位已满（1），新设备登录踢掉旧设备及其会话
        _mk_session(db, card, "device-AAAA", "web")
        r3 = dvb.bind_device_on_login(db, card, "web", "device-CCCC", "2.2.2.2")
        db.commit()
        assert r3["evicted"] == ["device-AAAA"]
        assert r3["kicked_sessions"] == 1
        assert dvb.bound_device_ids(db, card.id, "web") == {"device-CCCC"}
        assert "device-AAAA" not in _active_tokens(db, card)   # 旧设备会话已失效
        assert dvb.bound_device_ids(db, card.id, "extension") == {"device-BBBB"}  # 插件席不受影响

        # 4) 同设备重复登录：不换绑，仅续活
        r4 = dvb.bind_device_on_login(db, card, "web", "device-CCCC", "3.3.3.3")
        db.commit()
        assert r4["evicted"] == []
        assert dvb.bound_device_ids(db, card.id, "web") == {"device-CCCC"}

        # 5) 多设备卡（max_devices=2）：第二台共存，第三台踢最早那台
        card2 = _mk_card(db, "XM-TEST-0002", max_devices=2)
        dvb.bind_device_on_login(db, card2, "web", "dev-D1", "1.1.1.1")
        dvb.bind_device_on_login(db, card2, "web", "dev-D2", "1.1.1.1")
        db.commit()
        assert dvb.bound_device_ids(db, card2.id, "web") == {"dev-D1", "dev-D2"}
        r5 = dvb.bind_device_on_login(db, card2, "web", "dev-D3", "1.1.1.1")
        db.commit()
        assert r5["evicted"] == ["dev-D1"]                     # bound_at 最早的被淘汰
        assert dvb.bound_device_ids(db, card2.id, "web") == {"dev-D2", "dev-D3"}
    finally:
        db.close()


def test_unbind_and_kick_integration():
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        from db.models import DeviceBinding
        card = _mk_card(db, "XM-TEST-0003", max_devices=1)
        dvb.bind_device_on_login(db, card, "web", "dev-X", "1.1.1.1")
        dvb.bind_device_on_login(db, card, "extension", "dev-Y", "1.1.1.1")
        _mk_session(db, card, "dev-X", "web")
        _mk_session(db, card, "dev-Y", "extension")
        db.commit()

        # 解绑单个设备（指定席位）：会话一并失效，另一席位不动
        n = dvb.unbind_device(db, card, "dev-X", "web")
        db.commit()
        assert n == 1
        assert dvb.bound_device_ids(db, card.id, "web") == set()
        assert "dev-X" not in _active_tokens(db, card)
        assert "dev-Y" in _active_tokens(db, card)

        # 踢下线（保留绑定）：会话失效但绑定还在
        n2 = dvb.kick_all_sessions(db, card)
        db.commit()
        assert n2 == 1                                           # 只剩 dev-Y 的会话
        assert _active_tokens(db, card) == set()
        assert dvb.bound_device_ids(db, card.id, "extension") == {"dev-Y"}

        # 全部解绑：席位清空 + 兜底清会话
        n3 = dvb.unbind_all(db, card)
        db.commit()
        assert n3 == 1
        assert dvb.bound_device_ids(db, card.id, "extension") == set()
        assert db.query(DeviceBinding).filter_by(card_id=card.id).count() == 0
    finally:
        db.close()


def test_check_session_integration():
    """鉴权入口（check_session）：绑定在 → 放行；被顶号 → kicked。"""
    SessionLocal = _setup_db()
    from db.models import Session as CardSession
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0004", max_devices=1)
        dvb.bind_device_on_login(db, card, "web", "dev-Z", "1.1.1.1")
        _mk_session(db, card, "dev-Z", "web", token="tok-z")
        db.commit()
        sess = db.query(CardSession).filter_by(token="tok-z").first()

        # 席位绑定在 → 放行（直接调 check_session_pure + 真实绑定集合）
        bound = dvb.bound_device_ids(db, card.id, "web")
        ok, code, _ = dvb.check_session_pure(
            sess.device_id, sess.client_type, sess.last_active_at,
            "dev-Z", bound, 7,
        )
        assert ok

        # 顶号换绑后，旧会话 → kicked
        dvb.bind_device_on_login(db, card, "web", "dev-NEW", "2.2.2.2")
        db.commit()
        bound = dvb.bound_device_ids(db, card.id, "web")
        ok, code, msg = dvb.check_session_pure(
            sess.device_id, sess.client_type, sess.last_active_at,
            "dev-Z", bound, 7,
        )
        assert not ok and code == "kicked" and "其他设备" in msg
    finally:
        db.close()


if __name__ == "__main__":
    test_normalize_client_type()
    test_get_max_devices()
    test_check_session_pure()
    test_bind_and_evict_integration()
    test_unbind_and_kick_integration()
    test_check_session_integration()
    print("ALL PASS")
