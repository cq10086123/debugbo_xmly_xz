import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_current_card, ensure_interface_allowed
from api.search import _do_search
from api.download import (
    start_batch_download, 
    BatchDownloadRequest, 
    get_batch_status, 
    retry_batch_failed,
    _make_downloader,
    _batch_tasks
)
from core import config as _config
from core.account_manager import list_accounts
from api.extension import list_local_tasks, create_local_task, CreateTaskRequest

router = APIRouter(prefix="/api/skills", tags=["AI_Skills"])

class SearchRequest(BaseModel):
    keyword: str = Field(..., description="用户想要搜索的书籍名称或作者名称")

class AlbumIdRequest(BaseModel):
    album_id: int = Field(..., description="书籍（专辑）的唯一 ID")

class DownloadSubmitRequest(BaseModel):
    album_id: int = Field(..., description="书籍（专辑）的唯一 ID")
    start_episode: int = Field(1, description="起始集数（如果不指定默认从 1 开始）")
    end_episode: Optional[int] = Field(None, description="结束集数（如果不指定则默认下载到最后一集）")
    fmt: str = Field("mp3", description="下载格式，通常为 mp3 或 m4a")

class TaskIdRequest(BaseModel):
    task_id: str = Field(..., description="下载任务的唯一 task_id")

@router.post("/search_books", summary="搜索书籍", description="当用户想听某本书但不知道 album_id 时调用此接口。返回相关书籍列表与对应的 album_id。")
async def skill_search_books(req: SearchRequest, auth: dict = Depends(get_current_card)):
    ensure_interface_allowed(auth, "official")
    data = await asyncio.to_thread(_do_search, req.keyword, 1)
    if data.get("ret") != 200:
        return {"success": False, "error": data.get("msg", "搜索失败")}
    
    docs = data.get("data", {}).get("result", {}).get("response", {}).get("docs", [])
    simplified = []
    for doc in docs[:10]: # 只取前10个减轻Token压力
        simplified.append({
            "album_id": doc.get("albumId"),
            "title": doc.get("title"),
            "author": doc.get("nickname"),
            "intro": doc.get("intro", "")[:100], # 截断简介
            "tracks_count": doc.get("tracks"),
            "is_finished": doc.get("isFinished") == 2
        })
    return {"success": True, "results": simplified}

@router.post("/get_chapters", summary="获取书籍章节概况", description="在用户要下载前，获取此书籍共有多少集。")
async def skill_get_chapters(req: AlbumIdRequest, auth: dict = Depends(get_current_card)):
    try:
        ensure_interface_allowed(auth, "official")
        dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
        result = await asyncio.to_thread(dl.get_track_list, req.album_id)
        if not result.get("success"):
            return result
        return {
            "success": True,
            "album_title": result.get("albumTitle"),
            "total_count": result.get("totalCount"),
            "suggestion": f"共找到 {result.get('totalCount')} 集，请询问用户需要下载哪几集（例如：1到10集）"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/submit_download", summary="提交服务器下载任务", description="当用户确认书籍和章节范围后调用。如果成功会返回 task_id，务必将 task_id 记住以查询进度。")
async def skill_submit_download(req: DownloadSubmitRequest, auth: dict = Depends(get_current_card)):
    try:
        # 直接复用现有批量下载接口逻辑
        inner_req = BatchDownloadRequest(
            album_id=req.album_id,
            start_episode=req.start_episode,
            end_episode=req.end_episode,
            fmt=req.fmt
        )
        return await start_batch_download(inner_req, auth=auth)
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/check_task_status", summary="查询下载任务进度", description="使用 submit_download 返回的 task_id 查询实时进度、失败情况。")
async def skill_check_task_status(req: TaskIdRequest, auth: dict = Depends(get_current_card)):
    status_resp = await get_batch_status(req.task_id, auth=auth)
    if not status_resp.get("success"):
        return status_resp
    
    # 抽取对大模型友好的精简数据
    summary = {
        "success": True,
        "task_id": req.task_id,
        "status": status_resp.get("status"),
        "total": status_resp.get("total"),
        "completed": status_resp.get("completed"),
        "skipped": status_resp.get("skipped", 0),
        "percent": status_resp.get("percent"),
        "failed_count": len(status_resp.get("failed_list", [])),
        "errors": status_resp.get("failed_list", [])[:5]  # 只返回前5个错误，避免Token爆炸
    }
    
    if summary["status"] == "running":
        summary["suggestion"] = "任务正在下载中，请告诉用户当前进度，稍后可再次查询。"
    elif summary["status"] == "done":
        summary["suggestion"] = "下载已完成！"
    elif summary["status"] == "error" or summary["failed_count"] > 0:
        summary["suggestion"] = "有部分文件下载失败或任务出错，可以提示用户使用 retry_task 接口重试。"
        
    return summary

@router.post("/retry_task", summary="重试失败的下载任务", description="当 check_task_status 显示有失败项时，调用此接口触发重试。")
async def skill_retry_task(req: TaskIdRequest, auth: dict = Depends(get_current_card)):
    return await retry_batch_failed(req.task_id, auth=auth)

@router.post("/get_card_info", summary="获取卡密状态信息", description="获取当前使用的卡密的剩余时间、有效状态等基本信息。")
def skill_get_card_info(auth: dict = Depends(get_current_card)):
    from db.session import SessionLocal
    from db.models import Card
    import time
    
    db = SessionLocal()
    try:
        card = db.query(Card).filter_by(id=auth["card_id"]).first()
        if not card:
            return {"success": False, "error": "卡密不存在"}
        
        is_expired = False
        expire_time_str = "永久有效"
        if card.expire_time:
            is_expired = time.time() > card.expire_time
            from datetime import datetime
            expire_time_str = datetime.fromtimestamp(card.expire_time).strftime('%Y-%m-%d %H:%M:%S')
            
        return {
            "success": True,
            "card_code": card.code,
            "is_expired": is_expired,
            "expire_time": expire_time_str,
            "download_mode": card.download_mode or "both"
        }
    finally:
        db.close()

@router.post("/check_accounts", summary="巡检官方账号状态", description="查询当前卡密名下是否绑定了喜马拉雅账号，以及账号是否失效。")
def skill_check_accounts(auth: dict = Depends(get_current_card)):
    accounts = list_accounts(auth["card_id"])
    if not accounts:
        return {
            "success": True, 
            "accounts_count": 0, 
            "suggestion": "当前卡密未绑定任何喜马拉雅官方账号。官方接口下载VIP音频需要有账号支持，请提醒用户去网页版扫码登录。"
        }
    
    return {
        "success": True,
        "accounts_count": len(accounts),
        "accounts_list": [{"nickname": acc.get("nickname"), "is_vip": acc.get("is_vip")} for acc in accounts],
        "suggestion": "账号状态正常。如果有下载VIP音频失败（如签名错误），可能是账号过期或风控，可提醒用户重新扫码。"
    }

from fastapi import Request
from fastapi.responses import JSONResponse

@router.get("/export_openapi", summary="导出 AI Skills 配置", description="下载专属的 OpenAPI 规范，内置了当前卡密。")
async def export_skills_openapi(request: Request, token: str):
    base_url = str(request.base_url).rstrip("/")
    
    openapi_schema = {
        "openapi": "3.1.0",
        "info": {
            "title": "喜马拉雅下载管家 Skills",
            "version": "1.0.0",
            "description": "专为 AI 助手设计的全流程音频下载控制接口。"
        },
        "servers": [
            {"url": base_url, "description": "您的服务器地址"}
        ],
        "paths": {
            "/api/skills/search_books": {
                "post": {
                    "summary": "搜索书籍",
                    "description": "当用户想听某本书但不知道 album_id 时调用此接口。返回相关书籍列表与对应的 album_id。",
                    "operationId": "skill_search_books",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "keyword": {"type": "string", "description": "用户想要搜索的书籍名称或作者名称"}
                                    },
                                    "required": ["keyword"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/get_chapters": {
                "post": {
                    "summary": "获取书籍章节概况",
                    "description": "在用户要下载前，获取此书籍共有多少集。",
                    "operationId": "skill_get_chapters",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "album_id": {"type": "integer", "description": "书籍（专辑）的唯一 ID"}
                                    },
                                    "required": ["album_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/submit_download": {
                "post": {
                    "summary": "提交服务器下载任务",
                    "description": "当用户确认书籍和章节范围后调用。如果成功会返回 task_id，务必将 task_id 记住以查询进度。",
                    "operationId": "skill_submit_download",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "album_id": {"type": "integer", "description": "书籍（专辑）的唯一 ID"},
                                        "start_episode": {"type": "integer", "default": 1, "description": "起始集数"},
                                        "end_episode": {"type": "integer", "description": "结束集数"},
                                        "fmt": {"type": "string", "default": "mp3", "description": "格式"}
                                    },
                                    "required": ["album_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_task_status": {
                "post": {
                    "summary": "查询下载任务进度",
                    "description": "使用 submit_download 返回的 task_id 查询实时进度、失败情况。",
                    "operationId": "skill_check_task_status",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string", "description": "下载任务的唯一 task_id"}
                                    },
                                    "required": ["task_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/retry_task": {
                "post": {
                    "summary": "重试失败的下载任务",
                    "description": "当 check_task_status 显示有失败项时，调用此接口触发重试。",
                    "operationId": "skill_retry_task",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string", "description": "下载任务的唯一 task_id"}
                                    },
                                    "required": ["task_id"]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/get_card_info": {
                "post": {
                    "summary": "获取卡密状态信息",
                    "description": "获取当前使用的卡密的剩余时间、有效状态等基本信息。",
                    "operationId": "skill_get_card_info",
                    "responses": {"200": {"description": "成功"}}
                }
            },

            "/api/skills/check_local_tasks": {
                "post": {
                    "summary": "查询浏览器插件本地下载状态",
                    "description": "查询当前卡密下有哪些书籍正在通过浏览器插件（本地电脑）下载，或者在排队等待下载，以及进度和报错信息。",
                    "operationId": "skill_check_local_tasks",
                    "responses": {"200": {"description": "成功"}}
                }
            },
            "/api/skills/check_accounts": {
                "post": {
                    "summary": "巡检官方账号状态",
                    "description": "查询当前卡密名下是否绑定了喜马拉雅账号，以及账号是否失效。",
                    "operationId": "skill_check_accounts",
                    "responses": {"200": {"description": "成功"}}
                }
            }
        }
    }

    # 为每个路由注入 Authorization Header 参数（内置当前卡密）
    auth_param = {
        "name": "Authorization",
        "in": "header",
        "description": "用户的卡密 Token",
        "required": True,
        "schema": {
            "type": "string",
            "default": f"Bearer {token}"
        }
    }

    for path, path_item in openapi_schema["paths"].items():
        for method, operation in path_item.items():
            if "parameters" not in operation:
                operation["parameters"] = []
            operation["parameters"].append(auth_param)

    return JSONResponse(
        content=openapi_schema,
        headers={"Content-Disposition": f'attachment; filename="ai_skills_config_{token[:4]}.json"'}
    )


@router.post("/check_local_tasks", summary="查询浏览器插件本地下载状态", description="查询当前卡密下有哪些书籍正在通过浏览器插件（本地电脑）下载，或者在排队等待下载，以及进度和报错信息。")
async def skill_check_local_tasks(auth: dict = Depends(get_current_card)):
    # 直接复用 extension.py 里现成的轮询接口，它返回当前卡密下所有 pending 和 running 的任务
    resp = await list_local_tasks(auth=auth)
    if not resp.get("success"):
        return resp
    
    tasks = resp.get("tasks", [])
    if not tasks:
        return {"success": True, "message": "目前没有本地插件下载任务在运行或排队。"}
    
    summary_list = []
    for t in tasks:
        # progress JSON 长这样: {"total": 100, "completed": 45, "skipped": 0}
        prog = t.get("progress") or {}
        total = prog.get("total", len(t.get("tracks", [])) or 1)
        completed = prog.get("completed", 0)
        skipped = prog.get("skipped", 0)
        failed_count = len(t.get("failed_list", []))
        
        summary_list.append({
            "task_id": t.get("task_id"),
            "album_title": t.get("album_title"),
            "status": t.get("status"), # running(下载中) 或 pending(排队等待认领)
            "total_episodes": total,
            "completed": completed,
            "failed_count": failed_count,
            "error_msg": t.get("error", "")
        })
        
    return {
        "success": True,
        "active_tasks_count": len(tasks),
        "tasks": summary_list,
        "suggestion": "如果有任务一直处于 pending（排队）状态，请提醒用户确保他们电脑上的浏览器开着且安装了插件。如果有失败的，可以提示用户在网页端重试。"
    }
