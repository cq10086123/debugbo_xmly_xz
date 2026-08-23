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


def test_trim_bindings_to_limit():
    """管理员下调 max_devices：超额绑定即时裁剪（保留最新）。"""
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0005", max_devices=3)
        for d in ("dev-T1", "dev-T2", "dev-T3"):
            dvb.bind_device_on_login(db, card, "web", d)
            _mk_session(db, card, d, "web")
        db.commit()
        assert len(dvb.bound_device_ids(db, card.id, "web")) == 3

        card.max_devices = 1
        trimmed = dvb.trim_bindings_to_limit(db, card)
        db.commit()
        assert trimmed == 2
        # 保留 bound_at 最新的 dev-T3，裁掉 T1/T2，且其会话失效
        assert dvb.bound_device_ids(db, card.id, "web") == {"dev-T3"}
        assert _active_tokens(db, card) == {"dev-T3"}
    finally:
        db.close()


def test_risk_ip_network_key():
    """风控 IP 网段归一化：私网不计、v4 /24、v6 /48。"""
    from api.risk_control import _ip_network_key
    assert _ip_network_key("192.168.1.10") is None        # 私网不作证据
    assert _ip_network_key("127.0.0.1") is None           # 回环
    assert _ip_network_key("10.0.0.5") is None
    assert _ip_network_key("not-an-ip") is None
    assert _ip_network_key("1.2.3.4") == _ip_network_key("1.2.3.99")     # 同 /24
    assert _ip_network_key("1.2.3.4") != _ip_network_key("1.2.4.4")      # 不同 /24
    v6 = "2606:4700:aaaa:bbbb:cccc:dddd:eeee:1111"
    v6_same48 = "2606:4700:aaaa:ffff:0000:0000:0000:2222"                  # 同 /48
    v6_diff48 = "2606:4700:bbbb:aaaa:0000:0000:0000:2222"                  # 不同 /48
    assert _ip_network_key(v6) == _ip_network_key(v6_same48)
    assert _ip_network_key(v6) != _ip_network_key(v6_diff48)
    assert _ip_network_key("2001:db8::1") is None                          # 文档段=非公网，不作证据


def test_risk_record_token_ip():
    """多网段判定：同 /24 不踢，跨公网网段才判定共享。"""
    from api import risk_control as rc
    rc.forget_token("tok-risk")
    assert rc.record_token_ip("tok-risk", "192.168.1.10") is False        # 私网不计
    assert rc.record_token_ip("tok-risk", "1.2.3.4") is False             # 首个公网网段
    assert rc.record_token_ip("tok-risk", "1.2.3.200") is False           # 同 /24 → 正常
    assert rc.record_token_ip("tok-risk", "5.6.7.8") is True              # 跨网段 → 共享
    rc.forget_token("tok-risk")


def test_ip_scope_key():
    """IP → 网络键归一化：私网→lan、v4 /24、v6 /48。"""
    assert dvb.ip_scope_key(None) is None
    assert dvb.ip_scope_key("") is None
    assert dvb.ip_scope_key("not-an-ip") is None
    assert dvb.ip_scope_key("unknown") is None
    assert dvb.ip_scope_key("192.168.1.10") == "lan"        # 私网/内网部署 → 同一网络
    assert dvb.ip_scope_key("127.0.0.1") == "lan"
    assert dvb.ip_scope_key("10.0.0.5") == "lan"
    assert dvb.ip_scope_key("1.2.3.4") == dvb.ip_scope_key("1.2.3.99")   # 同 /24 → 同网络
    assert dvb.ip_scope_key("1.2.3.4") != dvb.ip_scope_key("1.2.4.4")    # 不同 /24 → 换了网络
    v6a = "2606:4700:aaaa:bbbb:cccc:dddd:eeee:1111"
    v6b = "2606:4700:aaaa:ffff::2222"                        # 同 /48（后缀轮换不误踢）
    v6c = "2606:4700:bbbb:aaaa::2222"                        # 不同 /48
    assert dvb.ip_scope_key(v6a) == dvb.ip_scope_key(v6b)
    assert dvb.ip_scope_key(v6a) != dvb.ip_scope_key(v6c)


def test_is_ip_scope_key():
    """网络键与浏览器 deviceId 的判别（双向平滑切换的依据）。"""
    assert dvb.is_ip_scope_key("lan")
    assert dvb.is_ip_scope_key("1.2.3.0/24")
    assert dvb.is_ip_scope_key("2606:4700:aaaa::/48")
    assert not dvb.is_ip_scope_key("a1b2c3d4-browser-uuid")
    assert not dvb.is_ip_scope_key("")
    assert not dvb.is_ip_scope_key(None)


def test_check_session_ip_pure():
    """scope=ip 宽松校验：同网络不限设备，换网络才踢。"""
    now = datetime.now(timezone.utc)
    net = "1.2.3.0/24"

    # 历史/免设备会话 → 放行
    ok, code, _ = dvb.check_session_ip_pure(None, "web", now, net, set(), 7, now)
    assert ok and not code

    # device 模式遗留会话（存的是浏览器 ID）→ 放行（切换 scope 不误踢）
    ok, code, _ = dvb.check_session_ip_pure("browser-uuid-1234", "web", now, net, set(), 7, now)
    assert ok and not code

    # 同网络（不管哪个浏览器/设备发起）→ 放行
    ok, code, _ = dvb.check_session_ip_pure(net, "web", now, net, {net}, 7, now)
    assert ok and not code

    # 拿不到请求 IP → 不作为踢出证据，放行（fail-open）
    ok, code, _ = dvb.check_session_ip_pure(net, "web", now, None, {net}, 7, now)
    assert ok and not code

    # 换到另一个公网网段 → network_changed（重新登录即换绑）
    ok, code, _ = dvb.check_session_ip_pure(net, "web", now, "5.6.7.0/24", {net}, 7, now)
    assert not ok and code == "network_changed"

    # 绑定已被新网络顶掉 → kicked
    ok, code, msg = dvb.check_session_ip_pure(net, "web", now, net, {"5.6.7.0/24"}, 7, now)
    assert not ok and code == "kicked" and "其他网络" in msg

    # 空闲超时
    old = now - timedelta(days=8)
    ok, code, _ = dvb.check_session_ip_pure(net, "web", old, net, {net}, 7, now)
    assert not ok and code == "session_expired"

    # 内网部署：lan ↔ lan 一直放行
    ok, code, _ = dvb.check_session_ip_pure("lan", "web", now, "lan", {"lan"}, 7, now)
    assert ok and not code


def test_login_identity_default_scope_ip():
    """默认 scope=ip：登录标识取网络键；拿不到 IP 回退 deviceId。"""
    assert dvb.binding_scope() == dvb.SCOPE_IP               # 未配置 → 默认宽松
    assert dvb.login_identity("browser-id", "1.2.3.4") == "1.2.3.0/24"
    assert dvb.login_identity("browser-id", "192.168.0.2") == "lan"
    assert dvb.login_identity("browser-id", None) == "browser-id"   # 无 IP → 回退
    assert dvb.login_identity(None, None) is None


def test_ip_mode_bind_and_evict_integration():
    """ip 模式集成：同网络多浏览器共存一条绑定；换网络登录顶掉旧网络会话。"""
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0006", max_devices=1)
        net_home = dvb.ip_scope_key("1.2.3.4")               # 家里
        net_other = dvb.ip_scope_key("5.6.7.8")              # 别人家

        # 家里 Chrome 登录 → 绑定家庭网络
        dvb.bind_device_on_login(db, card, "web", net_home, "1.2.3.4")
        _mk_session(db, card, net_home, "web", token="tok-home-chrome")
        db.commit()

        # 家里 Edge 再登录（同网络）→ 不换绑、不踢人，共用同一绑定
        r = dvb.bind_device_on_login(db, card, "web", dvb.ip_scope_key("1.2.3.77"), "1.2.3.77")
        _mk_session(db, card, net_home, "web", token="tok-home-edge")
        db.commit()
        assert r["evicted"] == [] and r["kicked_sessions"] == 0
        assert dvb.bound_device_ids(db, card.id, "web") == {net_home}

        # 两个浏览器的会话都通过校验
        for _tok in ("tok-home-chrome", "tok-home-edge"):
            ok, code, _ = dvb.check_session_ip_pure(
                net_home, "web", datetime.now(timezone.utc),
                dvb.ip_scope_key("1.2.3.200"),               # 同 /24 内任意来源
                dvb.bound_device_ids(db, card.id, "web"), 7,
            )
            assert ok and not code

        # 换到另一个家庭 IP 登录 → 顶号：旧网络绑定被淘汰、其会话全部失效
        r2 = dvb.bind_device_on_login(db, card, "web", net_other, "5.6.7.8")
        db.commit()
        assert r2["evicted"] == [net_home]
        assert r2["kicked_sessions"] == 2                    # 家里两个浏览器会话都被踢
        assert dvb.bound_device_ids(db, card.id, "web") == {net_other}

        # 旧网络会话再来 → kicked
        ok, code, _ = dvb.check_session_ip_pure(
            net_home, "web", datetime.now(timezone.utc), net_home,
            dvb.bound_device_ids(db, card.id, "web"), 7,
        )
        assert not ok and code == "kicked"
    finally:
        db.close()


def test_resolve_client_ip():
    """真实 IP 解析：默认不信 XFF；开启 trust_proxy_header 后取首个合法 XFF IP。"""
    class _Cli:
        host = "9.9.9.9"

    class _Req:
        client = _Cli()
        headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1", "x-real-ip": "5.6.7.8"}

    # 默认（不信任代理头）→ 直连 IP
    assert dvb.resolve_client_ip(_Req()) == "9.9.9.9"

    # 开启 trust_proxy_header → 取 XFF 首个合法 IP
    from db.models import ApiConfig
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        db.add(ApiConfig(cfg_key="trust_proxy_header", cfg_value="1", category="settings"))
        db.commit()
        dvb.invalidate_cfg_cache()
        assert dvb.resolve_client_ip(_Req()) == "1.2.3.4"

        # XFF 全非法 → 回退 X-Real-IP
        class _Req2(_Req):
            headers = {"x-forwarded-for": "evil, <script>", "x-real-ip": "5.6.7.8"}
        assert dvb.resolve_client_ip(_Req2()) == "5.6.7.8"

        # 两个头都没有 → 回退直连 IP
        class _Req3(_Req):
            headers = {}
        assert dvb.resolve_client_ip(_Req3()) == "9.9.9.9"
    finally:
        # 还原配置，避免影响其它用例
        row = db.query(ApiConfig).filter_by(cfg_key="trust_proxy_header").first()
        if row:
            db.delete(row)
            db.commit()
        dvb.invalidate_cfg_cache()
        db.close()


if __name__ == "__main__":
    test_normalize_client_type()
    test_get_max_devices()
    test_check_session_pure()
    test_bind_and_evict_integration()
    test_unbind_and_kick_integration()
    test_check_session_integration()
    test_trim_bindings_to_limit()
    test_risk_ip_network_key()
    test_risk_record_token_ip()
    test_ip_scope_key()
    test_is_ip_scope_key()
    test_check_session_ip_pure()
    test_login_identity_default_scope_ip()
    test_ip_mode_bind_and_evict_integration()
    test_resolve_client_ip()
    print("ALL PASS")
