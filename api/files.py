"""文件管理接口 — /api/files（按卡密隔离）

- 列出当前卡密已下载文件（仅扫描 {DOWNLOAD_DIR}/{card_code}/）
- 专辑 ZIP 流式打包下载（每次只把单个文件读入内存，防路径穿越）
- 删除单个文件、去重清理
"""

import io
import logging
import os
import re
import struct
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse, FileResponse

from core import config as _config
from api.deps import get_current_card, get_current_card_download

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["文件管理"])

_AUDIO_SUFFIX = (".m4a", ".mp3", ".aac")


def _card_dir(auth: dict) -> Path:
    """当前卡密的下载根目录"""
    return _config.DOWNLOAD_DIR / auth["code"]


def _safe_resolve(card_dir: Path, rel_path: str) -> Path:
    """将相对路径解析到卡密目录内，若发生穿越则抛 403"""
    card_dir_resolved = card_dir.resolve()
    target = (card_dir / rel_path).resolve()
    try:
        target.relative_to(card_dir_resolved)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")
    return target


@router.get("")
async def list_downloaded_files(auth: dict = Depends(get_current_card)):
    card_dir = _card_dir(auth)
    if not card_dir.exists():
        return {"success": True, "files": [], "albums": []}

    files = []
    albums = {}
    for f in card_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIX:
            rel = f.relative_to(card_dir)
            info = {
                "name": f.name,
                "size": f.stat().st_size,
                "path": str(rel).replace("\\", "/"),
            }
            if len(rel.parts) > 1:
                album_name = rel.parts[0]
                info["album"] = album_name
                albums.setdefault(album_name, []).append(info)
            else:
                info["album"] = ""
                files.append(info)

    sorted_albums = []
    for name in sorted(albums.keys()):
        items = albums[name]
        sorted_albums.append({
            "name": name,
            "count": len(items),
            "total_size": sum(x["size"] for x in items),
            "files": sorted(items, key=lambda x: x["name"]),
        })
    files.sort(key=lambda x: x["name"])
    return {"success": True, "files": files, "albums": sorted_albums}


@router.get("/file/{file_path:path}")
async def get_file(file_path: str, auth: dict = Depends(get_current_card_download)):
    """下载单个已保存文件（限定在当前卡密目录内）"""
    card_dir = _card_dir(auth)
    target = _safe_resolve(card_dir, file_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(target), filename=target.name,
                        media_type="application/octet-stream")


@router.delete("/{file_path:path}")
async def delete_file(file_path: str, auth: dict = Depends(get_current_card)):
    card_dir = _card_dir(auth)
    target = _safe_resolve(card_dir, file_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        target.unlink()
    except OSError:
        raise HTTPException(status_code=500, detail="删除失败")
    # 清理空专辑目录（target 已 resolve，card_dir 也必须 resolve 后再比，否则路径形式不一致会误判）
    parent = target.parent
    if parent != card_dir.resolve() and not any(parent.iterdir()):
        try:
            parent.rmdir()
        except OSError:
            pass
    return {"success": True, "message": f"已删除 {file_path}"}


@router.post("/cleanup-duplicates")
async def cleanup_duplicates(auth: dict = Depends(get_current_card)):
    card_dir = _card_dir(auth)
    deleted = []
    if card_dir.exists():
        groups: dict[tuple, list] = {}
        for f in card_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIX:
                norm = re.sub(r"\(\d+\)", "", f.stem).strip().lower()
                groups.setdefault((str(f.parent), norm, f.suffix.lower()), []).append(f)
        for (_parent, norm, _ext), group in groups.items():
            if len(group) < 2:
                continue
            exact = [f for f in group if f.stem.strip().lower() == norm]
            keep = exact[0] if exact else max(group, key=lambda x: x.stat().st_size)
            for f in group:
                if f != keep:
                    try:
                        f.unlink()
                        deleted.append(str(f.relative_to(card_dir)).replace("\\", "/"))
                    except OSError:
                        pass
    return {"success": True, "deleted": deleted, "deleted_count": len(deleted)}


# ── 流式 ZIP 打包（每次只读入单个文件，避免一次性占满内存）──
def _stream_zip(files):
    """files: list of (arcname: str, path: Path)。生成 ZIP 字节流（store 模式）。"""
    # 设置语言编码标志位（bit 11 = 0x0800），使 ZIP 内中文文件名按 UTF-8 解析
    UTF8_FLAG = 0x0800
    entries = []
    pos = 0
    for arcname, path in files:
        data = path.read_bytes()  # 单文件读入内存（海量文件时依次处理，不全部驻留）
        crc = zlib.crc32(data) & 0xFFFFFFFF
        size = len(data)
        arcname_b = arcname.encode("utf-8")
        local = struct.pack('<IHHHHHIIIHH', 0x04034B50, 20, UTF8_FLAG, 0, 0, 0,
                            crc, size, size, len(arcname_b), 0) + arcname_b
        yield local
        entries.append((pos, arcname, crc, size))  # pos 此时为本地文件头起始偏移
        pos += len(local)
        yield data
        pos += size

    central = b""
    for (offset, arcname, crc, size) in entries:
        arcname_b = arcname.encode("utf-8")
        central += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014B50, 20, 20, UTF8_FLAG, 0, 0, 0,
                               crc, size, size, len(arcname_b), 0, 0, 0, 0, 0, offset) + arcname_b
    yield central
    yield struct.pack('<IHHHHIIH', 0x06054B50, 0, 0, len(entries), len(entries),
                      len(central), pos, 0)


@router.get("/album-zip")
async def album_zip(album: str, auth: dict = Depends(get_current_card_download)):
    """将指定专辑目录下的音频打包成 ZIP 流式返回（仅限当前卡密目录，防穿越）"""
    if not album or "/" in album or "\\" in album or album in (".", ".."):
        raise HTTPException(status_code=400, detail="非法专辑名")
    card_dir = _card_dir(auth)
    album_dir = (card_dir / album).resolve()
    try:
        album_dir.relative_to(card_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")

    if not album_dir.exists() or not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="专辑不存在")

    audio_files = [
        f for f in album_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIX
    ]
    if not audio_files:
        raise HTTPException(status_code=404, detail="专辑内无音频文件")

    files = [(f"{album}/{f.name}", f) for f in sorted(audio_files, key=lambda x: x.name)]
    # HTTP 头部只能用 latin-1；中文专辑名需用 RFC 5987 的 filename* 编码，
    # 同时保留一个 ASCII 兜底 filename 兼容旧浏览器，避免 UnicodeEncodeError 500
    encoded_name = quote(f"{album}.zip")
    headers = {
        "Content-Disposition": f'attachment; filename="album.zip"; filename*=UTF-8\'\'{encoded_name}',
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(
        _stream_zip(files),
        media_type="application/zip",
        headers=headers,
    )


@router.get("/extension-zip")
async def extension_zip(auth: dict = Depends(get_current_card_download)):
    """打包浏览器插件目录为 zip，供用户下载后加载已解压扩展。"""
    ext_dir = Path(__file__).resolve().parent.parent / "extension"
    if not ext_dir.is_dir():
        raise HTTPException(status_code=404, detail="插件目录不存在")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in ext_dir.rglob("*"):
            if fp.is_file():
                arcname = fp.relative_to(ext_dir).as_posix()
                zf.write(fp, arcname)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="yousheng_extension.zip"',
            "Content-Length": str(buf.getbuffer().nbytes),
        },
    )
