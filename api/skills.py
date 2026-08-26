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
    source: str = Field("official", description="使用的搜索接口名称。如果不指定，默认为 'official' (官方接口)。可以通过 get_sources 接口获取可用的 source 列表。")

class AlbumIdRequest(BaseModel):
    album_id: str = Field(..., description="书籍（专辑）的唯一 ID (可能是整数也可能是字符串)")
    source: str = Field("official", description="获取该书籍使用的接口名称。必须与搜索时使用的 source 保持一致！")

class DownloadSubmitRequest(BaseModel):
    album_id: str = Field(..., description="书籍（专辑）的唯一 ID")
    start_episode: int = Field(1, description="起始集数（如果不指定默认从 1 开始）")
    end_episode: Optional[int] = Field(None, description="结束集数（如果不指定则默认下载到最后一集）")
    fmt: str = Field("mp3", description="下载格式，通常为 mp3 或 m4a")
    source: str = Field("official", description="执行下载的接口名称。必须与搜索时使用的 source 保持一致！")

class TaskIdRequest(BaseModel):
    task_id: str = Field(..., description="下载任务的唯一 task_id")


@router.post("/get_sources", summary="获取所有可用的音源接口", description="返回系统中当前可用的音源接口列表。大模型在搜索书籍前，可以先调用此接口让用户选择使用哪个音源，或者直接列出给用户看。")
async def skill_get_sources(auth: dict = Depends(get_current_card)):
    from api.interfaces import public_interface_list
    return await public_interface_list(auth)

@router.post("/search_books", summary="搜索书籍", description="当用户想听某本书但不知道 album_id 时调用此接口。可以指定 source（默认 official）。返回相关书籍列表与对应的 album_id。")
async def skill_search_books(req: SearchRequest, auth: dict = Depends(get_current_card)):
    if req.source == "official":
        ensure_interface_allowed(auth, "official")
        data = await asyncio.to_thread(_do_search, req.keyword, 1)
        if data.get("ret") != 200:
            return {"success": False, "error": data.get("msg", "搜索失败")}
        
        docs = data.get("data", {}).get("result", {}).get("response", {}).get("docs", [])
        simplified = []
        for doc in docs[:10]:
            aid = doc.get("id", doc.get("albumId"))
            simplified.append({
                "source": "official",
                "album_id": aid,
                "title": doc.get("title"),
                "author": doc.get("nickname"),
                "intro": doc.get("intro", "")[:100],
                "tracks_count": doc.get("tracks"),
                "is_finished": doc.get("isFinished") == 2
            })
        return {"success": True, "results": simplified}
    else:
        from api.interfaces import intf_search
        resp = await intf_search(req.source, req.keyword, 1, auth=auth)
        if not resp.get("success"):
            return resp
        
        docs = resp.get("list", [])
        simplified = []
        for doc in docs[:10]:
            simplified.append({
                "source": req.source,
                "album_id": doc.get("id"),
                "title": doc.get("title"),
                "author": doc.get("author"),
                "intro": doc.get("intro", "")[:100],
                "tracks_count": "未知" 
            })
        return {"success": True, "results": simplified}


@router.post("/get_chapters", summary="获取书籍章节概况", description="在用户要下载前，获取此书籍共有多少集。必须传入搜索时获得的 source 和 album_id。")
async def skill_get_chapters(req: AlbumIdRequest, auth: dict = Depends(get_current_card)):
    try:
        if req.source == "official":
            ensure_interface_allowed(auth, "official")
            dl = _make_downloader(download_root=_config.DOWNLOAD_DIR / auth["code"], card_id=auth["card_id"])
            result = await asyncio.to_thread(dl.get_track_list, int(req.album_id))
            if not result.get("success"):
                return result
            return {
                "success": True,
                "source": "official",
                "album_title": result.get("albumTitle"),
                "total_count": result.get("totalCount"),
                "suggestion": f"共找到 {result.get('totalCount')} 集，请询问用户需要下载哪几集（例如：1到10集）"
            }
        else:
            from api.interfaces import intf_album_list, AlbumListRequest as IntfAlbumListReq
            inner_req = IntfAlbumListReq(book_id=str(req.album_id))
            result = await intf_album_list(req.source, inner_req, auth=auth)
            if not result.get("success"):
                return result
            
            chapters = result.get("chapters", [])
            return {
                "success": True,
                "source": req.source,
                "total_count": len(chapters),
                "suggestion": f"共找到 {len(chapters)} 集，请询问用户需要下载哪几集（例如：1到10集）"
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/submit_download", summary="提交服务器下载任务", description="当用户确认书籍和章节范围后调用。如果成功会返回 task_id，务必将 task_id 记住以查询进度。")
async def skill_submit_download(req: DownloadSubmitRequest, auth: dict = Depends(get_current_card)):
    try:
        if req.source == "official":
            inner_req = BatchDownloadRequest(
                album_id=int(req.album_id),
                start_episode=req.start_episode,
                end_episode=req.end_episode,
                fmt=req.fmt
            )
            return await start_batch_download(inner_req, auth=auth)
        else:
            from api.interfaces import intf_batch, BatchRequest as IntfBatchReq
            inner_req = IntfBatchReq(
                book_id=str(req.album_id),
                start_episode=req.start_episode,
                end_episode=req.end_episode,
                fmt=req.fmt,
                concurrency=3 
            )
            return await intf_batch(req.source, inner_req, auth=auth)
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/check_task_status", summary="查询下载任务进度", description="使用 submit_download 返回的 task_id 查询实时进度、失败情况。")
async def skill_check_task_status(req: TaskIdRequest, auth: dict = Depends(get_current_card)):
    status_resp = await get_batch_status(req.task_id, auth=auth)
    if not status_resp.get("success"):
        return status_resp
    
    summary = {
        "success": True,
        "task_id": req.task_id,
        "status": status_resp.get("status"),
        "total": status_resp.get("total"),
        "completed": status_resp.get("completed"),
        "skipped": status_resp.get("skipped", 0),
        "percent": status_resp.get("percent"),
        "failed_count": len(status_resp.get("failed_list", [])),
        "errors": status_resp.get("failed_list", [])[:5]  
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


@router.post("/reset_stuck_local_task", summary="重置卡死的本地插件任务", description="当 check_local_tasks 发现有任务长时间卡在 running 状态进度不动，或者是由于浏览器崩溃导致的僵尸任务时，调用此接口将其重置为 pending，让插件能重新接管下载。")
def skill_reset_stuck_local_task(req: TaskIdRequest, auth: dict = Depends(get_current_card)):
    from db.session import SessionLocal
    from db.models import LocalTask
    
    db = SessionLocal()
    try:
        task = db.query(LocalTask).filter_by(task_id=req.task_id, card_id=auth["card_id"]).first()
        if not task:
            return {"success": False, "error": "任务不存在或不属于当前卡密"}
            
        if task.status != "running":
            return {"success": False, "message": f"任务当前状态为 {task.status}，无需重置。"}
            
        task.status = "pending"
        task.claim_id = None
        task.claimed_at = None
        db.commit()
        return {
            "success": True, 
            "message": "已成功将僵尸任务打回排队池。请提示用户打开浏览器并确保插件开启，插件会在 30 秒内重新接管并继续下载。"
        }
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()

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
from fastapi.responses import StreamingResponse
import io
import zipfile
import json

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
            "/api/skills/get_sources": {
                "post": {
                    "summary": "获取所有可用的音源接口",
                    "description": "返回系统中当前可用的音源接口列表。大模型在搜索书籍前，可以先调用此接口让用户选择使用哪个音源，或者直接列出给用户看。",
                    "operationId": "skill_get_sources",
                    "responses": {"200": {"description": "成功"}}
                }
            },
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
                                        "keyword": {"type": "string", "description": "用户想要搜索的书籍名称或作者名称"},
                                        "source": {"type": "string", "default": "official", "description": "使用的搜索接口名称。如果不指定，默认为 official (官方接口)。"}
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
                                        "album_id": {"type": "string", "description": "书籍（专辑）的唯一 ID (可能是整数也可能是字符串)"},
                                        "source": {"type": "string", "default": "official", "description": "获取该书籍使用的接口名称。必须与搜索时使用的 source 保持一致！"}
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
                                        "album_id": {"type": "string", "description": "书籍（专辑）的唯一 ID"},
                                        "source": {"type": "string", "default": "official", "description": "执行下载的接口名称。必须与搜索时使用的 source 保持一致！"},
                                        "start_episode": {"type": "integer", "default": 1, "description": "起始集数"},
                                        "end_episode": {"type": "integer", "description": "结束集数，如果不提供，代表下载到最新/最后一集"},
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
            "/api/skills/reset_stuck_local_task": {
                "post": {
                    "summary": "重置卡死的本地插件任务",
                    "description": "当 check_local_tasks 发现有任务长时间卡在 running 状态进度不动，或者是由于浏览器崩溃导致的僵尸任务时，调用此接口将其重置为 pending，让插件能重新接管下载。",
                    "operationId": "skill_reset_stuck_local_task",
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
            },
            "/api/skills/get_card_info": {
                "post": {
                    "summary": "获取卡密状态信息",
                    "description": "获取当前使用的卡密的剩余时间、有效状态等基本信息。",
                    "operationId": "skill_get_card_info",
                    "responses": {"200": {"description": "成功"}}
                }
            }
        }
    }

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

    md_content = f"""# 喜马拉雅 AI 下载管家 - Skills 接入指南

本压缩包为您提供了专门为 LLM（大模型）定制的 OpenAPI 配置文件。

## 什么是这个 JSON 文件？
这个压缩包内的 `ai_skills_config.json` 是符合 OpenAPI 3.1 规范的 API 定义文件。它是传统大模型平台（如 Coze、Dify、FastGPT 等）最通用的“插件/工具”标准。

## 接入步骤 (以 Coze/扣子 为例)
1. 登录 Coze 工作台，进入“插件” -> “创建插件” -> 选择“导入”。
2. 将本压缩包内的 `ai_skills_config.json` 文件上传。
3. 平台会自动识别出 10 个 API 工具。
4. **⚠️ 重要安全特性：** 该 JSON 已自动为您硬编码了您当前的卡密凭证 (`Bearer {token}`)。您不需要配置复杂的 API Key 授权，直接保存即可使用！

## 推荐的 AI 提示词 (Prompt)
您可以直接将以下文案复制到您 Bot 的“系统提示词”中：

```text
你是一个专业的喜马拉雅有声书下载管家。你的职责是通过自然语言帮助用户寻找、下载和诊断音频任务。
你拥有以下技能链，必须遵循以下工作流：

1. **绝对禁止向用户索要技术参数**：你必须自己去调接口查 `album_id`、`source`、`task_id`，或者利用自己的对话记忆。
2. **多意图指令解析（极度智能）**：如果用户一句话包含了“搜索词、指定接口、指定第几本、指定集数、指定下载方式”，你要**极其聪明地**在脑海中连续静默调用 API。
    - 例如用户说：“搜索完美世界，使用免密接口A。下载第二本 1到10集，使用服务器下载。”
    - 你的内部操作：静默调用 `skill_search_books` (传入 keyword="完美世界", source="免密接口A") -> 拿到结果中的第二本书的 `album_id` -> 静默调用 `skill_submit_download` (传入 album_id, start_episode=1, end_episode=10, source="免密接口A")。
3. **分步引导（当指令不全时）**：如果用户只说了“帮我搜完美世界”，你就只展示搜索结果，并默默记住所有结果的 `album_id` 和 `source`，然后问用户“要下载哪一本的哪几集？”。
4. **服务器下载与本地插件**：`skill_submit_download` 只能提交到服务器。如果用户明确要求“本地下载/浏览器下载”，请告诉用户：“抱歉，目前通过微信AI只能触发服务器下载。如果要使用本地下载，请打开您的电脑浏览器前往网页端操作。”
5. **状态与修复**：如果用户问进度，调用 `skill_check_task_status`；如果遇到 running 卡死，主动提议调用 `skill_reset_stuck_local_task`。
"""

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        zip_file.writestr("ai_skills_config.json", json.dumps(openapi_schema, indent=2, ensure_ascii=False))
        zip_file.writestr("Skills_接入说明.md", md_content)

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ai_skills_config_{token[:4]}.zip"'}
    )

@router.post("/check_local_tasks", summary="查询浏览器插件本地下载状态", description="查询当前卡密下有哪些书籍正在通过浏览器插件（本地电脑）下载，或者在排队等待下载，以及进度和报错信息。")
async def skill_check_local_tasks(auth: dict = Depends(get_current_card)):
    resp = await list_local_tasks(auth=auth)
    if not resp.get("success"):
        return resp

    tasks = resp.get("tasks", [])
    if not tasks:
        return {"success": True, "message": "目前没有本地插件下载任务在运行或排队。"}

    summary_list = []
    for t in tasks:
        prog = t.get("progress") or {}
        total = prog.get("total", len(t.get("tracks", [])) or 1)
        completed = prog.get("completed", 0)
        skipped = prog.get("skipped", 0)

        failed_list = t.get("failed_list", [])
        failed_count = len(failed_list)
        failed_details = []
        for f_item in failed_list[:3]:
            if isinstance(f_item, dict):
                failed_details.append(f"集数 {f_item.get('episode_num', '未知')}: {f_item.get('error', '未知错误')}")
            else:
                failed_details.append(str(f_item)[:100])
        
        summary_list.append({
            "task_id": t.get("task_id"),
            "album_title": t.get("album_title"),
            "status": t.get("status"), 
            "total_episodes": total,
            "completed": completed,
            "failed_count": failed_count,
            "failed_details": failed_details,
            "error_msg": t.get("error", "")
        })
        
    return {
        "success": True,
        "active_tasks_count": len(tasks),
        "tasks": summary_list,
        "suggestion": "如有 pending 任务，提醒用户打开浏览器；如果 running 任务进度长时间卡死，可能浏览器已崩溃，建议询问用户是否调用 reset_stuck_local_task 重置；如果 failed_details 包含签名错误/403，提示扫码续期。"
    }
