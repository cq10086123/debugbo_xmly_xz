"""系统日志路由 — SSE 实时推送 + 内存缓存"""

import asyncio
import logging
from collections import deque
from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api.deps import get_current_admin

router = APIRouter(tags=["系统日志"])  # 由 app.py 挂载到 /api/{admin_path}/log

# ── 内存日志缓存（最近 500 条）──
_log_buffer: deque[dict] = deque(maxlen=500)
_sse_queues: set[asyncio.Queue] = set()
_main_loop = None  # 首个 SSE 订阅时捕获主事件循环（emit 可能被任意线程触发）

# ── 日志等级映射 ──
_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "FATAL",
}


def _offer(q: asyncio.Queue, entry: dict) -> None:
    try:
        q.put_nowait(entry)
    except asyncio.QueueFull:
        pass


class _MemoryHandler(logging.Handler):
    """将日志记录写入内存队列并通知 SSE 订阅者"""

    def emit(self, record: logging.LogRecord):
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": _LEVEL_NAMES.get(record.levelno, "?"),
            "name": record.name,
            "message": self.format(record),
        }
        _log_buffer.append(entry)  # deque.append 在 CPython 下线程安全
        # 通知所有 SSE 订阅者。本方法可能被下载线程池等任意线程触发，
        # asyncio.Queue 非线程安全，必须经主循环 call_soon_threadsafe 投递。
        loop = _main_loop
        if loop is None:
            return
        for q in list(_sse_queues):
            try:
                loop.call_soon_threadsafe(_offer, q, entry)
            except RuntimeError:
                pass  # 循环已关闭


# 注册全局日志处理器（只注册一次）
_handler = _MemoryHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_root = logging.getLogger()
if not any(isinstance(h, _MemoryHandler) for h in _root.handlers):
    _root.addHandler(_handler)


# ════════════════════════════════════════
#  SSE 实时推送
# ════════════════════════════════════════

@router.get("/stream")
async def log_stream(_: bool = Depends(get_current_admin)):
    """SSE 端点：实时推送日志"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_queues.add(queue)

    async def gen():
        try:
            # 先发送历史日志
            history = list(_log_buffer)
            if history:
                import json
                yield f"data: {json.dumps(history, ensure_ascii=False)}\n\n"

            # 实时推送新日志
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30)
                    import json
                    yield f"data: {json.dumps([entry], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_queues.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
