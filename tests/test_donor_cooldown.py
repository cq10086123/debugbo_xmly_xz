"""回归测试：供体账号冷却按 uid 全局生效（审查报告 2026-08-17 中危 D / 方案 A）

不依赖真实 app.db：用临时文件 SQLite 搭独立库，monkeypatch 被测模块的
SessionLocal 后跑。直接 `python tests/test_donor_cooldown.py` 即可。
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 把项目根目录加入 import 路径（脚本在 tests/ 下运行时不会自动包含）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 1) 建一个独立的临时 SQLite，避免污染线上 app.db
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
_engine = create_engine(
    f"sqlite:///{_tmp.name}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, future=True)

# 2) 建表（XimalayaAccount / BackendXmAccount 等都挂在同一个 Base 上）
from db.models import Base  # noqa: E402

Base.metadata.create_all(_engine)

# 3) 在导入被测模块前替换它的 SessionLocal
import core.account_manager as am  # noqa: E402

am.SessionLocal = SessionLocal


def _add(card_id: int, uid: str, injected: bool = False) -> None:
    db = SessionLocal()
    try:
        db.add(
            am.XimalayaAccount(
                card_id=card_id,
                acc_id=f"acc_{uid}",
                uid=uid,
                nickname="t",
                cookie_str="x",
                injected=injected,
                added_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


def test_global_cooldown_across_cards():
    """同一供体 uid 注入到两张卡，任一张触发限流，另一张也须被冷却保护。"""
    _add(1, "10086", injected=True)
    _add(2, "10086", injected=True)  # 同一供体复制进另一张卡

    # 卡1 的副本触发限流
    am.set_rate_limited(1, "acc_10086")

    active_a = am.get_active_account_ids(1)
    active_b = am.get_active_account_ids(2)

    assert "acc_10086" not in active_a, "卡1副本应进入冷却"
    assert "acc_10086" not in active_b, "卡2副本应被全局冷却保护（共享 VIP 不被并发拖垮）"
    print("PASS: 全局冷却生效 —— 同名 uid 跨卡副本均进入冷却")


def test_single_card_only():
    """非共享 uid（各自独有账号）只冷却自身，不影响其它卡。"""
    _add(3, "200")
    _add(4, "300")

    am.set_rate_limited(3, "acc_200")

    assert "acc_200" not in am.get_active_account_ids(3), "卡3自身应冷却"
    assert "acc_300" in am.get_active_account_ids(4), "卡4的独有账号不应被误伤"


if __name__ == "__main__":
    try:
        test_global_cooldown_across_cards()
        test_single_card_only()
        print("ALL PASS")
    finally:
        try:
            _engine.dispose()
        except Exception:
            pass
        try:
            os.unlink(_tmp.name)
        except Exception:
            pass
