"""数据库初始化：建表 + 一次性配置迁移 + 默认管理员

启动流程：
1. 创建所有表（幂等）。
2. 若 api_config 为空，从 config.json 导入一次 itingshu_* / auto_retry_* 等键值。
3. 若 ximalaya_accounts 为空且 accounts.json 存在，迁移账号。
4. 若无管理员账号，创建默认管理员 admin / admin123（首次运行请尽快修改）。
"""

import json
import logging
from pathlib import Path

import bcrypt

from db.session import Base, _engine, SessionLocal
from db.models import (
    ApiConfig, Admin, XimalayaAccount, CardLog, Card, Interface, LocalTask,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


# ── 需要迁移进 api_config 的配置键（仅全局任务策略，接口密钥已改由接口管理维护）──
_CONFIG_KEYS = (
    "auto_retry_enabled",
    "auto_retry_interval_minutes",
    "auto_retry_max_rounds",
)


def init_db():
    Base.metadata.create_all(_engine)
    _migrate_card_columns()
    _migrate_session_columns()
    _migrate_backend_columns()
    _migrate_backend_xm_columns()
    _migrate_local_task_columns()
    _migrate_config_json()
    _migrate_accounts_json()
    _ensure_default_admin()
    _ensure_default_interfaces()
    _cleanup_global_itingshu()
    _log_event("startup", None, "数据库初始化完成")


def _migrate_card_columns():
    """轻量迁移：为已存在的 cards 表补充新增列（create_all 不会改已有表）。

    目前补充：
    - bound_interfaces TEXT  卡密绑定的接口名列表（JSON 数组，NULL=不限制）
    - download_mode     TEXT  下载模式权限（server/local/both，NULL=both）
    - max_devices       INTEGER  设备绑定数上限（NULL=1，见 core/device_binding.py）
    - quark_sync        INTEGER  是否允许同步到夸克挂载（0=关，默认关）
    - skill_token       TEXT     SKILL 专用长期凭证（NULL=尚未签发，UNIQUE）
    """
    needed = {
        "bound_interfaces": "TEXT",
        "download_mode": "TEXT",
        "max_devices": "INTEGER",
        "quark_sync": "INTEGER NOT NULL DEFAULT 0",
        "skill_token": "TEXT",
    }
    with _engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(cards)").fetchall()}
        for name, ddl in needed.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")
                logger.info(f"cards 表补充列: {name}")
        # UNIQUE 允许多个 NULL；存量卡未签发 SKILL 钥匙时 skill_token 为空
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_cards_skill_token ON cards (skill_token)"
        )
        conn.commit()


def _migrate_session_columns():
    """轻量迁移：为已存在的 sessions 表补充设备绑定相关列（全部可空，存量会话不受影响）。

    - client_type TEXT     登录端类型（web|extension，NULL=历史会话，设备校验放行）
    - device_id   TEXT     登录设备 ID（NULL=历史会话）
    - ip          TEXT     登录时出口 IP（风控用）
    """
    needed = {"client_type": "TEXT", "device_id": "TEXT", "ip": "TEXT"}
    with _engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sessions)").fetchall()}
        for name, ddl in needed.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
                logger.info(f"sessions 表补充列: {name}")
        conn.commit()


def _migrate_backend_columns():
    """轻量迁移：为已存在的 ximalaya_accounts 表补充注入相关列。

    - injected        INTEGER NOT NULL DEFAULT 0  是否由后端供体池注入
    - backend_src_id  INTEGER                     来源 BackendXmAccount.id
    """
    needed = {
        "injected": "INTEGER NOT NULL DEFAULT 0",
        "backend_src_id": "INTEGER",
    }
    with _engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(ximalaya_accounts)").fetchall()}
        for name, ddl in needed.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE ximalaya_accounts ADD COLUMN {name} {ddl}")
                logger.info(f"ximalaya_accounts 表补充列: {name}")
        conn.commit()


def _migrate_backend_xm_columns():
    """轻量迁移：为已存在的 backend_xm_accounts 表补充验证相关列。

    - last_verified_at  DATETIME  最后验证时间
    - is_valid          INTEGER   验证结果（1=有效, 0=失效, NULL=未验证）
    """
    needed = {
        "last_verified_at": "DATETIME",
        "is_valid": "INTEGER",
    }
    with _engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(backend_xm_accounts)").fetchall()}
        for name, ddl in needed.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE backend_xm_accounts ADD COLUMN {name} {ddl}")
                logger.info(f"backend_xm_accounts 表补充列: {name}")
        conn.commit()


def _migrate_local_task_columns():
    """轻量迁移：为已存在的 local_tasks 表补充下载槽租约相关列（全部可空，存量任务不受影响）。

    - claim_id      TEXT     本次 claim 凭证
    - progress      TEXT     JSON {total, done, failed}
    - failed_list   TEXT     JSON 失败集列表
    - error         TEXT     错误/提示
    - claimed_at    DATETIME
    - heartbeat_at  DATETIME
    - lease_until   DATETIME
    - finished_at   DATETIME
    """
    needed = {
        "claim_id": "TEXT",
        "progress": "TEXT",
        "failed_list": "TEXT",
        "error": "TEXT",
        "claimed_at": "DATETIME",
        "heartbeat_at": "DATETIME",
        "lease_until": "DATETIME",
        "finished_at": "DATETIME",
    }
    with _engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(local_tasks)").fetchall()}
        for name, ddl in needed.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE local_tasks ADD COLUMN {name} {ddl}")
                logger.info(f"local_tasks 表补充列: {name}")
        conn.commit()


def _cleanup_global_itingshu():
    """清理历史遗留的全局 itingshu_* 密钥行（接口密钥已改由接口管理维护，全局配置不再使用）。"""
    db = SessionLocal()
    try:
        deleted = db.query(ApiConfig).filter(ApiConfig.cfg_key.like("itingshu_%")).delete(
            synchronize_session=False
        )
        if deleted:
            db.commit()
            logger.info(f"已清理 {deleted} 条全局 itingshu 密钥配置（迁移至接口管理）")
    finally:
        db.close()


def _ensure_default_interfaces():
    """注入内置接口（仅 official）。

    设计：只有官方接口作为内置预置；其余音源（第三方 itingshu、自定义 HTTP 等）
    一律不在系统层内置，全部由用户在后台「接口管理」中自行添加。

    兼容性迁移：删除历史版本自动注入的 builtin itingshu 占位行
    （该行为系统占位、非用户数据）。用户自建的 itingshu（builtin=False）不受影响。
    """
    db = SessionLocal()
    try:
        # 仅官方接口内置预置
        if not db.query(Interface).filter_by(name="official").first():
            db.add(Interface(
                name="official",
                display_name="官方接口（需 VIP 账号）",
                type="official",
                enabled=True,
                builtin=True,
                priority=0,
                description="喜马拉雅官方接口，基于浏览器自动化，需先扫码登录 VIP 账号。",
            ))
        # 清理旧版自动注入的 itingshu 内置占位（builtin=True，非用户创建）
        legacy = db.query(Interface).filter_by(name="itingshu", builtin=True).first()
        if legacy:
            db.delete(legacy)
            logger.info("已清理历史内置 itingshu 占位，改由用户自行添加")
        db.commit()
        logger.info("默认接口注入完成（仅 official 内置）")
    finally:
        db.close()


def _migrate_config_json():
    """config.json → api_config 一次性迁移"""
    config_file = BASE_DIR / "config.json"
    if not config_file.exists():
        return
    try:
        saved = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return

    db = SessionLocal()
    try:
        existing = {row.cfg_key for row in db.query(ApiConfig).all()}
        for key in _CONFIG_KEYS:
            if key in saved and key not in existing:
                value = saved[key]
                if isinstance(value, bool):
                    value = "1" if value else "0"
                db.add(ApiConfig(
                    cfg_key=key,
                    cfg_value=str(value) if value is not None else None,
                    category="settings",
                ))
        db.commit()
        logger.info("config.json → api_config 迁移完成")
    finally:
        db.close()


def _migrate_accounts_json():
    """accounts.json → ximalaya_accounts 一次性迁移（可选但建议）"""
    accounts_file = BASE_DIR / "data" / "accounts.json"
    if not accounts_file.exists():
        accounts_file = BASE_DIR / "accounts.json"
    if not accounts_file.exists():
        return
    try:
        accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(accounts, list):
        return

    db = SessionLocal()
    try:
        if db.query(XimalayaAccount).count() > 0:
            return
        for acc in accounts:
            db.add(XimalayaAccount(
                nickname=acc.get("nickname"),
                uid=str(acc.get("uid")) if acc.get("uid") else None,
                mobile=acc.get("mobile"),
                cookie_str=acc.get("cookie_str"),
                is_vip=bool(acc.get("is_vip", False)),
            ))
        db.commit()
        logger.info(f"accounts.json → ximalaya_accounts 迁移 {len(accounts)} 个账号")
    finally:
        db.close()


def _ensure_default_admin():
    db = SessionLocal()
    try:
        if db.query(Admin).count() == 0:
            pw_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
            db.add(Admin(username="admin", password_hash=pw_hash))
            db.commit()
            logger.warning("已创建默认管理员 admin / admin123，请尽快在管理后台修改密码！")
    finally:
        db.close()


def _log_event(action: str, card_id: int | None, detail: str | None):
    db = SessionLocal()
    try:
        db.add(CardLog(action=action, card_id=card_id, detail=detail))
        db.commit()
    finally:
        db.close()


def log_card_event(action: str, card_id: int | None, detail: str | None = None):
    """对外暴露的卡密日志写入入口"""
    try:
        _log_event(action, card_id, detail)
    except Exception:
        pass
