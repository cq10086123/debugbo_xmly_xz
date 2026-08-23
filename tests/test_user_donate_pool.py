"""回归测试：用户在卡密端登录账号后，副本自动写入后端供体池（按 uid 去重）。

不依赖真实 app.db，用独立临时 SQLite + monkeypatch account_manager.SessionLocal。
模拟 api/accounts.py 登录落库的两步：add_account(本卡) + add_backend_account(供体池)。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_temp_db():
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    engine = create_engine(
        f"sqlite:///{_tmp.name}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    from db.models import Base
    Base.metadata.create_all(engine)
    return engine, SessionLocal, _tmp.name


def test_login_mirrors_to_donor_pool_and_dedups():
    engine, SessionLocal, db_path = _make_temp_db()
    import core.account_manager as am
    am.SessionLocal = SessionLocal

    card_id = 1
    uid = "123456"
    nickname = "听友A"
    cookie1 = "uid=123456; token=aaa"
    cookie2 = "uid=123456; token=bbb"

    try:
        # 第一次登录（模拟 api/accounts.py:_handle_poll 成功分支）
        am.add_account(card_id=card_id, nickname=nickname, uid=uid,
                       cookie_str=cookie1, mobile="189****0000", is_vip=True)
        am.add_backend_account(nickname=nickname, uid=uid,
                               cookie_str=cookie1, mobile="189****0000", is_vip=True)

        backends = am.list_backend_accounts()
        assert len(backends) == 1, f"供体池应有 1 行，实际 {len(backends)}"
        assert backends[0]["uid"] == uid, "供体池 uid 应为登录账号 uid"
        assert backends[0]["is_vip"] is True, "供体池应记录 VIP 状态"

        # 同一 uid 再次登录（cookie 刷新）→ 应更新不新增
        am.add_account(card_id=card_id, nickname=nickname, uid=uid,
                       cookie_str=cookie2, mobile="189****0000", is_vip=True)
        am.add_backend_account(nickname=nickname, uid=uid,
                               cookie_str=cookie2, mobile="189****0000", is_vip=True)

        backends = am.list_backend_accounts()
        assert len(backends) == 1, f"同 uid 重复登录不应新增行，实际 {len(backends)}"

        # 验证供体池 cookie 已更新为最新
        db = SessionLocal()
        try:
            from db.models import BackendXmAccount
            b = db.query(BackendXmAccount).filter_by(uid=uid).first()
            assert b is not None and b.cookie_str == cookie2, "供体池 cookie 应刷新为最新"
        finally:
            db.close()

        # 不同 uid 登录 → 应新增第 2 行（不误伤）
        am.add_account(card_id=card_id, nickname="听友B", uid="999",
                       cookie_str="uid=999; token=ccc", is_vip=False)
        am.add_backend_account(nickname="听友B", uid="999",
                               cookie_str="uid=999; token=ccc", is_vip=False)
        assert len(am.list_backend_accounts()) == 2, "不同 uid 应新增第 2 行"

        print("PASS: 登录账号自动入库供体池 + uid 去重正确")
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
        try:
            os.unlink(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    test_login_mirrors_to_donor_pool_and_dedups()
    print("ALL PASS")
