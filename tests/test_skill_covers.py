"""书籍封面：接口无关的通用提取 + 签名代理。

核心契约：**新增第三方接口无需改任何代码，封面功能自动生效**。
因此测试重点是「任意字段命名的搜索结果都能挖出封面」，
而不是针对某个具体接口断言。

运行：python -m pytest tests/test_skill_covers.py -v
"""
import asyncio
import time
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
            code=f"CV-{RUN}", status="used", expiry_type="fixed",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            skill_token=issue_skill_token(),
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        auth = {"card_id": card.id, "code": card.code, "token": card.skill_token,
                "bound": [], "download_mode": "both", "quark_sync": False}
    finally:
        db.close()
    return auth


def _run(coro):
    return asyncio.run(coro)


# ── ① 只读契约字段：字段映射是搜索脚本的职责，运行时绝不猜 ──
@pytest.mark.parametrize("payload,expected", [
    # 契约字段（script_examples.py 约定 bookImage；cover 为归一化标准名）
    ({"cover": "//fdfs.xmcdn.com/a.jpg"}, "https://fdfs.xmcdn.com/a.jpg"),
    ({"bookImage": "https://x.com/b.png"}, "https://x.com/b.png"),
    ({"cover": "", "bookImage": "https://x.com/b.png"}, "https://x.com/b.png"),
    ({"cover": "data:image/png;base64,iVBORw0KG"}, "data:image/png;base64,iVBORw0KG"),
    # 非契约字段一律不认 —— 由脚本负责映射
    ({"pic": "https://x.com/c.webp"}, ""),
    ({"imgUrl": "https://x.com/d.jpg"}, ""),
    ({"thumbnail": "//x.com/e.jpg"}, ""),
    ({"albumCover": "https://x.com/f.jpeg"}, ""),
    ({"novel": {"cover": "https://x.com/h.jpg"}}, ""),
    # 无封面 / 脏数据
    ({"title": "无图"}, ""),
    ({"cover": ""}, ""),
    ({"cover": "not-a-url"}, ""),
    ({"cover": "暂无封面"}, ""),
    ({"cover": None}, ""),
    ({"cover": 12345}, ""),
    ({}, ""),
])
def test_extract_cover_reads_contract_fields_only(payload, expected):
    from core.cover import extract_cover
    assert extract_cover(payload) == expected


@pytest.mark.parametrize("noise_key", [
    "avatar", "bookAnchorAvatar", "logo", "site_logo",
    "qrcode_img", "shareImage", "icon", "banner_img",
])
def test_extract_cover_never_mistakes_non_cover_images(noise_key):
    """回归：贪心匹配曾把主播头像/站点 logo/二维码/分享图当封面显示。

    用户正是靠封面区分同名书籍，给错图比不给图更糟。
    """
    from core.cover import extract_cover
    item = {"id": "1", "bookTitle": "书", noise_key: "https://x.com/noise.jpg"}
    assert extract_cover(item) == "", f"{noise_key} 不应被当作封面"


def test_contract_field_wins_over_noise():
    """同时存在契约字段与噪声图片时，必须取契约字段。"""
    from core.cover import extract_cover
    item = {"id": "1", "bookTitle": "书",
            "avatar": "https://x.com/头像.jpg",
            "bookImage": "https://x.com/封面.jpg"}
    assert extract_cover(item) == "https://x.com/封面.jpg"


def test_extract_cover_survives_bad_input():
    from core.cover import extract_cover
    for bad in (None, "string", 123, [], set()):
        assert extract_cover(bad) == ""


# ── ② 签名与校验 ──
def test_sign_verify_roundtrip():
    from core import cover as c
    url = "https://img.example.com/a.jpg?x=1&y=2"
    signed = c.sign(url)
    assert signed.startswith("/api/skills/cover?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(signed).query)
    assert c.verify(q["u"][0], q["e"][0], q["s"][0]) == url


def test_verify_rejects_tampering_and_expiry():
    from core import cover as c
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(c.sign("https://img.example.com/a.jpg")).query)
    u, e, s = q["u"][0], q["e"][0], q["s"][0]
    # 改 URL 但沿用旧签名 → 拒绝（防止被当成任意 URL 代理）
    from core.cover import _b64e
    assert c.verify(_b64e("https://evil.com/x.jpg"), e, s) is None
    # 改签名 → 拒绝
    assert c.verify(u, e, "deadbeef" * 4) is None
    # 过期 → 拒绝
    expired = str(int(time.time()) - 10)
    assert c.verify(u, expired, c._sign(u, int(expired))) is None


def test_ssrf_guard_blocks_internal_targets():
    from core.cover import is_safe_fetch_target
    for bad in ("http://127.0.0.1/a.jpg", "http://localhost/a.jpg",
                "http://192.168.1.1/a.jpg", "http://169.254.169.254/latest/meta-data",
                "file:///etc/passwd", "ftp://x.com/a.jpg", "http://[::1]/a.jpg"):
        assert is_safe_fetch_target(bad) is False, f"应拦截: {bad}"


def test_cover_proxy_rejects_bad_signature(env):
    from api.skills import skill_cover_proxy
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(skill_cover_proxy(u="aaa", e="99999999999", s="bad"))
    assert ei.value.status_code == 403


# ── ③ 端到端：搜索 → show_covers（模拟任意第三方接口） ──
def _fake_search(monkeypatch, docs):
    """把任意形状的第三方接口返回塞进 intf_search，验证无需改代码即可支持。"""
    async def fake(name, keyword, page=1, auth=None):
        return {"success": True, "results": docs}
    import api.interfaces as intf
    monkeypatch.setattr(intf, "intf_search", fake)


def test_search_then_show_covers_for_new_interface(env, monkeypatch):
    """模拟「以后才添加」的接口：脚本按契约映射到 cover 后，全链路可用。

    适配器归一化后对外就是 cover（见 _normalize_book），
    这里直接以归一化结果作为 intf_search 的返回。
    """
    from api.skills import skill_search_books, skill_show_covers, SearchRequest, ShowCoversRequest

    _fake_search(monkeypatch, [
        {"id": "1", "title": "斗破苍穹", "author": "A", "cover": "https://x.com/1.jpg"},
        {"id": "2", "title": "斗破苍穹", "author": "B", "cover": "https://x.com/2.jpg"},
        {"id": "3", "title": "斗破苍穹", "author": "C"},  # 无封面
    ])
    r = _run(skill_search_books(SearchRequest(keyword="斗破苍穹", source="brand_new_api"), auth=env))
    assert r["success"] is True
    assert [x["has_cover"] for x in r["results"]] == [True, True, False]
    assert [x["index"] for x in r["results"]] == [1, 2, 3]
    # 内部字段不应泄漏给 AI
    assert all("_cover" not in x for x in r["results"])

    # 「前三本封面」
    r2 = _run(skill_show_covers(ShowCoversRequest(count=3), auth=env))
    assert r2["success"] is True
    assert len(r2["covers"]) == 2
    assert len(r2["missing"]) == 1
    assert all(c["image_url"].startswith("/api/skills/cover?") for c in r2["covers"])

    # 「第 2 本的封面」
    r3 = _run(skill_show_covers(ShowCoversRequest(index=2), auth=env))
    assert r3["success"] is True
    assert len(r3["covers"]) == 1
    assert r3["covers"][0]["index"] == 2
    assert r3["covers"][0]["album_id"] == "2"


def test_show_covers_index_out_of_range(env, monkeypatch):
    from api.skills import skill_search_books, skill_show_covers, SearchRequest, ShowCoversRequest
    _fake_search(monkeypatch, [{"id": "1", "title": "书", "cover": "https://x.com/1.jpg"}])
    _run(skill_search_books(SearchRequest(keyword="书", source="s"), auth=env))
    r = _run(skill_show_covers(ShowCoversRequest(index=9), auth=env))
    assert r["success"] is False
    assert "超出范围" in r["error"]


def test_show_covers_without_search(env):
    from api.skills import skill_show_covers, ShowCoversRequest
    import api.skills as sk
    sk._last_search.pop(env["card_id"], None)
    r = _run(skill_show_covers(ShowCoversRequest(count=1), auth=env))
    assert r["success"] is False
    assert "search_books" in r["suggestion"]


def test_cover_cache_is_per_card(env, monkeypatch):
    """A 卡的搜索结果不得被 B 卡看到。"""
    from api.skills import skill_search_books, skill_show_covers, SearchRequest, ShowCoversRequest
    _fake_search(monkeypatch, [{"id": "1", "title": "私密书", "cover": "https://x.com/1.jpg"}])
    _run(skill_search_books(SearchRequest(keyword="x", source="s"), auth=env))

    other = dict(env, card_id=env["card_id"] + 99999)
    r = _run(skill_show_covers(ShowCoversRequest(count=1), auth=other))
    assert r["success"] is False, "不得跨卡密读到他人搜索结果"


def test_search_without_covers_has_no_suggestion(env, monkeypatch):
    """一本都没封面时不应误导 AI 去调 show_covers。"""
    from api.skills import skill_search_books, SearchRequest
    _fake_search(monkeypatch, [{"id": "1", "title": "无图书"}])
    r = _run(skill_search_books(SearchRequest(keyword="x", source="s"), auth=env))
    assert r["success"] is True
    assert all(not x["has_cover"] for x in r["results"])
    assert "suggestion" not in r


# ── ④ 后台添加接口时的封面自检（防止静默失败） ──
def test_describe_suggests_likely_cover_field():
    """未映射时，诊断应点名疑似封面字段并给出契约修法。"""
    from core.cover import describe_cover_detection
    msg = describe_cover_detection({"id": "1", "bookTitle": "书", "pic": "https://x.com/a.jpg"})
    assert "pic" in msg
    assert "bookImage" in msg
    assert "疑似封面字段" in msg


def test_describe_flags_noise_images_as_unlikely():
    """头像/logo 这类图片必须被标注为「不像封面」，避免误导管理员去映射。"""
    from core.cover import describe_cover_detection
    msg = describe_cover_detection({"id": "1", "bookTitle": "书",
                                    "avatar": "https://x.com/头像.jpg"})
    assert "avatar" in msg
    assert "头像" in msg or "而非书籍封面" in msg


def test_describe_when_no_image_at_all():
    from core.cover import describe_cover_detection
    msg = describe_cover_detection({"id": "1", "bookTitle": "书"})
    assert "未发现任何图片 URL" in msg


def test_describe_ignores_contract_fields_when_scanning():
    """已是契约字段的键不该出现在「建议映射」里。"""
    from core.cover import describe_cover_detection
    msg = describe_cover_detection({"id": "1", "cover": "not-a-url"})
    assert "['cover']" not in msg


def test_describe_handles_non_dict():
    from core.cover import describe_cover_detection
    assert "无法读取封面" in describe_cover_detection("not-a-dict")


def test_admin_test_endpoint_reports_cover_status(monkeypatch):
    """后台「测试接口」必须显式报告封面识别情况。"""
    from api.interfaces import test_interface, InterfaceTest
    import api.interfaces as intf

    class FakeAdapter:
        def search_books(self, kw, page=1):
            return {"success": True, "results": [
                {"id": "1", "title": "书", "cover": "https://x.com/a.jpg"},
                {"id": "2", "title": "书2"},
            ]}
        def get_chapters(self, b): return {"success": True, "tracks": []}
        def get_audio_url(self, b, c): return ""

    monkeypatch.setattr(intf.manager, "get_adapter_any", lambda n: FakeAdapter())
    r = _run(test_interface("x", InterfaceTest(keyword="书"), _=True))
    cov = r["result"]["search"]["cover"]
    assert cov["ok"] is True
    assert cov["detected"] == 1 and cov["total"] == 2


def test_admin_test_endpoint_warns_on_blind_spot(monkeypatch):
    """字段名无语义 → 后台必须给出警告与修改建议，而不是静默通过。"""
    from api.interfaces import test_interface, InterfaceTest
    import api.interfaces as intf

    class BlindAdapter:
        def search_books(self, kw, page=1):
            return {"success": True, "results": [
                {"id": "1", "title": "书", "bg": "https://x.com/a.jpg"}]}
        def get_chapters(self, b): return {"success": True, "tracks": []}
        def get_audio_url(self, b, c): return ""

    monkeypatch.setattr(intf.manager, "get_adapter_any", lambda n: BlindAdapter())
    r = _run(test_interface("x", InterfaceTest(keyword="书"), _=True))
    cov = r["result"]["search"]["cover"]
    assert cov["ok"] is False
    assert "bg" in cov["hint"]


# ── ⑤ 归一化层：后台添加接口的统一收口点 ──
@pytest.mark.parametrize("field", ["cover", "bookImage"])
def test_normalize_book_accepts_contract_fields(field):
    """脚本按契约输出 bookImage（或 cover）时，归一化产出标准 cover。

    这是网页端 <img :src="item.cover"> 与 AI 封面功能共同依赖的收口点。
    """
    from core.interface_manager import ScriptAdapter
    norm = ScriptAdapter._normalize_book(
        {"id": "1", "bookTitle": "书", field: "https://x.com/a.jpg"})
    assert norm["cover"] == "https://x.com/a.jpg", f"契约字段 {field} 未被归一化"
    # bookImage 是前端/批处理的别名，应与 cover 保持一致
    assert norm["bookImage"] == norm["cover"]


@pytest.mark.parametrize("field", ["pic", "thumbnail", "albumCover", "avatar", "logo"])
def test_normalize_book_ignores_unmapped_fields(field):
    """非契约字段不做猜测——映射是搜索脚本的职责。

    这样既避免把主播头像/站点 logo 误当封面，也让契约保持单一清晰。
    """
    from core.interface_manager import ScriptAdapter
    norm = ScriptAdapter._normalize_book(
        {"id": "1", "bookTitle": "书", field: "https://x.com/a.jpg"})
    assert norm["cover"] == ""
    # 原始字段仍应透传给章节脚本（契约要求），只是不当封面用
    assert norm[field] == "https://x.com/a.jpg"


def test_normalize_book_fixes_protocol_relative_url():
    """//x.com/a.jpg 必须补成 https:，否则前端 img 加载失败。"""
    from core.interface_manager import ScriptAdapter
    norm = ScriptAdapter._normalize_book(
        {"id": "1", "bookTitle": "书", "cover": "//x.com/a.jpg"})
    assert norm["cover"] == "https://x.com/a.jpg"


def test_normalize_book_rejects_garbage_cover():
    from core.interface_manager import ScriptAdapter
    for junk in ("not-a-url", "", None, 123):
        norm = ScriptAdapter._normalize_book(
            {"id": "1", "bookTitle": "书", "cover": junk})
        assert norm["cover"] == ""


def test_normalize_book_keeps_other_fields_intact():
    """封面改动不得影响 id/title/author/count 等既有归一化行为。"""
    from core.interface_manager import ScriptAdapter
    norm = ScriptAdapter._normalize_book({
        "id": 7, "bookTitle": "书名", "bookAnchor": "主播",
        "count": "12", "bookDesc": "简介", "albumId": "custom",
    })
    assert norm["albumId"] == "7" and norm["id"] == "7"
    assert norm["title"] == "书名" and norm["bookTitle"] == "书名"
    assert norm["author"] == "主播" and norm["bookAnchor"] == "主播"
    assert norm["count"] == 12 and norm["trackCount"] == 12
    assert norm["intro"] == "简介"
