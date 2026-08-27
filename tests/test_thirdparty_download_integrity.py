"""第三方接口下载的完整性回归（真实环境实测发现的缺陷）。

三个来自实机验证的 bug：
1. 第三方任务只在「启动失败」和「全部结束」两个时刻落库，服务器在下载途中
   崩溃 → 该任务从未持久化 → 重启后彻底消失，进度全丢。
2. album_title 为空时（脚本接口书名取自搜索缓存，直接按 book_id 下载时取不到），
   所有书都写进卡密根目录的「第N集.mp3」→ 不同书互相覆盖，
   且后一本被整本误判「已存在」而全部跳过（用户看到 done 但一集没下）。
3. 适配器是全局单例，_search_cache/_chapter_cache 每次搜索都整体 clear
   → 别的用户一搜索，就把你正准备下载的书从缓存里挤掉，触发 bug 2。
"""
import uuid
import pytest

RUN = uuid.uuid4().hex[:8]


def test_intf_batch_persists_immediately():
    """提交下载后必须立刻落库（与官方 batch 一致），否则途中崩溃就永久丢失。"""
    import inspect
    from api import interfaces
    src = inspect.getsource(interfaces.intf_batch)
    assert "persist_task" in src, "intf_batch 未在创建任务时落库"
    assert src.index("persist_task") < src.index("run_generic_batch"), \
        "persist_task 必须在启动下载协程之前调用"


def test_batch_runner_checkpoints_progress():
    """每集结束后要有进度检查点，否则崩溃时丢掉全部已完成进度。"""
    import inspect
    from core import batch_runner
    src = inspect.getsource(batch_runner.run_generic_batch)
    # finally 块里的 persist_task 就是检查点
    assert src.count("persist_task") >= 4, "缺少逐集进度检查点"
    assert "track_lock.release" in src


@pytest.mark.parametrize("album_title,book_id,expected_dir", [
    ("书A", "BOOK_A", "书A"),
    ("", "BOOK_A", "book_BOOK_A"),      # 无书名 → 按 book_id 隔离
    (None, "BOOK_B", "book_BOOK_B"),
])
def test_empty_album_title_still_isolates_books(tmp_path, album_title, book_id, expected_dir):
    """核心：书名为空也必须每本书一个目录，绝不能挤在根目录互相覆盖。"""
    from core.batch_runner import _episode_target_path
    folder = album_title or f"book_{book_id}"
    save_dir, path = _episode_target_path(folder, "第1集", "mp3", tmp_path)
    assert save_dir == tmp_path / expected_dir
    assert path.parent.name == expected_dir


def test_two_untitled_books_do_not_collide(tmp_path):
    """两本都没书名的书，落盘路径必须不同（旧代码里两者都是 根/第1集.mp3）。"""
    from core.batch_runner import _episode_target_path
    _, p1 = _episode_target_path("book_BOOK_A", "第1集", "mp3", tmp_path)
    _, p2 = _episode_target_path("book_BOOK_B", "第1集", "mp3", tmp_path)
    assert p1 != p2, "不同书的第1集落到了同一个文件，会互相覆盖"


def test_search_cache_not_cleared_by_other_users():
    """适配器是全局单例：A 用户搜索后，B 用户搜索不得挤掉 A 的结果。"""
    import inspect
    from core.interface_manager import ScriptAdapter
    src = inspect.getsource(ScriptAdapter.search_books)
    assert "_search_cache.clear()" not in src, \
        "search_books 仍在整体清空共享缓存，会破坏其他用户正在进行的下载"


def test_chapter_cache_only_evicts_same_book():
    """章节缓存同理：只能清当前书自己的条目，不能清别人的。"""
    import inspect
    from core.interface_manager import ScriptAdapter
    src = inspect.getsource(ScriptAdapter.get_chapters)
    assert "_chapter_cache.clear()" not in src, \
        "get_chapters 仍在整体清空共享缓存"
    assert "k[0] == str(book_id)" in src, "应只淘汰当前 book_id 的旧条目"


def test_caches_have_size_bounds():
    """既然不再 clear，就必须有上限，否则长期运行会无限增长。"""
    from core.interface_manager import ScriptAdapter
    assert ScriptAdapter._SEARCH_CACHE_MAX > 0
    assert ScriptAdapter._CHAPTER_CACHE_MAX > 0


def test_search_cache_accumulates_and_trims():
    """功能性验证：累积保留 + 超限淘汰最旧。"""
    from core.interface_manager import ScriptAdapter
    a = ScriptAdapter.__new__(ScriptAdapter)
    a._search_cache = {}
    for i in range(ScriptAdapter._SEARCH_CACHE_MAX + 50):
        a._search_cache[f"b{i}"] = {"id": f"b{i}"}
    a._trim_search_cache()
    assert len(a._search_cache) == ScriptAdapter._SEARCH_CACHE_MAX
    assert "b0" not in a._search_cache          # 最旧的被淘汰
    assert f"b{ScriptAdapter._SEARCH_CACHE_MAX + 49}" in a._search_cache  # 最新的保留


def test_chapter_cache_trims():
    from core.interface_manager import ScriptAdapter
    a = ScriptAdapter.__new__(ScriptAdapter)
    a._chapter_cache = {}
    for i in range(ScriptAdapter._CHAPTER_CACHE_MAX + 10):
        a._chapter_cache[("bk", str(i))] = {"chapter_id": str(i)}
    a._trim_chapter_cache()
    assert len(a._chapter_cache) == ScriptAdapter._CHAPTER_CACHE_MAX
