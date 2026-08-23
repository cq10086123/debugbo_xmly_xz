"""SQLAlchemy 数据模型（重构后的唯一数据源）

表清单：
- cards            卡密表
- sessions         卡密登录会话表
- admin_tokens     管理员会话 token 表
- admins           管理员账号表
- api_config       接口与密钥配置表（替代 config.json）
- download_tasks   下载任务表（替代 batch_tasks.json / thirdparty_tasks.json）
- download_records 下载记录表（每集一条）
- ximalaya_accounts 喜马拉雅账号表（替代 accounts.json）
- backend_xm_accounts 后端喜马拉雅供体账号池（仅注入逻辑读取，与下载链路隔离）
- card_logs        卡密操作日志（可选）
- announcements    公告表（管理端发布，前端/插件推送）
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Card(Base):
    __tablename__ = "cards"

    id = mapped_column(Integer, primary_key=True)
    code = mapped_column(String(64), unique=True, nullable=False, index=True)
    status = mapped_column(String(16), default="active", nullable=False)  # active/used/disabled/expired
    expiry_type = mapped_column(String(8), default="fixed", nullable=False)  # fixed/days
    expires_at = mapped_column(DateTime, nullable=True)
    valid_days = mapped_column(Integer, nullable=True)
    activated_at = mapped_column(DateTime, nullable=True)
    note = mapped_column(Text, nullable=True)
    # 绑定的接口名列表（JSON 数组字符串，如 '["official","tingyou8"]'）
    # NULL 或空数组 = 不限制（可用全部已启用接口）；绑定后仅可使用列表内接口
    bound_interfaces = mapped_column(Text, nullable=True)
    # 下载模式权限：server=仅服务器下载 | local=仅本地下载 | both=都允许（默认）
    # NULL 与 'both' 等价，保证历史卡与未设置卡向后兼容（两种都可用）
    download_mode = mapped_column(String(16), default="both", nullable=True)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_login_at = mapped_column(DateTime, nullable=True)

    sessions = relationship("Session", back_populates="card", cascade="all, delete-orphan")
    tasks = relationship("DownloadTask", back_populates="card", cascade="all, delete-orphan")
    records = relationship("DownloadRecord", back_populates="card", cascade="all, delete-orphan")
    accounts = relationship("XimalayaAccount", back_populates="card", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id = mapped_column(Integer, primary_key=True)
    token = mapped_column(String(64), unique=True, nullable=False, index=True)
    card_id = mapped_column(Integer, ForeignKey("cards.id"), nullable=False, index=True)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_active_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    is_active = mapped_column(Boolean, default=True, nullable=False)

    card = relationship("Card", back_populates="sessions")


class AdminToken(Base):
    """管理员登录后发放的 token（与业务 token 完全分离）"""

    __tablename__ = "admin_tokens"

    id = mapped_column(Integer, primary_key=True)
    token = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)


class Admin(Base):
    __tablename__ = "admins"

    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(128), nullable=False)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)


class ApiConfig(Base):
    """全局任务策略配置（如失败自动重试）。接口密钥已迁移至 interfaces 表，不再存放于此。"""

    __tablename__ = "api_config"

    id = mapped_column(Integer, primary_key=True)
    cfg_key = mapped_column(String(64), unique=True, nullable=False, index=True)
    cfg_value = mapped_column(Text, nullable=True)
    category = mapped_column(String(32), default="settings", nullable=False)
    updated_at = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class Interface(Base):
    """接口定义表 — 可配置/可增删的音源接口

    type 取值：
    - official  内置官方接口（Selenium XimalayaDownloader，需 VIP cookie）
    - script    Python 脚本接口（直接编写 search/get_chapters/get_audio_url）

    config（Text，JSON）：
    - official 可存放覆盖项（如官方 base_url），可选
    - script 类型存放 scripts.search/chapters/audio 源码与 script_timeout
    """

    __tablename__ = "interfaces"

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name = mapped_column(String(128), nullable=False)
    type = mapped_column(String(32), default="script", nullable=False)  # official|script
    enabled = mapped_column(Boolean, default=True, nullable=False)
    builtin = mapped_column(Boolean, default=False, nullable=False)
    priority = mapped_column(Integer, default=0, nullable=False)
    config = mapped_column(Text, nullable=True)  # JSON 文本
    description = mapped_column(Text, nullable=True)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id = mapped_column(Integer, primary_key=True)
    task_id = mapped_column(String(16), unique=True, nullable=False, index=True)
    card_id = mapped_column(Integer, ForeignKey("cards.id"), nullable=False, index=True)
    engine = mapped_column(String(16), default="official", nullable=False)  # official/thirdparty
    album_id = mapped_column(Text, nullable=True)
    album_title = mapped_column(Text, nullable=True)
    start_episode = mapped_column(Integer, default=1, nullable=False)
    end_episode = mapped_column(Integer, nullable=True)
    fmt = mapped_column(String(8), default="mp3", nullable=False)
    quality = mapped_column(Integer, default=0, nullable=False)
    concurrency = mapped_column(Integer, default=1, nullable=False)
    status = mapped_column(String(16), default="running", nullable=False)
    total = mapped_column(Integer, default=0, nullable=False)
    current = mapped_column(Integer, default=0, nullable=False)
    completed = mapped_column(Integer, default=0, nullable=False)
    skipped_count = mapped_column(Integer, default=0, nullable=False)
    current_title = mapped_column(Text, nullable=True)
    failed_list = mapped_column(Text, default="[]", nullable=False)  # JSON
    completed_files = mapped_column(Text, default="[]", nullable=False)  # JSON
    error = mapped_column(Text, nullable=True)
    retry_episodes = mapped_column(Text, nullable=True)  # JSON，重试子任务专用
    parent_task_id = mapped_column(String(16), nullable=True, index=True)
    retry_started = mapped_column(Boolean, default=False, nullable=False)
    auto_retry_round = mapped_column(Integer, default=0, nullable=False)
    auto_retry_next_at = mapped_column(DateTime, nullable=True)
    cancelled = mapped_column(Boolean, default=False, nullable=False)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at = mapped_column(DateTime, nullable=True)

    card = relationship("Card", back_populates="tasks")


class DownloadRecord(Base):
    __tablename__ = "download_records"

    id = mapped_column(Integer, primary_key=True)
    card_id = mapped_column(Integer, ForeignKey("cards.id"), nullable=False, index=True)
    task_id = mapped_column(String(16), nullable=True, index=True)
    album_id = mapped_column(Text, nullable=True)
    album_title = mapped_column(Text, nullable=True)
    track_id = mapped_column(Text, nullable=True)
    title = mapped_column(Text, nullable=True)
    episode_num = mapped_column(Integer, nullable=True)
    file_path = mapped_column(Text, nullable=True)
    file_size = mapped_column(Integer, default=0, nullable=False)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)

    card = relationship("Card", back_populates="records")


class XimalayaAccount(Base):
    """喜马拉雅 VIP 多账号（替代 accounts.json）

    按 card_id 隔离：每个卡密用户自行扫码登录，账号只属于该卡密。
    卡密过期清零时，本表 card_id 匹配的行一并删除。

    injected / backend_src_id：标记该账号是否由「后端供体池」注入（非用户自扫）。
    仅供识别/重注/撤销使用，不影响下载逻辑（下载器只认 card_id 维度）。
    """

    __tablename__ = "ximalaya_accounts"
    __table_args__ = (
        UniqueConstraint("card_id", "uid", name="uq_card_uid"),
    )

    id = mapped_column(Integer, primary_key=True)
    card_id = mapped_column(Integer, ForeignKey("cards.id"), nullable=True, index=True)
    acc_id = mapped_column(String(64), nullable=True, index=True)  # 兼容旧 acc_{uid}
    nickname = mapped_column(Text, nullable=True)
    uid = mapped_column(Text, nullable=True)
    mobile = mapped_column(Text, nullable=True)
    cookie_str = mapped_column(Text, nullable=True)
    is_vip = mapped_column(Boolean, default=False, nullable=False)
    rate_limited_until = mapped_column(DateTime, nullable=True)
    added_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    injected = mapped_column(Boolean, default=False, nullable=False)  # 由后端供体池注入
    backend_src_id = mapped_column(Integer, nullable=True)  # 来源 BackendXmAccount.id

    card = relationship("Card", back_populates="accounts")


class BackendXmAccount(Base):
    """后端喜马拉雅供体账号池（仅供「注入逻辑」读取，绝不被下载器直接引用）

    管理员在后台扫码登录的多个喜马拉雅账号暂存于此；注入时把 cookie 复制成
    某卡密名下的 XimalayaAccount 行。本表与下载链路完全隔离，仅被注入逻辑使用。
    """

    __tablename__ = "backend_xm_accounts"

    id = mapped_column(Integer, primary_key=True)
    uid = mapped_column(Text, nullable=True, index=True)
    nickname = mapped_column(Text, nullable=True)
    mobile = mapped_column(Text, nullable=True)
    is_vip = mapped_column(Boolean, default=False, nullable=False)
    cookie_str = mapped_column(Text, nullable=True)
    added_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class CardLog(Base):
    __tablename__ = "card_logs"

    id = mapped_column(Integer, primary_key=True)
    action = mapped_column(String(32), nullable=False)
    card_id = mapped_column(Integer, nullable=True, index=True)
    detail = mapped_column(Text, nullable=True)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)


class LocalTask(Base):
    """浏览器插件「本地下载」任务表

    服务器只下发专辑/章节元数据；音频获取接口（track API）由用户浏览器插件执行，
    请求走用户公网 IP、字节落用户本地磁盘，不占服务器空间。

    tracks 字段存放 JSON：[{track_id, episode_num, title, fmt}, ...]
    """

    __tablename__ = "local_tasks"

    id = mapped_column(Integer, primary_key=True)
    task_id = mapped_column(String(16), unique=True, nullable=False, index=True)
    card_id = mapped_column(Integer, ForeignKey("cards.id"), nullable=False, index=True)
    source = mapped_column(String(32), nullable=False)          # official | 第三方接口 name
    album_id = mapped_column(Text, nullable=True)
    album_title = mapped_column(Text, nullable=True)
    quality = mapped_column(Integer, default=0, nullable=False)
    fmt = mapped_column(String(8), default="mp3", nullable=False)
    tracks = mapped_column(Text, default="[]", nullable=False)   # JSON
    status = mapped_column(String(16), default="pending", nullable=False)  # pending|done
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)


class Announcement(Base):
    """公告表：管理端发布，业务前端与浏览器插件推送展示

    active 仅一条启用公告时业务端返回该条；多条启用按 id 倒序取最新。
    已读状态不落库：前端 localStorage / 插件 chrome.storage.local 记录已读 id。
    """

    __tablename__ = "announcements"

    id = mapped_column(Integer, primary_key=True)
    title = mapped_column(String(200), nullable=False)
    content = mapped_column(Text, nullable=False)
    active = mapped_column(Boolean, default=True, nullable=False)
    created_at = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
