"""浏览器插件「本地下载」接口 — /api/extension

设计目标：把"音频获取接口"从服务器移到用户浏览器插件执行，达到：
- 音频 track API 请求走用户公网 IP（而非服务器 IP），缓解服务器 IP 维度风控；
- 音频字节直接落用户本地磁盘，不占服务器空间；
- 服务器只负责：卡密鉴权、专辑/章节元数据下发、任务派发与 ack。

插件侧持有各音源的"音频解析器"（与后端 parse 契约一致，只是用 JS 实现），
给定 chapter 元数据后自行向音源 API 请求直链并下载。

全局下载槽（0.7.0）：
- 同一卡密全局只允许一个下载任务（本地插件 / 服务器下载共用，见 core/download_slot.py）。
- 插件流程：claim 抢槽 → 下载 → heartbeat 续约 → complete/cancel 释放；
  心跳被拒（409）或租约过期则必须停止本地下载（同一任务可能已被其他设备接管）。
- 官方源 cookie 领取强制携带 task_id + claim_id 并通过 5 项校验，
  防止同卡另一个插件趁槽被占用时偷拿 cookie 并行下载。
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import download_slot
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
# claim 串行锁：claim 是「选任务 + 抢槽 + 改状态」的多步操作，进程内串行化
# （下载槽本身的 DB 条件更新已是原子的，此锁只为保证多步整体一致）
_CLAIM_LOCK = threading.Lock()


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


def _slot_busy_response(holder: dict | None) -> JSONResponse:
    """统一 409：当前卡密已有下载任务进行中（holder 告诉调用方被谁占用）"""
    return download_slot.busy_response(holder)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task_to_dict(t: LocalTask, with_tracks: bool = True) -> dict:
    d = {
        "task_id": t.task_id,
        "source": t.source,
        "album_id": t.album_id,
        "album_title": t.album_title,
        "quality": t.quality,
        "fmt": t.fmt,
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "progress": json.loads(t.progress) if t.progress else None,
        "failed_list": json.loads(t.failed_list) if t.failed_list else [],
        "error": t.error or "",
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
    }
    if with_tracks:
        d["tracks"] = json.loads(t.tracks or "[]")
    return d


# ════════════════════════════════════════
#  任务创建（网页端推送）
# ════════════════════════════════════════
@router.post("/task")
async def create_local_task(req: CreateTaskRequest, auth: dict = Depends(get_current_card)):
    """前端选择「本地下载」时调用：把章节元数据推送给该卡密的浏览器插件。

    服务器不解析音频直链、不下载字节；插件 claim 该任务后用自身 IP 解析+下载。
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

            # 幂等去重增强：
            # 原逻辑仅检查 pending，ack 后任务变为 done，短时间内重复推送会产生新任务，导致插件侧重复下载 (1) 副本
            # 新逻辑：同卡密+同接口+同专辑 且曲目集完全一致，且状态非 cancelled → 视为重复
            new_ids = sorted(str(x.track_id) for x in tracks)
            candidates = (
                db.query(LocalTask)
                .filter_by(card_id=card_id, source=req.source, album_id=str(req.album_id))
                .filter(LocalTask.status != "cancelled")
                .order_by(LocalTask.created_at.desc())
                .limit(20)
                .all()
            )
            for t in candidates:
                try:
                    old_ids = sorted(str(x.get("track_id")) for x in json.loads(t.tracks or "[]"))
                except Exception:  # noqa: BLE001
                    continue
                if old_ids == new_ids:
                    if t.status in ("pending", "running"):
                        return {
                            "success": True,
                            "task_id": t.task_id,
                            "count": len(new_ids),
                            "duplicated": True,
                            "message": ("相同任务正在本地下载中，无需重复推送"
                                        if t.status == "running"
                                        else "相同任务已在插件队列中，无需重复推送"),
                        }
                    # done 任务在 30 分钟内也去重，避免快速双击产生重复
                    if t.status == "done" and t.created_at:
                        try:
                            now = datetime.now(timezone.utc)
                            created = t.created_at
                            if created.tzinfo is None:
                                created = created.replace(tzinfo=timezone.utc)
                            age = (now - created).total_seconds()
                            if age < 30 * 60:
                                return {
                                    "success": True,
                                    "task_id": t.task_id,
                                    "count": len(new_ids),
                                    "duplicated": True,
                                    "message": "相同任务在 30 分钟内已推送过，插件可能仍在下载中，无需重复推送",
                                }
                        except Exception:
                            pass

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
    """插件轮询：拉取当前卡密的待下载（pending）与下载中（running）任务。

    pending：可被 claim；running：已被某设备 claim（本设备 claim 的用于续传展示，
    其他设备 claim 的用于显示「其他设备下载中」并避免本地重复下载）。
    done/cancelled 不再下发（终态，由插件本地保留展示）。
    """
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        rows = (
            db.query(LocalTask)
            .filter(LocalTask.card_id == card_id, LocalTask.status.in_(["pending", "running"]))
            .order_by(LocalTask.created_at)
            .all()
        )
        out = [_task_to_dict(r) for r in rows]
        return {"success": True, "tasks": out}
    finally:
        db.close()


# ════════════════════════════════════════
#  下载槽生命周期：claim → heartbeat → complete/cancel
# ════════════════════════════════════════
def _do_claim(db, card_id: int, task_id: str | None) -> tuple[dict | None, JSONResponse | None]:
    """执行一次 claim（调用方持有 _CLAIM_LOCK）。

    成功 → (result_dict, None)；失败 → (None, error_response)。
    可 claim 的任务：pending；或 done 且仍有失败集（重试失败集 = 重新 claim）。
    """
    q = db.query(LocalTask).filter_by(card_id=card_id)
    if task_id:
        t = q.filter_by(task_id=task_id).first()
        if not t:
            return None, JSONResponse(status_code=404, content={
                "success": False, "error": "任务不存在"})
    else:
        t = q.filter_by(status="pending").order_by(LocalTask.created_at).first()
        if not t:
            return None, JSONResponse(status_code=200, content={
                "success": False, "error": "没有待下载的任务"})

    can_retry_failed = (
        t.status == "done"
        and bool(t.failed_list)
        and len([x for x in json.loads(t.failed_list) if x]) > 0
    )
    if t.status not in ("pending",) and not can_retry_failed:
        return None, JSONResponse(status_code=409, content={
            "success": False,
            "error": f"任务当前状态为 {t.status}，不可 claim",
        })

    claim_id = uuid.uuid4().hex[:16]
    acquired, holder = download_slot.acquire(
        card_id, "local", t.task_id, claim_id=claim_id,
        ttl_seconds=download_slot.LOCAL_TTL_SECONDS,
        source=t.source, album_id=t.album_id, album_title=t.album_title,
    )
    if not acquired:
        return None, _slot_busy_response(holder)

    now = _utcnow_naive()
    t.status = "running"
    t.claim_id = claim_id
    t.claimed_at = now
    t.heartbeat_at = now
    t.lease_until = now + timedelta(seconds=download_slot.LOCAL_TTL_SECONDS)
    t.finished_at = None
    if can_retry_failed:
        # 重试失败集：清空上次的失败信息，插件侧已把失败集重置为 pending
        t.failed_list = None
        t.error = None
    db.commit()
    result = _task_to_dict(t)
    result["claim_id"] = claim_id
    result["lease_seconds"] = download_slot.LOCAL_TTL_SECONDS
    result["heartbeat_seconds"] = download_slot.LOCAL_HEARTBEAT_SECONDS
    logger.info(f"下载槽 claim 成功: card={card_id} task={t.task_id} claim={claim_id} source={t.source}")
    return result, None


@router.post("/tasks/claim")
async def claim_task(auth: dict = Depends(get_current_card)):
    """通用 claim：服务端选最早的一个 pending 任务，抢下载槽并返回任务全量信息。

    同一卡密已有任一下载（本地或服务器）进行中 → 409。
    """
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        with _CLAIM_LOCK:
            result, err = _do_claim(db, card_id, None)
        if err:
            return err
        return {"success": True, "task": result}
    finally:
        db.close()


@router.post("/tasks/{task_id}/claim")
async def claim_task_by_id(task_id: str, auth: dict = Depends(get_current_card)):
    """指定任务 claim（插件优先下载某本 / 重试失败集）。"""
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        with _CLAIM_LOCK:
            result, err = _do_claim(db, card_id, task_id)
        if err:
            return err
        return {"success": True, "task": result}
    finally:
        db.close()


class HeartbeatRequest(BaseModel):
    claim_id: str


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat_task(task_id: str, req: HeartbeatRequest,
                         auth: dict = Depends(get_current_card)):
    """插件心跳续约。claim 失效（租约过期/被接管/已终态/claim_id 不匹配）→ 409，
    插件收到后必须停止该任务的本地下载。"""
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t or t.status != "running" or not t.claim_id or t.claim_id != req.claim_id:
            return JSONResponse(status_code=409, content={
                "success": False,
                "error": "下载租约已失效或不再有效，请停止本地下载并重新 claim",
            })
    finally:
        db.close()

    ok, lock = download_slot.heartbeat(card_id, task_id, ttl_seconds=download_slot.LOCAL_TTL_SECONDS)
    if not ok:
        return JSONResponse(status_code=409, content={
            "success": False,
            "error": "下载租约已失效或不再有效，请停止本地下载并重新 claim",
        })

    now = _utcnow_naive()
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if t is not None:
            t.heartbeat_at = now
            t.lease_until = now + timedelta(seconds=download_slot.LOCAL_TTL_SECONDS)
            db.commit()
    finally:
        db.close()

    return {
        "success": True,
        "lease_seconds": download_slot.LOCAL_TTL_SECONDS,
        "lease_until": lock["lease_until"] if lock else None,
    }


class CompleteRequest(BaseModel):
    claim_id: str
    progress: dict | None = None
    failed_list: list = Field(default_factory=list)
    error: str = ""


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str, req: CompleteRequest,
                        auth: dict = Depends(get_current_card)):
    """插件下载结束（全部完成或全部终止）：落库进度/失败集，标记 done，释放下载槽。

    注意：claim 匹配即接受（即使锁已过期但任务仍是本次 claim 的 running）——
    这样可避免插件已完成的任务被租约清扫重新排队后被另一设备重复下载。
    """
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        if t.status != "running" or not t.claim_id or t.claim_id != req.claim_id:
            return JSONResponse(status_code=409, content={
                "success": False,
                "error": "claim 已失效或任务不在下载中，请停止本地下载",
            })
        now = _utcnow_naive()
        t.status = "done"
        t.progress = json.dumps(req.progress or {}, ensure_ascii=False)
        t.failed_list = json.dumps(req.failed_list or [], ensure_ascii=False)
        t.error = req.error or None
        t.finished_at = now
        t.claim_id = None
        t.claimed_at = None
        t.heartbeat_at = None
        t.lease_until = None
        db.commit()
    finally:
        db.close()

    # 所有权校验的释放（槽已被他人抢占时为空操作，绝不误删新锁）
    download_slot.release(card_id, task_id)
    logger.info(f"本地任务完成，释放下载槽: card={card_id} task={task_id}")
    return {"success": True, "message": "下载完成，已释放下载槽"}


class CancelRequest(BaseModel):
    claim_id: str | None = None


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, req: CancelRequest,
                      auth: dict = Depends(get_current_card)):
    """取消本地任务并释放下载槽。

    - 带 claim_id：任务必须是本次 claim 的 running → cancelled + 释放槽。
    - 不带 claim_id：任务必须是 pending（尚未被 claim）→ cancelled（不涉及槽）。
    - 已终态（done/cancelled）：幂等返回成功。
    """
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        if t.status in ("done", "cancelled"):
            return {"success": True, "message": "任务已终态，无需取消"}
        if t.status == "running":
            if not req.claim_id or t.claim_id != req.claim_id:
                return JSONResponse(status_code=409, content={
                    "success": False,
                    "error": "任务正在其他 claim 下下载中，无法取消",
                })
            now = _utcnow_naive()
            t.status = "cancelled"
            t.finished_at = now
            t.claim_id = None
            t.claimed_at = None
            t.heartbeat_at = None
            t.lease_until = None
            db.commit()
            download_slot.release(card_id, task_id)
            logger.info(f"本地任务取消，释放下载槽: card={card_id} task={task_id}")
        else:  # pending
            t.status = "cancelled"
            t.finished_at = _utcnow_naive()
            db.commit()
        return {"success": True, "message": "已取消"}
    finally:
        db.close()


@router.post("/tasks/{task_id}/ack")
async def ack_local_task(task_id: str, auth: dict = Depends(get_current_card)):
    """插件下载完成后确认：标记任务为 done（不再被轮询拉取）。

    兼容旧版插件（0.6.x）保留。新流程（0.7+）以 complete 为准。
    保护：running（被某设备 claim 下载中）的任务不允许被 ack 置 done，
    防止旧插件把新插件正在下载的任务直接确认掉。
    """
    card_id = auth["card_id"]
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        if t.status == "running":
            return JSONResponse(status_code=409, content={
                "success": False,
                "error": "任务正在本地下载中，不能直接确认",
            })
        t.status = "done"
        t.finished_at = _utcnow_naive()
        db.commit()
    finally:
        db.close()
    # 兜底：若该任务仍持有下载槽（异常路径），释放之（所有权校验，空操作安全）
    download_slot.release(card_id, task_id)
    return {"success": True, "message": "已确认"}


# ════════════════════════════════════════
#  官方源 cookie / 冷却（0.7.0 起强制 claim 凭证校验）
# ════════════════════════════════════════
def _validate_active_claim(card_id: int, task_id: str | None, claim_id: str | None) -> tuple[LocalTask, None] | tuple[None, JSONResponse]:
    """校验「当前卡密持有本地下载槽，且 task/claim 匹配、任务 running、source 为 official」。

    防止同卡另一个插件趁下载槽被占用时偷拿 cookie 并行下载。
    返回 (task, None) 或 (None, error_response)。
    """
    if not task_id or not claim_id:
        return None, JSONResponse(status_code=400, content={
            "success": False,
            "error": "缺少 task_id / claim_id（必须先 claim 下载任务）",
        })
    lock = download_slot.get_lock(card_id)
    if lock is None or lock["expired"]:
        return None, JSONResponse(status_code=409, content={
            "success": False,
            "error": "当前卡密没有进行中的本地下载，无法获取官方 cookie",
        })
    if lock["holder_type"] != "local":
        return None, JSONResponse(status_code=409, content={
            "success": False,
            "error": "下载槽正被服务器下载占用，无法获取本地官方 cookie",
        })
    if lock["task_id"] != task_id:
        return None, JSONResponse(status_code=409, content={
            "success": False,
            "error": "task_id 与当前下载任务不匹配",
        })
    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
    finally:
        db.close()
    if not t or t.status != "running":
        return None, JSONResponse(status_code=409, content={
            "success": False,
            "error": "任务不在下载中（可能已完成或已被释放）",
        })
    if t.claim_id != claim_id:
        return None, JSONResponse(status_code=403, content={
            "success": False,
            "error": "claim_id 不匹配，拒绝下发（疑似未持有该任务的下载凭证）",
        })
    if t.source != "official":
        return None, JSONResponse(status_code=403, content={
            "success": False,
            "error": "该任务不是官方源，无需/不可通过此接口获取 cookie",
        })
    return t, None


class CooldownRequest(BaseModel):
    account_id: str
    task_id: str | None = None
    claim_id: str | None = None


@router.post("/xm-cookie/cooldown")
async def mark_xm_cookie_cooldown(req: CooldownRequest, auth: dict = Depends(get_current_card)):
    """插件检测到某账号被限流时，标记该账号进入冷却（24h，仅当前卡密）。

    0.7.0 起同样要求 claim 凭证校验：防止同卡另一插件在持有者下载期间
    恶意把所有账号打冷却（24h），实现"软 DDoS"。
    """
    from core.account_manager import set_rate_limited
    card_id = auth["card_id"]
    ensure_interface_allowed(auth, "official")
    ensure_download_mode_allowed(auth, "local")
    _, err = _validate_active_claim(card_id, req.task_id, req.claim_id)
    if err:
        return err
    set_rate_limited(card_id, req.account_id)
    return {"success": True}


@router.get("/xm-cookie")
async def get_xm_cookie(task_id: str | None = None, claim_id: str | None = None,
                        auth: dict = Depends(get_current_card)):
    """插件官方源下载时，返回当前登录卡密下所有可用（未冷却）账号的 cookie 列表，供多账号轮询。

    严格 per-card：只返回 auth 中当前登录卡密的账号，绝不跨卡密。
    与服务器官方下载账号选择逻辑一致：get_active_account_ids 排除冷却账号 → VIP 优先排序。
    插件拿到列表后逐账号尝试，某账号命中限流则调 /xm-cookie/cooldown 标记冷却并换下一个。

    0.7.0 起必须携带 task_id + claim_id 并通过 5 项校验
    （本地槽持有 / task 匹配 / claim 匹配 / running / source=official），
    避免另一个同卡插件趁已有下载槽时偷拿 cookie 并行下载。
    """
    from core.account_manager import list_accounts, get_active_account_ids, get_account_cookie
    card_id = auth["card_id"]
    ensure_interface_allowed(auth, "official")
    ensure_download_mode_allowed(auth, "local")
    _, err = _validate_active_claim(card_id, task_id, claim_id)
    if err:
        return err
    all_accounts = list_accounts(card_id)
    active_ids = get_active_account_ids(card_id)
    # VIP 优先（与 api/download.py 顺序一致）
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
