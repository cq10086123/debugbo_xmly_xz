"""喜马拉雅 WebUI — FastAPI 主入口（重构版：纯 API + 前端静态托管）

- 后端只提供 REST API（/api/...）
- 前端构建产物由 frontend/dist 静态托管（不存在时给出提示）
- 管理后台路由挂载于 /api/{admin_path}（api_config.admin_path，默认 admin，改后重启生效）
- 管理后台与 API 文档仅允许局域网 IP 访问（公网来源一律 403）
- 生命周期：初始化数据库 → 恢复中断任务 → 启动卡密过期清理后台任务
"""

import asyncio
import ipaddress
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

# ── 全局强制 IPv4，避免 IPv6 优先但不通导致 requests 卡死 ──
import urllib3.util.connection
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from core.config import ensure_dirs, HOST, PORT, get_admin_path, get_admin_lan_only
from db.init_db import init_db
from db.session import SessionLocal
from core.interface_manager import manager as iface_manager
from core import download_slot
from db.models import Card, LocalTask
from api.card_helpers import is_expired, mark_expired
from api.persistence import clear_card_data
from api.download import router as download_router, resume_interrupted_auto_retries as _resume_off, reload_tasks as _reload_off
from api.auth import router as auth_router
from api.admin import router as admin_router
from api.accounts import router as accounts_router
from api.search import router as search_router
from api.files import router as files_router
from api.log_router import router as log_router
from api.skills import router as skills_router
from api.interfaces import router as interfaces_router, admin_router as interfaces_admin_router
from api.extension import router as extension_router
from api.announcements import router as announcements_router, admin_router as announcements_admin_router

logger = logging.getLogger(__name__)


async def _card_expiry_loop():
    """后台定时任务：每分钟扫描过期卡密，标记 expired、清空数据（磁盘文件保留）"""
    # 延迟导入避免循环依赖（download/interfaces 在路由注册前已就绪）
    from api.download import _batch_tasks
    from api.interfaces import _intf_tasks
    while True:
        await asyncio.sleep(60)
        db = SessionLocal()
        try:
            cards = db.query(Card).filter(Card.status.in_(["active", "used"])).all()
            for card in cards:
                if is_expired(card):
                    logger.info(f"卡密 {card.code} 过期，执行清零")
                    mark_expired(db, card)
                    clear_card_data(card.id)
                    # 同步停止内存中的下载协程：否则过期卡的官方/第三方任务仍继续跑、继续写盘
                    for tasks in (_batch_tasks, _intf_tasks):
                        for t in list(tasks.values()):
                            if t.get("card_id") == card.id and t.get("status") not in ("done", "failed", "cancelled"):
                                t["cancelled"] = True
                                t["status"] = "cancelling"
                    # 全局下载槽：过期卡释放其锁，本地下载任务置 cancelled（卡已不可用）
                    download_slot.force_release(card.id)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    for lt in db.query(LocalTask).filter_by(card_id=card.id, status="running").all():
                        lt.status = "cancelled"
                        lt.finished_at = now
                        lt.claim_id = None
                        lt.lease_until = None
                        lt.error = "卡密已过期，任务已取消"
                    db.commit()
        except Exception as e:
            logger.exception(f"卡密过期扫描异常: {e}")
        finally:
            db.close()


async def _slot_maintenance_loop(interval: int = 30):
    """全局下载槽维护循环（30s 一拍）：

    1. 为进程内仍在运行的服务器下载任务（官方批量 / 第三方批量 / 单集）续约租约；
    2. 租约过期的本地任务（插件侧心跳已断）回退为 pending，重新可被 claim；
    3. 物理清理过期锁行（表卫生）。

    注意：循环只「续约」，绝不重新 acquire —— 管理员强制释放后不能把槽抢回来。
    """
    from api.download import _batch_tasks, _episode_jobs
    from api.interfaces import _intf_tasks
    while True:
        await asyncio.sleep(interval)
        try:
            running: list[tuple[int, str]] = []
            for tasks in (_batch_tasks, _intf_tasks):
                for t in list(tasks.values()):
                    if t.get("status") in ("running", "cancelling"):
                        running.append((t.get("card_id"), t.get("task_id")))
            for job_id, card_id in list(_episode_jobs.items()):
                running.append((card_id, job_id))
            for card_id, task_id in running:
                if card_id is None or not task_id:
                    continue
                ok, _ = download_slot.heartbeat(card_id, task_id,
                                                ttl_seconds=download_slot.SERVER_TTL_SECONDS)
                if not ok:
                    logger.debug(f"下载槽心跳未生效（可能已被释放）: card={card_id} task={task_id}")
            swept = download_slot.sweep_expired_local_tasks()
            if swept:
                logger.info(f"{swept} 个本地下载任务因租约过期回退为 pending")
            download_slot.expire_stale()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("下载槽维护循环异常")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    logger.info(f"管理后台 API 挂载于 /api/{app.state.admin_path}（仅局域网可访问）")
    # 服务器重启 ⇒ 进程内任务全部消亡 ⇒ 清理服务器下载遗留锁（本地插件锁保留）
    download_slot.cleanup_on_startup()
    # 建表并注入默认接口后，加载接口到管理器缓存
    iface_manager.reload_all()
    # 建表后重新加载上次运行的任务（标记 interrupted）
    _reload_off()
    # 恢复重启前中断的「失败自动重试」循环
    _resume_off()
    # 启动卡密过期清理后台任务
    asyncio.create_task(_card_expiry_loop())
    # 启动全局下载槽维护（服务器任务心跳 + 本地任务租约清扫）
    asyncio.create_task(_slot_maintenance_loop())
    logger.info("应用启动完成")
    yield


app = FastAPI(
    title="喜马拉雅音频下载器",
    description="基于 WebUI 的喜马拉雅音频下载工具（卡密授权版）",
    version="2.0.0",
    lifespan=lifespan,
)

# 管理后台地址段：模块加载时从 api_config 读取（首次运行表不存在则回退默认 admin）。
# 注意必须在 "/" 静态托管之前注册，否则会被 StaticFiles 捕获。
_ADMIN_PATH = get_admin_path()
app.state.admin_path = _ADMIN_PATH


# ════════════════════════════════════════
#  局域网限制：管理后台 API + API 文档仅允许私网/回环 IP
# ════════════════════════════════════════
def _is_lan_ip(ip_str: str) -> bool:
    """判断来源 IP 是否为局域网/本机（私网段、回环、链路本地）"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


# 除管理后台前缀外，API 文档同样只允许局域网（避免公网暴露接口结构）
_LAN_ONLY_EXACT_PATHS = {"/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def admin_lan_only(request: Request, call_next):
    path = request.url.path
    admin_prefix = f"/api/{request.app.state.admin_path}"
    lan_only = path in _LAN_ONLY_EXACT_PATHS or path == admin_prefix or path.startswith(admin_prefix + "/")
    if lan_only:
        if not get_admin_lan_only():
            # 开关关闭：允许公网访问管理后台与文档页（即时生效，无需重启）
            return await call_next(request)
        # 经 resolve_client_ip 取真实来源：trust_proxy_header 开启（可信反代部署）时
        # 解析 XFF/X-Real-IP —— 否则反代场景下所有请求的直连 IP 都是代理 IP（内网），
        # 「仅局域网」限制会对公网访客形同虚设；开关关闭时行为与直连语义完全一致。
        from core.device_binding import resolve_client_ip
        client_ip = resolve_client_ip(request) or ""
        if not _is_lan_ip(client_ip):
            logger.warning(f"拒绝公网访问管理后台: {client_ip} -> {path}")
            return JSONResponse({"detail": "管理后台仅允许局域网访问"}, status_code=403)
    return await call_next(request)


# 为插件/浏览器扩展提供跨域支持（仅 API，不影响管理后台 IP 限制）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 注册 API 路由（业务端：卡密用户可用，不做 IP 限制）
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(search_router)
app.include_router(download_router)
app.include_router(files_router)
app.include_router(interfaces_router)
app.include_router(extension_router)
app.include_router(announcements_router)

# 管理端路由：挂载于 /api/{admin_path}（仅局域网，见上方中间件）
app.include_router(admin_router, prefix=f"/api/{_ADMIN_PATH}")
app.include_router(interfaces_admin_router, prefix=f"/api/{_ADMIN_PATH}")
app.include_router(announcements_admin_router, prefix=f"/api/{_ADMIN_PATH}")
app.include_router(log_router, prefix=f"/api/{_ADMIN_PATH}/log")
app.include_router(skills_router)


# 静态托管前端构建产物（若存在）
_dist = BASE_DIR / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
else:
    @app.get("/")
    async def index():
        return JSONResponse({
            "message": "后端 API 已运行。前端未构建，请进入 frontend/ 执行 npm install && npm run build。",
            "api_docs": "/docs",
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        reload=False,
    )
