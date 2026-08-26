"""配置管理模块"""

import json
import os
import re
from pathlib import Path

# ── 自动加载 .env 文件 ──
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent.parent / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据目录：优先使用 DATA_DIR 环境变量（Docker 挂载卷），否则用项目根目录
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))

# 用户配置文件路径
USER_CONFIG_FILE = DATA_DIR / "config.json"

# 默认值
_DEFAULT_CONFIG = {
    "download_dir": "",  # 空字符串表示使用默认 downloads 目录
    "quality": 0,        # 0=标准, 1=高, 2=超高
    "format": "mp3",     # mp3 或 m4a
    # 失败自动重试（任务结束后仍有失败集时，按分钟间隔自动重试）
    "auto_retry_enabled": True,        # 是否启用自动重试
    "auto_retry_interval_minutes": 5,  # 两轮自动重试之间的间隔（分钟）
    "auto_retry_max_rounds": 10,       # 最大自动重试轮数（防止死循环）
}

# 下载保存路径（环境变量 DOWNLOAD_DIR 优先，兼容旧 AUDIO_DIR 变量）
_DEFAULT_DL = str(BASE_DIR / "downloads")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", os.environ.get("AUDIO_DIR", _DEFAULT_DL)))

# 夸克挂载目录（容器内路径）。空字符串 = 未配置，同步功能不可用。
# Docker 示例：宿主机 /vol02/.../yousheng → 容器 /app/quark
_QUARK_RAW = (os.environ.get("QUARK_SYNC_DIR") or "").strip()
QUARK_SYNC_DIR = Path(_QUARK_RAW) if _QUARK_RAW else None

# Cookie 保存路径（跟随 DATA_DIR）
COOKIE_FILE = DATA_DIR / "cookie.txt"

# 默认音质: 0=标准, 1=高, 2=超高
DEFAULT_QUALITY = 0

# 默认保存格式
DEFAULT_FORMAT = "mp3"

# 服务配置
HOST = "0.0.0.0"
PORT = 6500

# 轮询配置
POLL_INTERVAL = 2      # 扫码轮询间隔(秒)
POLL_TIMEOUT = 180     # 扫码超时(秒)


def load_user_config() -> dict:
    """加载用户配置文件，不存在则返回默认配置"""
    if USER_CONFIG_FILE.exists():
        try:
            with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # 合并默认值，确保新增字段有默认值
            cfg = dict(_DEFAULT_CONFIG)
            cfg.update(saved)
            return cfg
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_user_config(cfg: dict) -> dict:
    """保存用户配置到文件，返回完整配置"""
    # 只保存用户可配置的字段
    to_save = {}
    for key in _DEFAULT_CONFIG:
        if key in cfg:
            to_save[key] = cfg[key]
    with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    # 立即应用配置
    apply_config(to_save)
    return to_save


def apply_config(cfg: dict):
    """将配置应用到运行时变量"""
    global DOWNLOAD_DIR, DEFAULT_QUALITY, DEFAULT_FORMAT

    # 环境变量优先，避免 config.json 写入宿主机路径
    if "DOWNLOAD_DIR" not in os.environ and cfg.get("download_dir"):
        DOWNLOAD_DIR = Path(cfg["download_dir"])
    # else: 保留环境变量或默认值，不覆盖

    if cfg.get("quality") is not None:
        DEFAULT_QUALITY = int(cfg["quality"])

    if cfg.get("format"):
        DEFAULT_FORMAT = cfg["format"]


def ensure_dirs():
    """确保必要目录存在"""
    # 先加载并应用用户配置
    cfg = load_user_config()
    apply_config(cfg)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _load_config_from_db() -> dict:
    """从 api_config 表读取全部键值（懒加载，避免与 db 顶层循环导入）

    数据库连接失败 / 表不存在时返回空 dict，调用方回退到默认值。
    """
    try:
        from db.session import SessionLocal
        from db.models import ApiConfig
    except Exception:
        return {}
    try:
        db = SessionLocal()
        try:
            rows = db.query(ApiConfig).all()
            return {r.cfg_key: r.cfg_value for r in rows}
        finally:
            db.close()
    except Exception:
        return {}


def get_auto_retry_config() -> dict:
    """获取失败自动重试配置（批量任务结束后按分钟间隔自动重试失败集）

    优先读 api_config 表，缺失则回退到默认值。
    """
    db_cfg = _load_config_from_db()

    def _int(key, default):
        try:
            return int(db_cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": db_cfg.get("auto_retry_enabled", "1") not in ("0", "false", "False", None, ""),
        "interval_minutes": max(1, _int("auto_retry_interval_minutes", 5)),
        "max_rounds": max(1, _int("auto_retry_max_rounds", 10)),
    }


# ════════════════════════════════════════
#  管理后台地址（/api/{admin_path}）
# ════════════════════════════════════════

DEFAULT_ADMIN_PATH = "admin"

# 合法性格式：小写字母开头，仅小写字母/数字/中划线/下划线，2~32 位
_ADMIN_PATH_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# 保留前缀：与业务 API 路由、静态资源、FastAPI 内置文档冲突的段不允许用作后台地址
_ADMIN_PATH_RESERVED = {
    "api", "auth", "search", "download", "files", "log", "interfaces", "intf",
    "extension", "accounts", "quark", "docs", "redoc", "openapi.json", "static", "assets",
    "favicon.ico",
}


def validate_admin_path(path: str) -> str | None:
    """校验后台地址是否合法。合法返回规范化后的字符串，非法返回 None。"""
    p = (path or "").strip().strip("/").lower()
    if not _ADMIN_PATH_RE.match(p):
        return None
    if p in _ADMIN_PATH_RESERVED:
        return None
    return p


def get_admin_path() -> str:
    """读取当前生效的管理后台地址段（api_config.admin_path，默认 admin）。

    启动时由 app.py 读取一次并决定路由挂载；运行期修改需重启后生效。
    读取失败/值非法时安全回退到默认值。
    """
    raw = _load_config_from_db().get("admin_path")
    if raw:
        p = validate_admin_path(raw)
        if p:
            return p
    return DEFAULT_ADMIN_PATH


def get_admin_lan_only() -> bool:
    """是否启用「管理后台仅允许局域网访问」限制（默认开启）。

    读取 api_config.admin_lan_only：缺失或 "1"/"true" → 开启（仅局域网）；
    "0"/"false" → 关闭（公网也可访问管理后台与文档页）。
    中间件 admin_lan_only 每次请求实时读取，开关即时生效，无需重启。
    """
    db_cfg = _load_config_from_db()
    v = db_cfg.get("admin_lan_only", "1")
    if v is None:
        return True
    return str(v).lower() not in ("0", "false", "")
