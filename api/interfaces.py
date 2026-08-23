"""接口管理 + 统一调度 API

管理端（/api/admin/interfaces，管理员 token）：
- GET    列表
- POST   新增（自定义 http 接口 / 克隆官方或第三方）
- PUT    修改
- DELETE 删除（内置接口不允许删除，仅可禁用）
- POST   {name}/test  测试搜索/章节/音频

业务端（卡密 token）：
- GET  /api/interfaces              公开列表（供前端下拉选择音源）
- GET  /api/intf/{name}/search      搜索
- POST /api/intf/{name}/album-list  章节列表
- POST /api/intf/{name}/audio       获取音频直链
- POST /api/intf/{name}/batch       批量下载（直链型接口）
- GET  /api/intf/tasks              任务列表
- GET  /api/intf/tasks/{task_id}    任务详情
- POST /api/intf/tasks/{task_id}/cancel  取消
- DELETE /api/intf/tasks/{task_id}        删除（非运行中）
"""

import asyncio
import json
import logging
import re
import time
import uuid
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.interface_manager import (
    manager, VALID_TYPES, TYPE_SCRIPT,
)
from core.batch_runner import run_generic_batch
from core import config as _config
from api.deps import get_current_admin, get_current_card, ensure_interface_allowed, ensure_download_mode_allowed
from db.session import SessionLocal
from db.models import Interface

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["接口统一调度"])

# 管理端路由：不带前缀，由 app.py 挂载到 /api/{admin_path}
admin_router = APIRouter(tags=["接口管理（管理端）"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")

# 统一批量任务的进程内状态（独立于 legacy 官方/第三方任务系统）
_intf_tasks: Dict[str, dict] = {}


# ── 请求模型 ──
class InterfaceCreate(BaseModel):
    name: str
    display_name: str
    type: str = TYPE_SCRIPT
    enabled: bool = True
    priority: int = 100
    description: str = ""
    config: dict = {}


class InterfaceUpdate(BaseModel):
    display_name: str | None = None
    type: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    description: str | None = None
    config: dict | None = None


class InterfaceTest(BaseModel):
    keyword: str = "斗破苍穹"
    book_id: str = ""
    chapter_id: str = ""


class ScriptTestDraft(BaseModel):
    stage: str  # search | chapters | audio
    source: str
    timeout: int = 30
    params: dict = {}
    allowed_modules: list = []  # 调试时自定义追加可 import 的模块


class AlbumListRequest(BaseModel):
    book_id: str


class AudioRequest(BaseModel):
    book_id: str
    chapter_id: str


class BatchRequest(BaseModel):
    book_id: str
    start_episode: int = 1
    end_episode: int | None = None
    fmt: str = "mp3"
    concurrency: int = 1


# ════════════════════════════════════════
#  管理端 CRUD
# ════════════════════════════════════════
@admin_router.get("/interfaces")
async def list_interfaces(_: bool = Depends(get_current_admin)):
    return {"success": True, "interfaces": manager.list_interfaces()}


def _validate_script_config(cfg: dict):
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")
    scripts = cfg.get("scripts") or {}
    if not isinstance(scripts, dict):
        raise HTTPException(status_code=400, detail="config.scripts 必须是对象")
    for sec in ("search", "chapters", "audio"):
        src = scripts.get(sec)
        if src is not None and not isinstance(src, str):
            raise HTTPException(status_code=400, detail=f"config.scripts.{sec} 必须是字符串")
    # 运行时再做 AST 校验；这里仅做结构校验
    timeout = cfg.get("script_timeout")
    if timeout is not None and (not isinstance(timeout, int) or not (1 <= timeout <= 120)):
        raise HTTPException(status_code=400, detail="config.script_timeout 必须在 1~120 秒之间")
    allowed = cfg.get("allowed_modules")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
            raise HTTPException(status_code=400, detail="config.allowed_modules 必须是字符串数组")


@admin_router.post("/interfaces")
async def create_interface(req: InterfaceCreate, _: bool = Depends(get_current_admin)):
    name = req.name.strip()
    display_name = req.display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="接口名称不能为空")
    if not display_name:
        raise HTTPException(status_code=400, detail="显示名称不能为空")
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="接口名称只能包含字母、数字和下划线")
    if len(name) > 64:
        raise HTTPException(status_code=400, detail="接口名称不能超过 64 个字符")
    if req.type not in VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"type 必须是 {VALID_TYPES} 之一")

    db = SessionLocal()
    try:
        if db.query(Interface).filter_by(name=name).first():
            raise HTTPException(status_code=400, detail=f"接口 {name} 已存在")

        if req.type == TYPE_SCRIPT:
            _validate_script_config(req.config)

        row = Interface(
            name=name,
            display_name=display_name,
            type=req.type,
            enabled=req.enabled,
            builtin=False,
            priority=req.priority,
            config=json.dumps(req.config, ensure_ascii=False) if req.config else None,
            description=req.description,
        )
        db.add(row)
        db.commit()
        manager.reload(name)
        return {
            "success": True,
            "message": "接口创建成功",
            "interface": _row_to_dict(row),
        }
    finally:
        db.close()


@admin_router.put("/interfaces/{name}")
async def update_interface(name: str, req: InterfaceUpdate, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        row = db.query(Interface).filter_by(name=name).first()
        if not row:
            raise HTTPException(status_code=404, detail="接口不存在")

        if req.display_name is not None:
            row.display_name = req.display_name.strip()
        if req.type is not None:
            if req.type not in VALID_TYPES:
                raise HTTPException(status_code=400, detail=f"type 必须是 {VALID_TYPES} 之一")
            row.type = req.type
        if req.enabled is not None:
            row.enabled = req.enabled
        if req.priority is not None:
            row.priority = req.priority
        if req.description is not None:
            row.description = req.description
        if req.config is not None:
            if row.type == TYPE_SCRIPT:
                _validate_script_config(req.config)
            row.config = json.dumps(req.config, ensure_ascii=False) if req.config else None

        db.commit()
        manager.reload(name)
        return {"success": True, "message": "接口更新成功", "interface": _row_to_dict(row)}
    finally:
        db.close()


@admin_router.delete("/interfaces/{name}")
async def delete_interface(name: str, _: bool = Depends(get_current_admin)):
    db = SessionLocal()
    try:
        row = db.query(Interface).filter_by(name=name).first()
        if not row:
            raise HTTPException(status_code=404, detail="接口不存在")
        if row.builtin:
            raise HTTPException(status_code=400, detail="内置接口（官方/第三方）不允许删除，仅可禁用")
        db.delete(row)
        db.commit()
        manager.unregister(name)
        return {"success": True, "message": "接口已删除"}
    finally:
        db.close()


@admin_router.post("/interfaces/{name}/test")
async def test_interface(name: str, req: InterfaceTest, _: bool = Depends(get_current_admin)):
    adapter = manager.get_adapter_any(name)
    if not adapter:
        raise HTTPException(status_code=404, detail="接口不存在或构建失败")
    result: Dict[str, Any] = {"name": name, "type": type(adapter).__name__}

    # 搜索测试
    try:
        if req.keyword:
            s = adapter.search_books(req.keyword, 1)
            result["search"] = {
                "success": s.get("success", False),
                "count": len(s.get("results", [])),
                "error": s.get("error"),
                "sample": s.get("results", [])[:3],
            }
        else:
            result["search"] = {"success": False, "error": "未提供 keyword"}
    except Exception as e:  # noqa: BLE001
        result["search"] = {"success": False, "error": f"搜索异常: {e}"}

    # 章节测试
    try:
        if req.book_id:
            c = adapter.get_chapters(req.book_id)
            result["chapters"] = {
                "success": c.get("success", False),
                "album_title": c.get("album_title"),
                "count": len(c.get("tracks", [])),
                "error": c.get("error"),
                "sample": c.get("tracks", [])[:3],
            }
        else:
            result["chapters"] = {"success": False, "error": "未提供 book_id"}
    except Exception as e:  # noqa: BLE001
        result["chapters"] = {"success": False, "error": f"章节异常: {e}"}

    # 音频测试
    try:
        if req.book_id and req.chapter_id:
            u = adapter.get_audio_url(req.book_id, req.chapter_id)
            result["audio"] = {"success": bool(u), "url": (u or "")[:200]}
        else:
            result["audio"] = {"success": False, "error": "未提供 book_id/chapter_id"}
    except Exception as e:  # noqa: BLE001
        result["audio"] = {"success": False, "error": f"音频异常: {e}"}

    return {"success": True, "result": result}


@admin_router.post("/interfaces/test-script")
async def test_script_draft(req: ScriptTestDraft, _: bool = Depends(get_current_admin)):
    """在线调试：直接执行一段未保存的脚本源码（沙箱环境）。

    与参考项目 py 的契约一致：脚本定义 ``def parse(params):``，由 stage 决定参数与返回。
    """
    stage = req.stage.strip().lower()
    if stage not in ("search", "chapters", "audio"):
        raise HTTPException(status_code=400, detail="stage 必须是 search/chapters/audio 之一")

    from core.script_engine import ScriptInterface, ScriptSecurityError

    timeout = max(1, min(120, int(req.timeout or 30)))
    params_in = req.params or {}
    allowed_modules = [str(m).strip() for m in (req.allowed_modules or []) if str(m).strip()]

    # 按 stage 构建与 py 一致的 params
    now = int(time.time())
    if stage == "search":
        keyword = str(params_in.get("keyword", "斗破苍穹"))
        data = {
            "keyword": keyword,
            "encoded_keyword": urllib.parse.quote(keyword),
            "timestamp": now * 1000,
            "timestamp_sec": now,
            "page": int(params_in.get("page", 1)),
        }
    elif stage == "chapters":
        book_id = str(params_in.get("book_id", ""))
        if not book_id:
            return {"success": False, "error": "缺少 book_id"}
        data = {
            "bookId": book_id,
            "page": 1,
            "page0": 0,
            "size": int(params_in.get("size", 2000)) or 2000,
            "count": int(params_in.get("count", 2000)) or 2000,
            "timestamp": now * 1000,
            "timestamp_sec": now,
        }
        # 透传调试面板里额外填写的自定义字段
        for k, v in params_in.items():
            if k not in ("book_id", "size", "count"):
                data[k] = v
    else:  # audio
        book_id = str(params_in.get("book_id", ""))
        chapter_id = str(params_in.get("chapter_id", ""))
        if not book_id or not chapter_id:
            return {"success": False, "error": "缺少 book_id / chapter_id"}
        data = {
            "bookId": book_id,
            "chapterId": chapter_id,
            "trackId": chapter_id,
            "rid": chapter_id,
            "timestamp": now * 1000,
            "timestamp_sec": now,
        }
        for k, v in params_in.items():
            if k not in ("book_id", "chapter_id"):
                data[k] = v

    # transient：调试独占一个用完即销毁的 worker，避免调试态污染生产缓存
    try:
        iface = ScriptInterface(req.source, script_type=stage, timeout=timeout,
                                transient=True, extra_modules=allowed_modules)
        result = iface.execute(data)
        return {"success": True, "result": result}
    except ScriptSecurityError as e:
        return {"success": False, "error": f"安全校验失败: {e}"}
    except TimeoutError as e:
        return {"success": False, "error": f"执行超时: {e}"}
    except Exception as e:
        logger.exception(f"test-script error: {e}")
        return {"success": False, "error": f"运行异常: {e}"}
    finally:
        try:
            iface.shutdown()
        except Exception:
            pass


@admin_router.get("/interfaces/examples")
async def script_examples(_: bool = Depends(get_current_admin)):
    """返回各阶段的示例脚本（与参考项目 py 的脚本契约一致），供前端「加载示例」使用。"""
    from core.script_examples import get_all_examples
    return {"success": True, "examples": get_all_examples()}


def _row_to_dict(row: Interface) -> dict:
    cfg = {}
    if row.config:
        try:
            cfg = json.loads(row.config)
        except Exception:
            cfg = {}
    return {
        "name": row.name,
        "display_name": row.display_name,
        "type": row.type,
        "enabled": bool(row.enabled),
        "builtin": bool(row.builtin),
        "priority": row.priority,
        "description": row.description or "",
        "config": cfg,
    }


# ════════════════════════════════════════
#  业务端：公开列表 + 统一调度
# ════════════════════════════════════════
@router.get("/interfaces")
async def public_interface_list(auth: dict = Depends(get_current_card)):
    """前端音源下拉：返回已启用接口；若卡密绑定了接口，仅返回绑定的接口。"""
    items = manager.list_interfaces(enabled_only=True)
    bound = auth.get("bound")
    if bound is not None:
        items = [it for it in items if it["name"] in bound]
    return {
        "success": True,
        "interfaces": [
            {"name": it["name"], "display_name": it["display_name"], "type": it["type"], "enabled": True}
            for it in items
        ],
    }


def _get_adapter_or_404(name: str, auth: dict | None = None):
    if auth is not None:
        ensure_interface_allowed(auth, name)
    adapter = manager.get_adapter(name)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"接口 {name} 不存在或未启用")
    return adapter


@router.get("/intf/{name}/search")
async def intf_search(name: str, keyword: str, page: int = 1, auth: dict = Depends(get_current_card)):
    if not keyword or not keyword.strip():
        return {"success": False, "error": "请输入搜索关键词"}
    adapter = _get_adapter_or_404(name, auth)
    try:
        return await asyncio.to_thread(adapter.search_books, keyword.strip(), page)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"intf search error: {e}")
        return {"success": False, "error": f"搜索异常: {e}"}


@router.post("/intf/{name}/album-list")
async def intf_album_list(name: str, req: AlbumListRequest, auth: dict = Depends(get_current_card)):
    if not req.book_id:
        return {"success": False, "error": "请输入书籍 ID"}
    adapter = _get_adapter_or_404(name, auth)
    try:
        return await asyncio.to_thread(adapter.get_chapters, req.book_id)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"intf album-list error: {e}")
        return {"success": False, "error": f"获取章节列表失败: {e}"}


@router.post("/intf/{name}/audio")
async def intf_audio(name: str, req: AudioRequest, auth: dict = Depends(get_current_card)):
    if not req.book_id or not req.chapter_id:
        return {"success": False, "error": "缺少 book_id / chapter_id"}
    adapter = _get_adapter_or_404(name, auth)
    try:
        url = await asyncio.to_thread(adapter.get_audio_url, req.book_id, req.chapter_id)
        if not url:
            return {"success": False, "error": "未获取到下载链接"}
        return {"success": True, "url": url}
    except Exception as e:  # noqa: BLE001
        logger.exception(f"intf audio error: {e}")
        return {"success": False, "error": f"获取下载链接失败: {e}"}


@router.post("/intf/{name}/batch")
async def intf_batch(name: str, req: BatchRequest, auth: dict = Depends(get_current_card)):
    # 第三方接口的批量下载同样属于「服务器下载」，受卡密下载模式约束
    ensure_download_mode_allowed(auth, "server")
    adapter = _get_adapter_or_404(name, auth)
    if not adapter.supports_direct_download:
        raise HTTPException(
            status_code=400,
            detail=f"接口 {name}（{type(adapter).__name__}）不支持统一批量下载，请使用其专用面板",
        )
    if not req.book_id:
        raise HTTPException(status_code=400, detail="缺少 book_id")
    if not (1 <= req.concurrency <= 30):
        raise HTTPException(status_code=400, detail="concurrency 必须在 1~30 之间")

    card_id = auth["card_id"]
    download_root = str(_config.DOWNLOAD_DIR / auth["code"])
    task_id = str(uuid.uuid4())[:8]
    _intf_tasks[task_id] = {
        "task_id": task_id, "card_id": card_id, "interface": name,
        "download_root": download_root, "engine": name, "book_id": req.book_id,
        "start_episode": req.start_episode, "end_episode": req.end_episode,
        "status": "running", "album_title": "", "total": 0, "current": 0,
        "current_title": "", "completed": 0, "skipped_count": 0,
        "completed_files": [], "failed_list": [], "error": "", "last_error": "",
        "started_at": time.time(), "finished_at": None, "cancelled": False,
        "fmt": req.fmt, "concurrency": req.concurrency,
    }
    asyncio.create_task(run_generic_batch(
        adapter, _intf_tasks[task_id], card_id, req.book_id, req.start_episode,
        req.end_episode or 999999, req.fmt, req.concurrency, download_root,
    ))
    return {"success": True, "task_id": task_id}


# ── 统一任务状态（轮询）──
def _task_summary(task: dict) -> dict:
    total = task.get("total", 0) or 1
    completed = task.get("completed", 0)
    skipped = task.get("skipped_count", 0)
    done = completed + skipped
    pct = round(done / total * 100) if total > 0 else 0
    return {
        "task_id": task.get("task_id", ""),
        "interface": task.get("interface", ""),
        "interface_name": task.get("interface", ""),
        "status": task.get("status", ""),
        "album_title": task.get("album_title", ""),
        "book_id": task.get("book_id", ""),
        "total": task.get("total", 0),
        "current": task.get("current", 0),
        "current_title": task.get("current_title", ""),
        "completed": completed,
        "skipped_count": skipped,
        "failed_count": len(task.get("failed_list", [])),
        "failed_list": task.get("failed_list", []),
        "error": task.get("error", ""),
        "last_error": task.get("last_error", ""),
        "percent": pct,
        "fmt": task.get("fmt", "mp3"),
        "concurrency": task.get("concurrency", 1),
    }


@router.get("/intf/tasks")
async def list_intf_tasks(auth: dict = Depends(get_current_card)):
    card_id = auth["card_id"]
    tasks = sorted(
        (t for t in _intf_tasks.values() if t.get("card_id") == card_id),
        key=lambda t: t.get("started_at", 0), reverse=True,
    )
    return {"success": True, "tasks": [_task_summary(t) for t in tasks]}


@router.get("/intf/tasks/{task_id}")
async def get_intf_task(task_id: str, auth: dict = Depends(get_current_card)):
    task = _intf_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    return {"success": True, **_task_summary(task)}


@router.post("/intf/tasks/{task_id}/cancel")
async def cancel_intf_task(task_id: str, auth: dict = Depends(get_current_card)):
    task = _intf_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    task["cancelled"] = True
    task["status"] = "cancelling"
    return {"success": True, "message": "正在取消..."}


@router.delete("/intf/tasks/{task_id}")
async def delete_intf_task(task_id: str, auth: dict = Depends(get_current_card)):
    task = _intf_tasks.get(task_id)
    if not task or task.get("card_id") != auth["card_id"]:
        return {"success": False, "error": "任务不存在"}
    if task.get("status") in ("running", "cancelling"):
        return {"success": False, "error": "运行中的任务无法删除，请先取消并等待其停止"}
    del _intf_tasks[task_id]
    return {"success": True, "message": "已删除"}
