"""夸克挂载同步：拷贝语义 + 卡密权限 + 书名匹配。

不依赖 FastAPI 应用实例。拷贝用例用临时目录替换 DOWNLOAD_DIR / QUARK_SYNC_DIR。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from api.deps import ensure_quark_sync_allowed
from core import config as _cfg
from core import quark_sync as qs


def test_permission_default_off():
    try:
        ensure_quark_sync_allowed({"quark_sync": False})
        raise AssertionError("未开通应 403")
    except HTTPException as e:
        assert e.status_code == 403
    try:
        ensure_quark_sync_allowed({})
        raise AssertionError("缺失字段应 403")
    except HTTPException as e:
        assert e.status_code == 403
    ensure_quark_sync_allowed({"quark_sync": True})  # 放行


def test_title_matches_sanitized_folder():
    # 下载器会去掉 : 等非法字符，任务标题和文件夹名可能不一致
    assert qs._title_matches_folder("三体:黑暗森林", "三体黑暗森林")
    assert qs._title_matches_folder("完美世界", "完美世界")
    assert not qs._title_matches_folder("遮天", "完美世界")


def test_validate_album_name():
    assert qs.validate_album_name("完美世界") == "完美世界"
    for bad in ("", "../x", "a/b", "a\\b", ".", ".."):
        try:
            qs.validate_album_name(bad)
            raise AssertionError(f"{bad!r} 应拒绝")
        except qs.QuarkSyncError:
            pass


def test_match_album():
    albums = [
        {"name": "完美世界", "count": 10},
        {"name": "遮天", "count": 3},
        {"name": "完美世界续", "count": 2},
    ]
    hit, cand = qs.match_album(albums, "完美世界")
    assert hit["name"] == "完美世界"
    hit, cand = qs.match_album(albums, "遮")
    assert hit["name"] == "遮天"
    hit, cand = qs.match_album(albums, "完美")
    assert hit is None and len(cand) == 2


def _setup_dirs():
    tmp = Path(tempfile.mkdtemp(prefix="quark_sync_"))
    dl = tmp / "downloads"
    qk = tmp / "quark"
    dl.mkdir()
    qk.mkdir()
    _cfg.DOWNLOAD_DIR = dl
    _cfg.QUARK_SYNC_DIR = qk
    return dl, qk


def test_copy_skip_same_size_keep_local():
    dl, qk = _setup_dirs()
    src = dl / "CARD" / "测试书"
    src.mkdir(parents=True)
    f1 = src / "01.mp3"
    f1.write_bytes(b"abc123")
    dest_dir = qk / "测试书"
    dest_dir.mkdir()
    (dest_dir / "01.mp3").write_bytes(b"abc123")  # 同大小 → 跳过

    job = {"total": 0, "done": 0, "copied": 0, "skipped": 0, "failed": 0, "errors": [], "current": ""}
    qs.copy_album(src, dest_dir, job)
    assert job["skipped"] == 1 and job["copied"] == 0 and job["failed"] == 0
    assert f1.exists(), "本地必须保留"


def test_copy_overwrite_different_size():
    dl, qk = _setup_dirs()
    src = dl / "CARD" / "测试书"
    src.mkdir(parents=True)
    (src / "01.mp3").write_bytes(b"new-content-xxxx")
    dest_dir = qk / "测试书"
    dest_dir.mkdir()
    (dest_dir / "01.mp3").write_bytes(b"old")

    job = {"total": 0, "done": 0, "copied": 0, "skipped": 0, "failed": 0, "errors": [], "current": ""}
    qs.copy_album(src, dest_dir, job)
    assert job["copied"] == 1 and job["failed"] == 0
    assert (dest_dir / "01.mp3").read_bytes() == b"new-content-xxxx"
    assert (src / "01.mp3").exists(), "本地必须保留"


def test_quark_dir_missing_not_created():
    _cfg.QUARK_SYNC_DIR = Path("/tmp/definitely-not-a-quark-mount-dir-xyz")
    try:
        qs.quark_dir()
        raise AssertionError("目录不存在应失败")
    except qs.QuarkSyncError as e:
        assert "不可用" in str(e)
    assert not _cfg.QUARK_SYNC_DIR.exists(), "不得 mkdir 伪造挂载点"


def test_scan_card_albums():
    dl, _qk = _setup_dirs()
    a = dl / "XM-TEST" / "一书"
    a.mkdir(parents=True)
    (a / "1.mp3").write_bytes(b"x")
    (a / "readme.txt").write_text("ignore")
    empty = dl / "XM-TEST" / "空目录"
    empty.mkdir()
    books = qs.scan_card_albums("XM-TEST")
    assert [b["name"] for b in books] == ["一书"]
    assert books[0]["count"] == 1


if __name__ == "__main__":
    test_permission_default_off()
    test_validate_album_name()
    test_match_album()
    test_copy_skip_same_size_keep_local()
    test_copy_overwrite_different_size()
    test_quark_dir_missing_not_created()
    test_scan_card_albums()
    print("ALL PASS")
