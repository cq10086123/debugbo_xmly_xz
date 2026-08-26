"""夸克挂载同步 — /api/quark

仅管理员在后台开通 quark_sync 的卡密可用。
手动触发：文件管理页；AI 触发：/api/skills。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_current_card, ensure_quark_sync_allowed
from core import quark_sync as qs

router = APIRouter(prefix="/api/quark", tags=["夸克同步"])


class SyncRequest(BaseModel):
    album: str = Field(..., description="已下载书籍（专辑目录）名称")


def _guard_perm(auth: dict) -> None:
    ensure_quark_sync_allowed(auth)


def _guard(auth: dict) -> None:
    _guard_perm(auth)
    try:
        qs.quark_dir()
    except qs.QuarkSyncError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/status")
async def quark_status(auth: dict = Depends(get_current_card)):
    """当前卡密是否开通、挂载是否可用、进行中的任务。"""
    mounted = True
    mount_error = ""
    try:
        qs.quark_dir()
    except qs.QuarkSyncError as e:
        mounted = False
        mount_error = str(e)
    return {
        "success": True,
        "allowed": bool(auth.get("quark_sync")),
        "mounted": mounted,
        "mount_error": mount_error,
        "running": qs.running_job_for_card(auth["card_id"]),
    }


@router.post("/sync")
async def start_quark_sync(req: SyncRequest, auth: dict = Depends(get_current_card)):
    _guard(auth)
    try:
        job = qs.start_sync(auth["card_id"], auth["code"], req.album)
        return {"success": True, **job}
    except qs.QuarkSyncError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/jobs")
async def list_quark_jobs(auth: dict = Depends(get_current_card)):
    _guard_perm(auth)
    return {"success": True, "jobs": qs.list_jobs(auth["card_id"])}


@router.get("/jobs/{job_id}")
async def get_quark_job(job_id: str, auth: dict = Depends(get_current_card)):
    _guard_perm(auth)
    job = qs.get_job(job_id, auth["card_id"])
    if not job:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    return {"success": True, **job}
