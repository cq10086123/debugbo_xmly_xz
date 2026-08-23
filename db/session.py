"""数据库会话与连接管理（SQLite + WAL）"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

# 数据目录：优先用 core.config 的 DATA_DIR（兼容 Docker 挂载卷 /app/data）
try:
    from core.config import DATA_DIR as _DATA_DIR
except Exception:
    _DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))

_DB_PATH = Path(_DATA_DIR) / "app.db"

# 单文件 SQLite，开启 WAL 提升并发读写能力
# timeout 即 sqlite3 busy_timeout（秒）：多用户并发写遇到锁时自动重试，
# 而非立即抛 "database is locked"，避免下载记录/任务状态静默丢失。
_connect_args = {"check_same_thread": False, "timeout": 30}
_engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args=_connect_args,
    future=True,
)

Base = declarative_base()


# PRAGMA foreign_keys 是连接级设置，只对执行它的那条连接生效。
# 用 connect 事件挂钩，保证连接池里每条新建连接都开启外键约束（级联删除依赖它）。
@event.listens_for(_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=ON;")
    finally:
        cur.close()


def _enable_wal(engine):
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass


_enable_wal(_engine)

SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI 依赖：每个请求一个会话，结束自动关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_engine():
    return _engine
