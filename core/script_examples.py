# -*- coding: utf-8 -*-
"""
脚本示例模板（与参考项目 C:/Users/11754/Desktop/1/py 的脚本契约完全一致）

所有脚本只定义一个函数：``def parse(params):``
- 脚本自己发请求（完整请求脚本 / full 形式），无需引擎代发。
- 参数 params 由引擎按阶段自动注入（keyword / bookId / chapterId / timestamp 等）。
- 返回结构：
    search   : list[dict]，每项必含 id、bookTitle（可选 bookImage/bookAnchor/count 等）
    chapters : list[dict]，每项必含 chapter_id、title（可选 order/duration 等）
    audio    : 音频直链字符串（http/https 开头）

注意：示例里的 URL 均为占位（api.example.com），请替换成真实接口。
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 搜索完整请求脚本
# ═══════════════════════════════════════════════════════════════════════════════
SEARCH_SCRIPT = '''# ═══════════════════════════════════════════════════════════════
# 【搜索完整请求脚本】def parse(params) —— 自己发请求 + 解析
# ═══════════════════════════════════════════════════════════════════════════
#
# 【输入 params 可用字段】
#   keyword         - 搜索关键词（中文）
#   encoded_keyword - URL 编码后的关键词
#   timestamp       - 毫秒时间戳
#   timestamp_sec   - 秒级时间戳
#   page            - 页码（从 1 开始）
#
# 【输出】书籍列表 list[dict]，每项必含：
#   id        - 书籍唯一 ID（字符串）★必须★
#   bookTitle - 书籍标题 ★必须★
# 可选：bookImage(封面) / bookAnchor(主播) / count(集数) / bookDesc(简介) 等
#
# 【自定义字段传递】★重要★：搜索结果里的任何字段都会透传给【章节脚本】，
#   例如 book['albumId'] = item.get('albumId')，章节里用 params.get('albumId') 读取。
# ═══════════════════════════════════════════════════════════════════════════

import requests

def parse(params):
    keyword = params.get('keyword', '')
    page = params.get('page', 1)

    # TODO: 替换成你的真实搜索接口（这里用占位 URL）
    url = "https://api.example.com/search"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params={"kw": keyword, "page": page},
                             headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"请求失败: {e}")
        return []

    # 按你的真实响应结构提取
    items = data.get('data', {}).get('list', []) or []
    result = []
    for item in items:
        book = {
            'id': str(item.get('id', '')),
            'bookTitle': item.get('title', ''),
            'bookImage': item.get('cover', ''),
            'bookAnchor': item.get('author', ''),
            'count': item.get('trackCount', 0),
            # 'albumId': item.get('albumId'),   # 自定义字段，会透传给章节脚本
        }
        if book['id'] and book['bookTitle']:
            result.append(book)

    print(f"共获取到 {len(result)} 本书")
    return result
'''

# ═══════════════════════════════════════════════════════════════════════════════
# 章节完整请求脚本
# ═══════════════════════════════════════════════════════════════════════════════
CHAPTERS_SCRIPT = '''# ═══════════════════════════════════════════════════════════════
# 【章节完整请求脚本】def parse(params) —— 自己发请求 + 解析
# ═══════════════════════════════════════════════════════════════════════════
#
# 【输入 params 可用字段】
#   bookId          - 书籍 ID（来自搜索结果的 id 字段）
#   page / page0    - 页码（page 从 1 开始，page0 从 0 开始）
#   size            - 每页数量
#   count           - 总章节数（来自搜索结果的 count 字段）
#   timestamp / timestamp_sec - 时间戳
#   params.get('xxx') - 搜索脚本透传的任意自定义字段
#
# 【输出】章节列表 list[dict]，每项必含：
#   chapter_id - 章节唯一 ID（字符串）★必须★
#   title      - 章节标题 ★必须★
# 可选：order(序号) / duration(时长) 等
#
# 【自定义字段传递】★重要★：章节结果里的任何字段会透传给【音频脚本】，
#   例如 chapter['trackId'] = item.get('trackId')，音频里用 params.get('trackId') 读取。
# ═══════════════════════════════════════════════════════════════════════════

import requests

def parse(params):
    book_id = params.get('bookId', '')
    page = params.get('page', 1)
    size = params.get('size', 2000)

    # TODO: 替换成你的真实章节接口
    url = f"https://api.example.com/chapters?albumId={book_id}&page={page}&size={size}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"请求失败: {e}")
        return []

    items = data.get('chapters', []) or data.get('data', {}).get('list', []) or []
    result = []
    for idx, item in enumerate(items):
        chapter = {
            'chapter_id': str(item.get('id', '')),
            'title': item.get('name', '') or item.get('title', ''),
            'order': idx + 1,
            'duration': item.get('duration', 0),
            # 'trackId': item.get('trackId'),   # 自定义字段，会透传给音频脚本
        }
        if chapter['chapter_id'] and chapter['title']:
            result.append(chapter)

    print(f"共获取到 {len(result)} 个章节")
    return result
'''

# ═══════════════════════════════════════════════════════════════════════════════
# 音频完整请求脚本
# ═══════════════════════════════════════════════════════════════════════════════
AUDIO_SCRIPT = '''# ═══════════════════════════════════════════════════════════════
# 【音频完整请求脚本】def parse(params) —— 自己发请求 + 提取直链
# ═══════════════════════════════════════════════════════════════════════════
#
# 【输入 params 可用字段】
#   bookId          - 书籍 ID
#   chapterId       - 章节 ID（来自章节结果的 chapter_id 字段）
#   trackId / rid   - 同 chapterId（兼容别名）
#   timestamp / timestamp_sec - 时间戳
#   params.get('xxx') - 章节脚本透传的任意自定义字段（如 trackId / playUrl）
#
# 【输出】音频直链字符串（http/https 开头），例如 "https://.../xxx.mp3"
# ═══════════════════════════════════════════════════════════════════════════

import requests
import re
import json

def parse(params):
    chapter_id = params.get('chapterId') or params.get('trackId', '')

    # TODO: 替换成你的真实音频接口
    url = f"https://api.example.com/audio?id={chapter_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"请求失败: {e}")
        return None

    # 方式1：直接取字段
    audio_url = data.get('url', '') or data.get('playUrl', '')
    # 方式2：正则兜底
    if not audio_url:
        text = json.dumps(data)
        m = re.search(r'https?://[^"\\s]+\\.(mp3|m4a)[^"\\s]*', text)
        if m:
            audio_url = m.group(0)

    print(f"音频URL: {audio_url[:80]}..." if audio_url else "未找到")
    return audio_url
'''


def get_all_examples() -> dict[str, str]:
    """获取所有示例脚本"""
    return {
        "search": SEARCH_SCRIPT,
        "chapters": CHAPTERS_SCRIPT,
        "audio": AUDIO_SCRIPT,
    }
