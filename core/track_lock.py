"""下载去重锁 — 防止多个批量/恢复/重试任务并发下载同一专辑的同一集

所有调用都发生在同一个 asyncio 事件循环内，
「检查 + 占用」之间没有 await，因此用普通 dict 即可保证原子性。

锁 key 在 release() 时主动删除；若因异常路径遗漏 release（未走 finally），
则依靠 TTL 兜底在 _TTL_SECONDS 后自动失效，避免永久残留导致该集永远无法再被下载。
"""

import re
import time

# 去重锁 TTL（秒）：正常路径下 release 会立即删除 key；
# 此处仅作为「异常遗漏 release」的兜底，超过该时长未释放的 key 自动过期。
_TTL_SECONDS = 30 * 60  # 30 分钟

# key -> 过期时间戳（time.monotonic()，避免系统时钟回拨影响 TTL 计算）
_locks: dict[str, float] = {}


def _sanitize(name: str) -> str:
    """清理名称中的非法字符（与各路由的文件名清洗规则保持一致）"""
    return re.sub(r'[\\/:*?"<>|\r\n]', "", name).strip()


def make_key(card_id: int | str, album_title: str, episode_num: int) -> str:
    """生成某一集的去重锁 key（两套下载引擎共用同一命名空间，可跨引擎去重）

    card_id 维度确保不同卡密任务的同名专辑互不抢锁（各自独立下载目录）。
    """
    return f"{card_id}::{_sanitize(album_title).lower()}::{episode_num}"


def _expire_now():
    """惰性清理所有已过期条目（无外部锁，单进程事件循环内调用安全）"""
    now = time.monotonic()
    expired = [k for k, exp in _locks.items() if exp <= now]
    for k in expired:
        _locks.pop(k, None)


def try_acquire(key: str) -> bool:
    """尝试占用某集的下载权，已被其他任务占用（且未过期）则返回 False"""
    _expire_now()
    if key in _locks:
        return False
    _locks[key] = time.monotonic() + _TTL_SECONDS
    return True


def release(key: str):
    """释放某集的下载权（无论是否过期都安全删除）"""
    _locks.pop(key, None)
