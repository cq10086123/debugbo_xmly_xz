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

import hashlib
import hashlib
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


def _session_hash(raw_token: str | None) -> str | None:
    """claim 归属用的会话指纹（sha256 前 16 位，绝不落库原 token）。

    只用来回答「这条 running 任务是不是你这个会话 claim 的」。
    历史实现让插件靠「不是我的 claimState ⇒ 一定是别的设备」瞎猜，
    SW 重启 / 心跳 5xx / DB 抖动都会误报「其他设备正在下载此任务」并冻结自己的任务。
    """
    if not raw_token:
        return None
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:16]


def _cfg(key: str, default: str) -> str:
    """读 api_config（复用 device_binding 的 5s 缓存，与 api/risk_control.py 同一套做法）。"""
    try:
        from core.device_binding import _load_cfg_cached
        return _load_cfg_cached().get(key, default) or default
    except Exception:
        return default


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def _renew_relaxed() -> bool:
    """slot_renew_relaxed：租约已过期但仍是本次持有者时，允许原地续租（默认开）。

    关掉 = 回到旧行为（心跳晚一拍就 409，插件必须中断下载并重新排队 claim）。
    """
    return _truthy(_cfg("slot_renew_relaxed", "1"))


def _infra_error_mode() -> str:
    """heartbeat_infra_error_mode：下载槽基础设施异常时，心跳怎么回。

    - "success_degraded"（默认）：200 + degraded:true + 不做任何写入。
      因为老插件（≤0.7.1）把任何非 200 都当「claim 失效」并立刻取消在飞下载 —— 直接给它
      503 只是把「误判为别人抢」换成「误判为服务器错误」，症状一模一样。
      安全性：所有权判定仍依赖 local_tasks.claim_id 未变（见 heartbeat_task 的守卫），
      真被别的会话 claim 过时下一拍心跳即被拒 ⇒ 最坏重叠窗口 = 一个心跳周期。
    - "http_503"：插件升级到能区分 soft-lost 之后（见修复方案 F1.4）再切换。
    """
    v = (_cfg("heartbeat_infra_error_mode", "success_degraded") or "").strip().lower()
    return v if v in ("success_degraded", "http_503") else "success_degraded"


def _beat_seconds() -> int:
    """下发给插件的建议心跳间隔（0.7.2 起真的被消费）。

    不变式：**一个租约周期内至少 4 拍**，且单次心跳的超时上限（20s）必须远小于间隔，
    否则一次网络抖动就会吃掉整拍。老插件忽略此字段，所以这里改的只是新插件的节奏。
    """
    ttl = download_slot.LOCAL_TTL_SECONDS
    beat = max(5, min(60, ttl // 6))
    if beat * 4 > ttl:              # TTL 被配得很小（如 30s）时兜住下限，别让心跳比租约还慢
        beat = max(5, ttl // 4)
    return beat


def _api_error(status: int, code: str, error: str, *, headers: dict | None = None,
               **extra) -> JSONResponse:
    """统一失败响应：code 供客户端分支，error 给人看。

    历史失败只有「409 + 一句中文」，插件无法区分「真被别人持有」/「任务被重新排队」/
    「服务器读不到状态」，于是一律中断下载。code 就是把这三件事拆开。
    """
    return JSONResponse(status_code=status,
                        content={"success": False, "code": code, "error": error, **extra},
                        headers=headers or None)


def _release_slot(card_id: int, task_id: str) -> bool:
    """释放下载槽并**确认**结果（修 C2：旧实现忽略 release 返回值）。

    释锁失败会让该卡密白占一个完整租约周期（最长 5 分钟），期间用户点下一本一律 409
    「当前卡密已有下载任务进行中」，看起来就像「任务卡死 / 被别人占了」。
    """
    if download_slot.release(card_id, task_id):
        return True
    lock = download_slot.get_lock(card_id)
    if lock is None:
        return True                                     # 已无锁（expire_stale 清掉了等）：目标状态已达成
    if lock.get("task_id") != task_id:
        return True                                     # 槽已属新持有者：所有权校验语义要求绝不越权删除
    if download_slot.release(card_id, task_id):
        logger.warning(f"下载槽释放重试成功: card={card_id} task={task_id}")
        return True
    logger.error(f"下载槽释放失败（槽仍由本任务持有，将随租约过期自动回收）: card={card_id} task={task_id}")
    return False


# 登录时只存了 client_type / device_id / ip（没有设备名），所以「谁在下」只能给到「端 + 出口 IP」这个粒度
_OWNER_LABELS = {"extension": "插件", "web": "网页", "skill": "SKILL 客户端"}


def _owner_label(client: str | None, ip: str | None) -> str:
    """下载持有者的展示名（给用户「等谁」的实感；只用于展示，绝不参与任何判定）。"""
    who = _OWNER_LABELS.get(client or "", "其他下载会话")
    return f"{who} · {ip}" if ip else who


def _task_to_dict(t: LocalTask, with_tracks: bool = True, *,
                  viewer_hash: str | None = None,
                  labels: dict | None = None,
                  resolve_viewer: bool = True) -> dict:
    """任务快照。

    resolve_viewer=False ⇒ 不下发 mine/owner（单任务接口用，避免无谓的会话表查询）。
    新增字段只增不改（老插件忽略未知字段，天然向后兼容）：
    - claimed：服务器侧该任务当前确有 claim 持有者
    - mine   ：该持有者是不是**当前请求的这个会话**。
               存量数据 claim_session 为 NULL ⇒ 一律 True（fail-open）：否则升级那一刻，
               正在下载的设备会立刻把自己的书判成「别人在下」并冻结，凭空制造新故障。
    - owner  ：持有者展示信息（端类型 / 出口 IP / 心跳多久前），给用户「等谁」的实感。
    """
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

    claimed = t.status == "running" and bool(t.claim_id)
    d["claimed"] = claimed
    if not resolve_viewer:
        return d
    if not claimed:
        d["mine"] = None
        d["owner"] = None
        return d
    # 存量行没有会话指纹 ⇒ mine 一律 fail-open（True），owner 索性不给：
    # 避免升级那一刻把用户自己正在下的书判成「别人在下」，也避免 UI 出现「未知持有者」这种半截信息
    d["mine"] = True if not t.claim_session else (t.claim_session == viewer_hash)
    if not t.claim_session:
        d["owner"] = None
        return d
    hb = t.heartbeat_at
    if hb is not None and hb.tzinfo is not None:
        hb = hb.replace(tzinfo=None)
    now = _utcnow_naive()
    age = int((now - hb).total_seconds()) if hb else None
    lu = download_slot.lease_of(t.lease_until)
    info = (labels or {}).get(t.claim_session or "") or {}
    d["owner"] = {
        "client": info.get("client") or "unknown",
        "ip": info.get("ip") or "",
        "label": _owner_label(info.get("client"), info.get("ip")),
        "heartbeat_age_seconds": age,
        "lease_left_seconds": int((lu - now).total_seconds()) if lu else None,
    }
    return d


def _session_labels(card_id: int) -> dict[str, dict]:
    """会话指纹 → 展示信息（端类型 / 登录 IP）。一次请求查一次，失败返回空表（只影响展示）。"""
    from db.models import Session as CardSession
    db = SessionLocal()
    try:
        out: dict[str, dict] = {}
        for sess in db.query(CardSession).filter_by(card_id=card_id).all():
            h = _session_hash(sess.token)
            if h:
                out[h] = {"client": sess.client_type or "web", "ip": sess.ip or ""}
        return out
    except Exception:
        logger.debug("读取 claim 会话展示信息失败（不影响业务）", exc_info=True)
        return {}
    finally:
        db.close()


# ════════════════════════════════════════
#  任务创建（网页端推送）
# ════════════════════════════════════════
def _track_payload(item: "TrackItem", task_fmt: str | None) -> dict:
    """章节元数据入库前的 fmt 归一。

    TrackItem.fmt 有默认值 "mp3"，于是调用方（SKILL / 直接调 API 的第三方）不写 fmt 时，
    任务级 fmt=m4a 也会给每集填成 mp3；插件取文件后缀用的是 `tr.fmt || task.fmt`
    （background.js buildFilename），结果按 m4a 解析出音频、却存成 .mp3 —— 内容与后缀不符。
    这里只在调用方**没有显式给** fmt 时继承任务级 fmt，显式给过的（网页端、SKILL 正常路径）
    一字不改，保证零回归。
    """
    data = item.model_dump()
    if "fmt" not in (item.model_fields_set or set()):
        data["fmt"] = task_fmt or "mp3"
    return data


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
    requested = len(req.tracks)
    tracks = req.tracks[:_MAX_TRACKS_PER_TASK]
    truncated = max(0, requested - len(tracks))
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
                # 去重身份 = 曲目集合 + 输出格式。历史上只看曲目，于是「同一本书换成 m4a
                # 再下一次」会被当成重复直接吞掉：用户以为在重下，插件其实拿着旧 mp3 任务（2026-09-12 修）。
                same_ids = old_ids == new_ids
                same_fmt = (t.fmt or "mp3") == (req.fmt or "mp3")
                if same_ids and same_fmt:
                    if t.status in ("pending", "running"):
                        return {
                            "success": True,
                            "task_id": t.task_id,
                            "count": len(new_ids),
                            "requested": requested,
                            "truncated": truncated,
                            "duplicated": True,
                            "quality_diff": (t.quality or 0) != (req.quality or 0),
                            "message": ("相同任务正在本地下载中，无需重复推送"
                                        if t.status == "running"
                                        else "相同任务已在插件队列中，无需重复推送")
                                   + ("；注意：换音质不改变本地文件名，已下载的集会被插件跳过，"
                                      "要重下请先在插件里删除本地文件"
                                      if (t.quality or 0) != (req.quality or 0) else ""),
                        }
                    # done 任务在 30 分钟内也去重，避免快速双击产生重复。
                    # 锚点必须优先用 finished_at：一本下了一个小时的书，用 created_at 算 age 会
                    # 刚好在"完成的那一刻"失去去重保护 ⇒ 双击/重推立刻产生重复任务（插件落
                    # 成 "书名 (1)" 副本）。
                    if t.status == "done" and (t.finished_at or t.created_at):
                        try:
                            now = datetime.now(timezone.utc)
                            anchor = t.finished_at or t.created_at
                            if anchor.tzinfo is None:
                                anchor = anchor.replace(tzinfo=timezone.utc)
                            age = (now - anchor).total_seconds()
                            if age < 30 * 60:
                                return {
                                    "success": True,
                                    "task_id": t.task_id,
                                    "count": len(new_ids),
                                    "requested": requested,
                                    "truncated": truncated,
                                    "duplicated": True,
                                    "quality_diff": (t.quality or 0) != (req.quality or 0),
                                    "message": "相同任务在 30 分钟内已完成，插件可能仍在收尾，无需重复推送",
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
                tracks=json.dumps([_track_payload(x, req.fmt) for x in tracks], ensure_ascii=False),
                status="pending",
            )
            db.add(t)
            db.commit()
        resp = {
            "success": True,
            "task_id": task_id,
            "count": len(tracks),
            "message": "已推送到本地浏览器插件，请打开插件开始下载（走你的 IP，不占服务器空间）",
        }
        if truncated:
            # 以前是 req.tracks[:5000] 一刀切：用户以为整本在下，实际最后 N 集根本没进队列
            resp["truncated"] = truncated
            resp["requested"] = requested
            resp["message"] = (f"已推送 {len(tracks)} 集（超出单任务上限 {_MAX_TRACKS_PER_TASK} 集，"
                               f"剩余 {truncated} 集未加入队列，请再推一次剩余区间）")
        return resp
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
        # 归属判定只需「本卡密下的会话」→ 一次查询建映射，避免逐条任务查会话表
        viewer_hash = _session_hash(auth.get("token"))
        labels = _session_labels(card_id)
        out = [_task_to_dict(r, viewer_hash=viewer_hash, labels=labels) for r in rows]
        return {"success": True, "tasks": out}
    finally:
        db.close()


# ════════════════════════════════════════
#  下载槽生命周期：claim → heartbeat → complete/cancel
# ════════════════════════════════════════
def _do_claim(db, card_id: int, task_id: str | None,
            sess_hash: str | None = None) -> tuple[dict | None, JSONResponse | None]:
    """执行一次 claim（调用方持有 _CLAIM_LOCK）。

    成功 → (result_dict, None)；失败 → (None, error_response)。
    可 claim 的任务：pending；或 done 且仍有失败集（重试失败集 = 重新 claim）。
    """
    q = db.query(LocalTask).filter_by(card_id=card_id)
    if task_id:
        t = q.filter_by(task_id=task_id).first()
        if not t:
            return None, _api_error(404, "task_gone", "任务不存在")
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
        # code=already_claimed ⇒ 插件据此知道「这本正被某个会话持有」，
        # 而不是笼统一句 409（历史上插件把它和「槽忙」混为一谈，一律冻结 + 谎报其他设备）
        return None, _api_error(409, "already_claimed" if t.status == "running" else "not_claimable",
                                f"任务当前状态为 {t.status}，不可 claim",
                                holder_claimed=bool(t.claim_id))

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
    t.claim_session = sess_hash      # 「谁 claim 的」：只存会话指纹，供 /tasks 回 mine 字段
    t.claimed_at = now
    t.heartbeat_at = now
    t.lease_until = now + timedelta(seconds=download_slot.LOCAL_TTL_SECONDS)
    t.finished_at = None
    if can_retry_failed:
        # 重试失败集：清空上次的失败信息，插件侧已把失败集重置为 pending
        t.failed_list = None
        t.error = None
    db.commit()
    # 自己刚 claim ⇒ 归属已知，不必再查会话表
    result = _task_to_dict(t, resolve_viewer=False)
    result["claim_id"] = claim_id
    result["lease_seconds"] = download_slot.LOCAL_TTL_SECONDS
    result["heartbeat_seconds"] = download_slot.LOCAL_HEARTBEAT_SECONDS
    # 新增下发项（老插件忽略未知字段，天然兼容）：心跳节奏由租约反推，租约调大也不怕心跳太慢
    result["beat_seconds"] = _beat_seconds()
    result["grace_seconds"] = max(60, download_slot.LOCAL_TTL_SECONDS // 2)
    result["mine"] = True
    logger.info(f"下载槽 claim 成功: card={card_id} task={t.task_id} claim={claim_id} source={t.source}")
    return result, None


@router.post("/tasks/claim")
async def claim_task(auth: dict = Depends(get_current_card)):
    """通用 claim：服务端选最早的一个 pending 任务，抢下载槽并返回任务全量信息。

    同一卡密已有任一下载（本地或服务器）进行中 → 409。
    """
    card_id = auth["card_id"]
    sess_hash = _session_hash(auth.get("token"))
    db = SessionLocal()
    try:
        with _CLAIM_LOCK:
            result, err = _do_claim(db, card_id, None, sess_hash=sess_hash)
        if err:
            return err
        return {"success": True, "task": result}
    finally:
        db.close()


@router.post("/tasks/{task_id}/claim")
async def claim_task_by_id(task_id: str, auth: dict = Depends(get_current_card)):
    """指定任务 claim（插件优先下载某本 / 重试失败集）。"""
    card_id = auth["card_id"]
    sess_hash = _session_hash(auth.get("token"))
    db = SessionLocal()
    try:
        with _CLAIM_LOCK:
            result, err = _do_claim(db, card_id, task_id, sess_hash=sess_hash)
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
    """插件心跳续约。响应语义（2026-09-12 修订，修 A1/A2/A3）：

    - 200 `success:true`  续约成功（含「租约已过期但仍是本次持有者 ⇒ 原地续租」）
    - 200 `degraded:true` 服务器读不到下载槽（DB 抖动/锁超时）——**不是**租约失效，
                          插件必须继续下载并按自己的节奏重试心跳
    - 409 `code:requeued` 任务已被租约清扫打回 pending ⇒ 插件应【立即重新 claim 同一本】，
                          而不是取消在飞下载 + 冻结一个租约周期
    - 409 `code:lost`     确实被别的持有者接管（claim_id 已被改写）⇒ 插件必须停止本地下载
    - 503 `code:busy`     仅当 heartbeat_infra_error_mode=http_503 时出现

    所有权判定的第一道（也是唯一必须的）依据是 local_tasks.claim_id 与来者凭证一致：
    任何一次别的设备的 claim 都会把它改写掉，所以「原地续租」不可能抢走他人所有权。
    下载槽只是第二道（活性/占用）视图，读不到时绝不当成「被别人占了」。
    """
    card_id = auth["card_id"]
    degraded = False

    db = SessionLocal()
    try:
        t = db.query(LocalTask).filter_by(task_id=task_id, card_id=card_id).first()
        if t is None:
            return _api_error(404, "task_gone", "任务不存在")
        if t.status != "running" or not t.claim_id or t.claim_id != req.claim_id:
            # 任务已被清扫回 pending（sweep 会同时清空 claim_id）⇒ 可自愈，不是被抢
            if t.status == "pending" and not t.claim_id:
                return _api_error(409, "requeued", "任务已重新排队，请重新 claim 本任务后继续")
            return _api_error(409, "lost", "下载租约已失效或不再有效，请停止本地下载并重新 claim")

        # ── 到这里本次 claim 仍然是本任务的有效凭证（不变式 I3 的判据）──
        # 观测用：进来时任务侧租约是否已过期（= 「心跳晚了一拍」的真实发生率），不影响判定
        renewed_after_expiry = t.lease_until is not None and t.lease_until < _utcnow_naive()
        try:
            state, lock = download_slot.renew(
                card_id, task_id, claim_id=req.claim_id, holder_type="local",
                ttl_seconds=download_slot.LOCAL_TTL_SECONDS,
                require_live=not _renew_relaxed(),   # 开关关闭 ⇒ 退回「过期即拒」的旧行为
                allow_reacquire=_renew_relaxed(),    # 锁行已被 expire_stale 删除时重新登记（同条件 SQL，不抢占）
                source=t.source, album_id=t.album_id, album_title=t.album_title,
                db=db,                                # 与下面的任务侧写入同事务（修 C3：不再两本账分开提交）
            )
        except download_slot.SlotUnavailable:
            db.rollback()
            if _infra_error_mode() == "http_503":
                return _api_error(503, "busy", "服务器繁忙，请稍后重试（下载租约状态暂时读不到）",
                                  headers={"Retry-After": "15"})
            # 降级：只承认「本轮没写成」，不改任务状态、不通知插件失败 —— 插件下一拍自然重试
            logger.warning(f"心跳期间下载槽不可用，本轮降级放行: card={card_id} task={task_id}")
            degraded = True
            state = "degraded"
            lock = None

        if state == "renewed":
            now = _utcnow_naive()
            t.heartbeat_at = now
            t.lease_until = now + timedelta(seconds=download_slot.LOCAL_TTL_SECONDS)
            db.commit()
            return {
                "success": True,
                "lease_seconds": download_slot.LOCAL_TTL_SECONDS,
                "lease_until": lock["lease_until"] if lock else None,
                "beat_seconds": _beat_seconds(),
                "grace_seconds": max(60, download_slot.LOCAL_TTL_SECONDS // 2),
                # 观测点：上线后统计「过期后原地续租」次数，用来判断是否还需要继续放宽租约
                "renewed_after_expiry": renewed_after_expiry,
                "mine": True,
            }
        if state == "lost":
            db.rollback()
            return _api_error(409, "lost", "下载租约已失效或不再有效，请停止本地下载并重新 claim",
                              holder_task_id=(lock or {}).get("task_id"))
        # degraded：租约保持原样（不写任务侧，避免与槽不一致），让插件下一拍重试
        return {
            "success": True,
            "degraded": True,
            "lease_seconds": download_slot.LOCAL_TTL_SECONDS,
            "beat_seconds": _beat_seconds(),
            "message": "服务器暂时读不到下载槽状态，本次未变更租约，请继续下载并稍后重试心跳",
        }
    finally:
        db.close()


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
            # code=already_done：服务器侧已判完成 → 插件按「已完成」收尾（走 finalizeTask 取服务器明细），
            # 不再与「claim 被接管」混为一谈（历史上两者共用同一句 409，插件只能显示那句吓人文案）
            return _api_error(409, "already_done" if t.status == "done" else "claim_invalid",
                              "claim 已失效或任务不在下载中，请停止本地下载")
        now = _utcnow_naive()
        t.status = "done"
        t.progress = json.dumps(req.progress or {}, ensure_ascii=False)
        t.failed_list = json.dumps(req.failed_list or [], ensure_ascii=False)
        t.error = req.error or None
        t.finished_at = now
        t.claim_id = None
        t.claim_session = None
        t.claimed_at = None
        t.heartbeat_at = None
        t.lease_until = None
        db.commit()
    finally:
        db.close()

    # 所有权校验的释放（槽已被他人抢占时为空操作，绝不误删新锁），并确认结果
    released = _release_slot(card_id, task_id)
    logger.info(f"本地任务完成，释放下载槽: card={card_id} task={task_id} released={released}")
    return {"success": True, "slot_released": released, "message": "下载完成，已释放下载槽"}


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
                # code=claim_invalid：确实被别的会话 claim 着 → 仍拒绝，但不再谎报「其他设备在下载」
                return _api_error(409, "claim_invalid", "任务正在其他 claim 下下载中，无法取消")
            now = _utcnow_naive()
            t.status = "cancelled"
            t.finished_at = now
            t.claim_id = None
            t.claim_session = None
            t.claimed_at = None
            t.heartbeat_at = None
            t.lease_until = None
            db.commit()
            released = _release_slot(card_id, task_id)
            logger.info(f"本地任务取消，释放下载槽: card={card_id} task={task_id} released={released}")
        else:  # pending
            t.status = "cancelled"
            t.claim_session = None
            t.finished_at = _utcnow_naive()
            db.commit()
            released = True      # 未 claim 过的任务不涉及槽
        return {"success": True, "slot_released": released, "message": "已取消"}
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
            # code=claim_active：SKILL 据此知道「插件正在下这一本」，可稍后再 ack（旧行为只有一句 409）
            return _api_error(409, "claim_active", "任务正在本地下载中，不能直接确认")
        t.status = "done"
        t.finished_at = _utcnow_naive()
        t.claim_session = None
        db.commit()
    finally:
        db.close()
    # 兜底：若该任务仍持有下载槽（异常路径），释放之（所有权校验，空操作安全），并确认结果
    released = _release_slot(card_id, task_id)
    return {"success": True, "slot_released": released, "message": "已确认"}


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
    # raise_on_error：读不到锁时**绝不**当成「没有下载在进行」而放行 cookie，也不当成「没资格」硬拒，
    # 交由端点转 503 busy（可重试）—— 与心跳同源的原则：DB 抖动不许被翻译成任何一种「失败结论」
    lock = download_slot.get_lock(card_id, raise_on_error=True)
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
    try:
        _, err = _validate_active_claim(card_id, req.task_id, req.claim_id)
    except download_slot.SlotUnavailable:
        return _api_error(503, "busy", "服务器繁忙，请稍后重试（下载租约状态暂时读不到）",
                          headers={"Retry-After": "15"})
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
    try:
        _, err = _validate_active_claim(card_id, task_id, claim_id)
    except download_slot.SlotUnavailable:
        # 修 A3 的同族问题：DB 读不到锁时旧实现按「没有进行中的下载」拒绝（409），
        # 插件会把它记成账号错误、连吃 3 次就把整本判死。503 + Retry-After 才是可重试语义。
        return _api_error(503, "busy", "服务器繁忙，请稍后重试（下载租约状态暂时读不到）",
                          headers={"Retry-After": "15"})
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
