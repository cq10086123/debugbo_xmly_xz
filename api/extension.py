"""浏览器插件「本地下载」接口 — /api/extension

设计目标：把"音频获取接口"从服务器移到用户浏览器插件执行，达到：
- 音频 track API 请求走用户公网 IP（而非服务器 IP），缓解服务器 IP 维度风控；
- 音频字节直接落用户本地磁盘，不占服务器空间；
- 服务器只负责：卡密鉴权、专辑/章节元数据下发、任务派发与 ack。

插件侧持有各音源的"音频解析器"（与后端 parse 契约一致，只是用 JS 实现），
给定 chapter 元数据后自行向音源 API 请求直链并下载。
"""

import json
import threading
import uuid
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db.session import SessionLocal
from db.models import LocalTask
from api.deps import get_current_card, ensure_interface_allowed, ensure_download_mode_allowed

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/extension", tags=["浏览器插件本地下载"])

# 防滥用上限
_MAX_PENDING_PER_CARD = 50
_MAX_TRACKS_PER_TASK = 5000

# 任务创建串行锁：幂等去重是「先查后插」，无唯一约束兜底，并发推送同一任务会双写
_TASK_CREATE_LOCK = threading.Lock()


class TrackItem(BaseModel):
    track_id: str
    episode_num: int | None = None
    title: str = ""
    fmt: str = "mp3"


class CreateTaskRequest(BaseModel):
    source: str                                   # official | 第三方接口 name
    album_id: str
    album_title: str = ""
    quality: int = 0
    fmt: str = "mp3"
    tracks: list[TrackItem] = Field(default_factory=list)


@router.post("/task")
async def create_local_task(req: CreateTaskRequest, auth: dict = Depends(get_current_card)):
    """前端选择「本地下载」时调用：把章节元数据推送给该卡密的浏览器插件。

    服务器不解析音频直链、不下载字节；插件拿到任务后用自身 IP 解析+下载。
    """
    card_id = auth["card_id"]
    # 卡密绑定校验：source 为该任务使用的接口名（official 或第三方接口 name）
    ensure_interface_allowed(auth, req.source)
    # 下载模式校验：本地下载入口，仅允许 download_mode 含 local 的卡密
    ensure_download_mode_allowed(auth, "local")
    tracks = req.tracks[:_MAX_TRACKS_PER_TASK]
    if not tracks:
        raise HTTPException(status_code=400, detail="没有可下载的章节")

    db = SessionLocal()
    try:
        with _TASK_CREATE_LOCK:  # 去重检查与插入必须原子，防并发双写
            pending = db.query(LocalTask).filter_by(card_id=card_id, status="pending").count()
            if pending >= _MAX_PENDING_PER_CARD:
                raise HTTPException(status_code=429, detail="待下载任务过多，请先在插件中完成已有任务")

            # 幂等去重：同卡密+同接口+同专辑 且有待处理任务的曲目集完全一致 → 视为重复推送，
            # 直接返回原任务，避免插件侧同集重复下载产生 "(1)" 副本文件
            new_ids = sorted(x.track_id for x in tracks)
            for t in db.query(LocalTask).filter_by(
                card_id=card_id, source=req.source, album_id=str(req.album_id), status="pending"
            ):
                try:
                    old_ids = sorted(str(x.get("track_id")) for x in json.loads(t.tracks or "[]"))
                except Exception:  # noqa: BLE001
                    continue
                if old_ids == new_ids:
                    return {
                        "success": True,
                        "task_id": t.task_id,
                        "count": len(new_ids),
                        "duplicated": True,
                        "message": "相同任务已在插件队列中，无需重复推送",
                    }

            task_id = uuid.uuid4().hex[:12]
            t = LocalTask(
                task_id=task_id,
                card_id=card_id,
                source=req.source,
                album_id=str(req.album_id),
                album_title=req.album_title,
                quality=req.quality,
                fmt=req.fmt,
                tracks=json.dumps([x.model_dump() for x in tracks], ensure_ascii=False),
                status="pending",
            )
            db.add(t)
            db.commit()
        return {
            "success": True,
            "task_id": task_id,
            "count": len(tracks),
            "message": "已推送到本地浏览器插件，请打开插件开始下载（走你的 IP，不占服务器空间）",
        }
    finally:
        db.close()


@router.get("/tasks")
async def list_local_tasks(auth: dict = Depends(get_current_card)):
    """插件轮询：拉取当前卡密所有待下载任务（含章节元数据）。"""
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        rows = (
            db.query(LocalTask)
            .filter_by(card_id=card_id, status="pending")
            .order_by(LocalTask.created_at)
            .all()
        )
        out = []
        for r in rows:
            out.append({
                "task_id": r.task_id,
                "source": r.source,
                "album_id": r.album_id,
                "album_title": r.album_title,
                "quality": r.quality,
                "fmt": r.fmt,
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "tracks": json.loads(r.tracks or "[]"),
            })
        return {"success": True, "tasks": out}
    finally:
        db.close()


@router.post("/tasks/{task_id}/ack")
async def ack_local_task(task_id: str, auth: dict = Depends(get_current_card)):
    """插件下载完成后确认：标记任务为 done（不再被轮询拉取）。"""
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        t.status = "done"
        db.commit()
        return {"success": True, "message": "已确认"}
    finally:
        db.close()


class CooldownRequest(BaseModel):
    account_id: str


@router.post("/xm-cookie/cooldown")
async def mark_xm_cookie_cooldown(req: CooldownRequest, auth: dict = Depends(get_current_card)):
    """插件检测到某账号被限流时，标记该账号进入冷却（24h，仅当前卡密）。

    与服务器官方下载 _switch_downloader 中的 set_rate_limited 行为一致；
    标记后下次 /xm-cookie 拉取将自动排除该账号，实现插件侧多账号轮询闭环。
    """
    from core.account_manager import set_rate_limited
    card_id = auth["card_id"]
    ensure_interface_allowed(auth, "official")
    ensure_download_mode_allowed(auth, "local")
    set_rate_limited(card_id, req.account_id)
    return {"success": True}


@router.get("/xm-cookie")
async def get_xm_cookie(auth: dict = Depends(get_current_card)):
    """插件官方源下载时，返回当前登录卡密下所有可用（未冷却）账号的 cookie 列表，供多账号轮询。

    严格 per-card：只返回 auth 中当前登录卡密的账号，绝不跨卡密。
    与服务器官方下载账号选择逻辑一致：get_active_account_ids 排除冷却账号 → VIP 优先排序。
    插件拿到列表后逐账号尝试，某账号命中限流则调 /xm-cookie/cooldown 标记冷却并换下一个。
    """
    from core.account_manager import list_accounts, get_active_account_ids, get_account_cookie
    card_id = auth["card_id"]
    ensure_interface_allowed(auth, "official")
    ensure_download_mode_allowed(auth, "local")
    all_accounts = list_accounts(card_id)
    active_ids = get_active_account_ids(card_id)
    # VIP 优先（与 api/download.py:148-161 顺序一致）
    ordered = sorted(
        active_ids,
        key=lambda aid: (0 if any(a["id"] == aid and a.get("is_vip") for a in all_accounts) else 1,),
    )
    accounts = []
    for aid in ordered:
        c = get_account_cookie(card_id, aid)
        if c:
            accounts.append({"id": aid, "cookie": c})
    return {"success": True, "accounts": accounts}

