"""回归测试：卡密下载模式权限校验（ensure_download_mode_allowed）。

纯函数测试，不依赖数据库 / FastAPI 应用实例。
覆盖：
- download_mode=both / 缺失 / 非法值 → 两种模式都放行
- download_mode=server → 仅 server 放行，local 抛 403
- download_mode=local  → 仅 local 放行，server 抛 403
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from api.deps import ensure_download_mode_allowed


def _auth(mode_value):
    return {"card_id": 1, "download_mode": mode_value}


def test_both_allows_both():
    # both / 缺失 / 非法值 都应放行（不抛异常）
    for mode in ("both", None, "weird", ""):
        ensure_download_mode_allowed(_auth(mode), "server")
        ensure_download_mode_allowed(_auth(mode), "local")


def test_server_allows_only_server():
    auth = _auth("server")
    ensure_download_mode_allowed(auth, "server")  # 放行
    try:
        ensure_download_mode_allowed(auth, "local")
        raise AssertionError("server 卡应拒绝 local 模式")
    except HTTPException as e:
        assert e.status_code == 403, f"应为 403，实际 {e.status_code}"


def test_local_allows_only_local():
    auth = _auth("local")
    ensure_download_mode_allowed(auth, "local")  # 放行
    try:
        ensure_download_mode_allowed(auth, "server")
        raise AssertionError("local 卡应拒绝 server 模式")
    except HTTPException as e:
        assert e.status_code == 403, f"应为 403，实际 {e.status_code}"


if __name__ == "__main__":
    test_both_allows_both()
    test_server_allows_only_server()
    test_local_allows_only_local()
    print("ALL PASS")
