"""公告推送接口 — 独立模块，不影响现有业务

包含两组路由：
- router       业务端（卡密 token）：GET /announcements/current，前端/插件拉取最新启用公告
- admin_router 管理端（管理员 token）：公告 CRUD，由 app.py 挂载到 /api/{admin_path}

已读状态不在后端跟踪：前端用 localStorage、插件用 chrome.storage.local 记录已读公告 id。
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.session import SessionLocal
from db.models import Announcement
from api.deps import get_current_card, get_current_admin

logger = logging.getLogger(__name__)


def _public(a: Announcement) -> dict:
    """业务端返回的精简结构（不暴露内部字段）"""
    return {
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _full(a: Announcement) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "active": a.active,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


# ════════════════════════════════════════
#  业务端（卡密用户）：拉取当前公告
# ════════════════════════════════════════
router = APIRouter(prefix="/api/announcements", tags=["公告"])


@router.get("/current")
async def current_announcement(_: dict = Depends(get_current_card)):
    """返回最新一条启用公告；无启用公告返回 announcement=None。

    前端/插件据此与本地已读 id 对比，决定是否弹出。
    """
    db = SessionLocal()
    try:
        a = (
            db.query(Announcement)
            .filter(Announcement.active.is_(True))
            .order_by(Announcement.id.desc())
            .first()
        )
        return {"success": True, "announcement": _public(a) if a else None}
    finally:
        db.close()


# ════════════════════════════════════════
#  管理端：公告 CRUD（app.py 挂载到 /api/{admin_path}/announcements）
# ════════════════════════════════════════
admin_router = APIRouter(prefix="/announcements", tags=["管理后台-公告"])


class AnnouncementUpsertRequest(BaseModel):
    title: str
    content: str
    active: bool = True


def _validate_payload(req: AnnouncementUpsertRequest) -> tuple[str, str]:
    title = (req.title or "").strip()
    content = (req.content or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="标题不能超过 200 字")
    if len(content) > 10000:
        raise HTTPException(status_code=400, detail="内容不能超过 10000 字")
    return title, content


@admin_router.get("")
async def list_announcements(_: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        rows = db.query(Announcement).order_by(Announcement.id.desc()).all()
        return {"success": True, "announcements": [_full(a) for a in rows]}
    finally:
        db.close()


@admin_router.post("")
async def create_announcement(req: AnnouncementUpsertRequest, _: bool = Depends(get_current_admin)):
    title, content = _validate_payload(req)
    db = SessionLocal()
    try:
        a = Announcement(title=title, content=content, active=req.active)
        db.add(a)
        db.commit()
        db.refresh(a)
        return {"success": True, "announcement": _full(a)}
    finally:
        db.close()


@admin_router.put("/{ann_id}")
async def update_announcement(ann_id: int, req: AnnouncementUpsertRequest, _: bool = Depends(get_current_admin)):
    title, content = _validate_payload(req)
    db = SessionLocal()
    try:
        a = db.query(Announcement).filter_by(id=ann_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="公告不存在")
        # 若内容变化，刷新 updated_at（onupdate 仅在字段被赋值时触发；显式赋值保证生效）
        a.title = title
        a.content = content
        a.active = req.active
        a.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(a)
        return {"success": True, "announcement": _full(a)}
    finally:
        db.close()


@admin_router.patch("/{ann_id}/active")
async def toggle_announcement(ann_id: int, _: bool = Depends(get_current_admin)):
    """启停切换（单独端点，便于列表页一键开关）"""
    db = SessionLocal()
    try:
        a = db.query(Announcement).filter_by(id=ann_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="公告不存在")
        a.active = not a.active
        a.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": True, "announcement": _full(a)}
    finally:
        db.close()


@admin_router.delete("/{ann_id}")
async def delete_announcement(ann_id: int, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        a = db.query(Announcement).filter_by(id=ann_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="公告不存在")
        db.delete(a)
        db.commit()
        return {"success": True, "message": "已删除"}
    finally:
        db.close()
