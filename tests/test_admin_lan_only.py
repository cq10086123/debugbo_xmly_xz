"""回归测试：管理后台「局域网访问限制」开关（get_admin_lan_only）。

纯函数测试，通过 monkeypatch core.config._load_config_from_db 模拟 api_config 取值，
不依赖真实数据库连接。
覆盖：
- 缺省（key 不存在）→ 默认开启（True）
- "1" / "true" → 开启（True）
- "0" / "false" / "" → 关闭（False）
- 显式存 None → 开启（True，安全回退）
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as _cfg


def _with(db_dict):
    """临时替换 _load_config_from_db 并返回原值，测试后由调用方恢复。"""
    original = _cfg._load_config_from_db
    _cfg._load_config_from_db = lambda: db_dict
    return original


def _restore(original):
    _cfg._load_config_from_db = original


def test_default_when_missing():
    orig = _with({})
    try:
        assert _cfg.get_admin_lan_only() is True
    finally:
        _restore(orig)


def test_on_values():
    for v in ("1", "true", "TRUE", "yes"):
        orig = _with({"admin_lan_only": v})
        try:
            assert _cfg.get_admin_lan_only() is True, f"值 {v!r} 应视为开启"
        finally:
            _restore(orig)


def test_off_values():
    for v in ("0", "false", "FALSE", ""):
        orig = _with({"admin_lan_only": v})
        try:
            assert _cfg.get_admin_lan_only() is False, f"值 {v!r} 应视为关闭"
        finally:
            _restore(orig)


def test_explicit_none_falls_back_to_on():
    orig = _with({"admin_lan_only": None})
    try:
        assert _cfg.get_admin_lan_only() is True
    finally:
        _restore(orig)


if __name__ == "__main__":
    test_default_when_missing()
    test_on_values()
    test_off_values()
    test_explicit_none_falls_back_to_on()
    print("ALL PASS")
