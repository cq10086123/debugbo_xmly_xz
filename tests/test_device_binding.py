"""回归测试：网络绑定（IP 归一化 + 顶号换绑 + 会话网络校验）。

覆盖：
- 纯函数：normalize_client_type / get_max_devices / ip_scope_key / check_session_pure
- 集成（临时 SQLite）：bind_network_on_login 顶号淘汰、unbind、kick、trim
- resolve_client_ip：反代头信任开关

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
    """网络键与旧版浏览器设备 ID 的判别（升级平滑放行的依据）。"""
    assert dvb.is_ip_scope_key("lan")
    assert dvb.is_ip_scope_key("1.2.3.0/24")
    assert dvb.is_ip_scope_key("2606:4700:aaaa::/48")
    assert not dvb.is_ip_scope_key("a1b2c3d4-browser-uuid")  # 旧版设备 ID
    assert not dvb.is_ip_scope_key("")
    assert not dvb.is_ip_scope_key(None)


def test_check_session_pure():
    """会话网络校验：同网络不限设备，换网络才拒。"""
    now = datetime.now(timezone.utc)
    net = "1.2.3.0/24"

    # 历史/免绑定会话 → 放行
    ok, code, _ = dvb.check_session_pure(None, now, net, set(), 7, now)
    assert ok and not code

    # 旧版遗留会话（存的是浏览器设备 ID）→ 放行（升级不误踢）
    ok, code, _ = dvb.check_session_pure("browser-uuid-1234", now, net, set(), 7, now)
    assert ok and not code

    # 同网络（不管哪个浏览器/设备发起）→ 放行
    ok, code, _ = dvb.check_session_pure(net, now, net, {net}, 7, now)
    assert ok and not code

    # 拿不到请求 IP → 不作为踢出证据，放行（fail-open）
    ok, code, _ = dvb.check_session_pure(net, now, None, {net}, 7, now)
    assert ok and not code

    # 换到另一个公网网段 → network_changed（重新登录即换绑）
    ok, code, _ = dvb.check_session_pure(net, now, "5.6.7.0/24", {net}, 7, now)
    assert not ok and code == "network_changed"

    # 绑定已被新网络顶掉 → kicked
    ok, code, msg = dvb.check_session_pure(net, now, net, {"5.6.7.0/24"}, 7, now)
    assert not ok and code == "kicked" and "其他网络" in msg

    # 空闲超时
    old = now - timedelta(days=8)
    ok, code, _ = dvb.check_session_pure(net, old, net, {net}, 7, now)
    assert not ok and code == "session_expired"

    # 内网部署：lan ↔ lan 一直放行
    ok, code, _ = dvb.check_session_pure("lan", now, "lan", {"lan"}, 7, now)
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


def _mk_session(db, card, net_key, client_type="web", token=None):
    from db.models import Session as CardSession
    token = token or f"tok-{net_key}-{client_type}-{datetime.now().timestamp()}"
    db.add(CardSession(token=token, card_id=card.id, is_active=True,
                       client_type=client_type, device_id=net_key,
                       last_active_at=datetime.now(timezone.utc)))
    db.commit()
    return token


def _active_nets(db, card):
    from db.models import Session as CardSession
    rows = db.query(CardSession).filter_by(card_id=card.id, is_active=True).all()
    return {r.device_id for r in rows}


def test_bind_and_evict_integration():
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0001", max_devices=1)
        net_home = dvb.ip_scope_key("1.2.3.4")               # 家里
        net_other = dvb.ip_scope_key("5.6.7.8")              # 别人家

        # 1) 家里网页登录 → 绑定家庭网络
        r1 = dvb.bind_network_on_login(db, card, "web", net_home, "1.2.3.4")
        db.commit()
        assert r1["evicted"] == []
        assert dvb.bound_network_keys(db, card.id) == {net_home}

        # 2) 家里插件/其他浏览器再登录（同 /24）→ 复用同一绑定，不顶号
        _mk_session(db, card, net_home, "web", token="tok-home-chrome")
        r2 = dvb.bind_network_on_login(db, card, "extension", dvb.ip_scope_key("1.2.3.77"), "1.2.3.77")
        _mk_session(db, card, net_home, "extension", token="tok-home-ext")
        db.commit()
        assert r2["evicted"] == [] and r2["kicked_sessions"] == 0
        assert dvb.bound_network_keys(db, card.id) == {net_home}   # 仍只有一条绑定

        # 3) 顶号换绑：别人家登录 → 家庭网络绑定被淘汰，其全部会话失效
        r3 = dvb.bind_network_on_login(db, card, "web", net_other, "5.6.7.8")
        db.commit()
        assert r3["evicted"] == [net_home]
        assert r3["kicked_sessions"] == 2                    # 家里网页+插件会话都被踢
        assert dvb.bound_network_keys(db, card.id) == {net_other}
        assert net_home not in _active_nets(db, card)

        # 4) 同网络重复登录：不换绑，仅续活
        r4 = dvb.bind_network_on_login(db, card, "web", net_other, "5.6.7.9")
        db.commit()
        assert r4["evicted"] == []

        # 5) 多网络卡（max_devices=2）：第二个网络共存，第三个踢最早那个
        card2 = _mk_card(db, "XM-TEST-0002", max_devices=2)
        n1, n2, n3 = dvb.ip_scope_key("11.1.1.1"), dvb.ip_scope_key("22.2.2.2"), dvb.ip_scope_key("33.3.3.3")
        dvb.bind_network_on_login(db, card2, "web", n1)
        dvb.bind_network_on_login(db, card2, "web", n2)
        db.commit()
        assert dvb.bound_network_keys(db, card2.id) == {n1, n2}
        r5 = dvb.bind_network_on_login(db, card2, "web", n3)
        db.commit()
        assert r5["evicted"] == [n1]                         # bound_at 最早的被淘汰
        assert dvb.bound_network_keys(db, card2.id) == {n2, n3}
    finally:
        db.close()


def test_unbind_and_kick_integration():
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        from db.models import DeviceBinding
        card = _mk_card(db, "XM-TEST-0003", max_devices=2)
        na, nb = dvb.ip_scope_key("44.4.4.4"), dvb.ip_scope_key("55.5.5.5")
        dvb.bind_network_on_login(db, card, "web", na)
        dvb.bind_network_on_login(db, card, "web", nb)
        _mk_session(db, card, na, "web", token="tok-na")
        _mk_session(db, card, nb, "web", token="tok-nb")
        db.commit()

        # 解绑单个网络：其会话一并失效，另一网络不动
        n = dvb.unbind_device(db, card, na)
        db.commit()
        assert n == 1
        assert dvb.bound_network_keys(db, card.id) == {nb}
        assert na not in _active_nets(db, card)
        assert nb in _active_nets(db, card)

        # 踢下线（保留绑定）：会话失效但绑定还在
        n2 = dvb.kick_all_sessions(db, card)
        db.commit()
        assert n2 == 1                                       # 只剩 nb 的会话
        assert _active_nets(db, card) == set()
        assert dvb.bound_network_keys(db, card.id) == {nb}

        # 全部解绑：绑定清空 + 兜底清会话
        _mk_session(db, card, nb, "web", token="tok-nb2")
        n3 = dvb.unbind_all(db, card)
        db.commit()
        assert n3 == 1
        assert db.query(DeviceBinding).filter_by(card_id=card.id).count() == 0
        assert _active_nets(db, card) == set()
    finally:
        db.close()


def test_trim_bindings_to_limit():
    """管理员下调 max_devices：超额绑定即时裁剪（保留最新）。"""
    SessionLocal = _setup_db()
    db = SessionLocal()
    try:
        card = _mk_card(db, "XM-TEST-0005", max_devices=3)
        nets = [dvb.ip_scope_key(f"66.{i}.1.1") for i in (1, 2, 3)]
        for n in nets:
            dvb.bind_network_on_login(db, card, "web", n)
            _mk_session(db, card, n, "web")
        db.commit()
        assert len(dvb.bound_network_keys(db, card.id)) == 3

        card.max_devices = 1
        trimmed = dvb.trim_bindings_to_limit(db, card)
        db.commit()
        assert trimmed == 2
        # 保留 bound_at 最新的，裁掉前两个，且其会话失效
        assert dvb.bound_network_keys(db, card.id) == {nets[2]}
        assert _active_nets(db, card) == {nets[2]}
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


# ════════════════════════════════════════
#  风控（token 多网段兜底）
# ════════════════════════════════════════
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


if __name__ == "__main__":
    test_normalize_client_type()
    test_get_max_devices()
    test_ip_scope_key()
    test_is_ip_scope_key()
    test_check_session_pure()
    test_bind_and_evict_integration()
    test_unbind_and_kick_integration()
    test_trim_bindings_to_limit()
    test_resolve_client_ip()
    test_risk_ip_network_key()
    test_risk_record_token_ip()
    print("ALL PASS")
