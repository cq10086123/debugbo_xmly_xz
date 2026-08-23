"""Python 脚本接口执行引擎（安全沙箱版，常驻 Worker 池）

为「高级接口」提供纯 Python 扩展能力：

- 脚本在独立子进程中运行，支持强制超时终止（进程级隔离，仍是第一道安全边界）。
- AST 静态检查 + 受限 builtins + 模块黑名单（仅拦 os/sys/subprocess/socket/pickle/ctypes 等），
  其余模块（含 requests / Crypto / jwt 等第三方）默认放行，降低 RCE 风险又方便对接。
- 脚本契约与参考项目 ``C:\\Users\\11754\\Desktop\\1\\py`` **完全一致**：
  每个阶段（搜索/章节/音频）只定义一个 ``def parse(params):`` 函数，脚本自己发请求。
    - search   : params = {keyword, encoded_keyword, timestamp, timestamp_sec, page}
                 返回 书籍列表 list[dict]，每项必含 id、bookTitle
    - chapters : params = {bookId, page, page0, size, count, timestamp, timestamp_sec, **自定义字段}
                 返回 章节列表 list[dict]，每项必含 chapter_id、title
    - audio    : params = {bookId, chapterId, trackId, rid, timestamp, timestamp_sec, **自定义字段}
                 返回 音频直链字符串（http/https 开头）
- 内置 HTTP 客户端与常用加签/编码工具，方便对接第三方 API。
- **常驻 Worker 池**：每个脚本源只加载一次并长期驻留，跨调用保持模块级全局状态
  （可复用 requests.Session、登录态、缓存等），下载时不再每集新开进程。

脚本内可用的两种 HTTP 方式（二选一）：
  1. 直接用内置的 ``requests``（已预注入全局命名空间，无需 import）：
         resp = requests.get(url, headers=..., timeout=10)
  2. 用内置辅助函数 ``http_get`` / ``http_post``：
         resp = http_get(url, params=..., headers=..., timeout=10)
"""

from __future__ import annotations

import ast
import base64
import binascii
import collections
import datetime as _dt
import functools
import hashlib
import hmac
import html
import itertools
import json
import logging
import math
import multiprocessing
import random
import re
import string
import threading
import time
import traceback
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════
#  安全配置
# ════════════════════════════════════════════

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120

# 模块导入策略：默认放行所有 import，仅拦截下面的危险模块黑名单。
# 这样用户无需配置白名单即可使用 Crypto / jwt / numpy 等任意第三方库，
# 同时保留对 os/sys/subprocess/socket/pickle/ctypes 等高危模块的硬隔离。
# 禁止顶层 import 的模块（能接触文件系统 / 进程 / 解释器内部的入口）
_BLOCKED_MODULES: Tuple[str, ...] = (
    "os", "sys", "subprocess", "importlib", "builtins", "__builtin__",
    "socket", "pickle", "marshal", "ctypes", "multiprocessing", "threading",
    "signal", "resource", "pty", "fcntl", "termios", "commands", "popen2",
    "posix", "nt", "shutil", "pathlib", "tempfile", "glob", "fnmatch", "io",
)

# 禁止调用的函数名
_BLOCKED_CALL_NAMES: Tuple[str, ...] = (
    "eval", "exec", "compile", "__import__", "open", "input", "exit", "quit",
    "getattr", "setattr", "delattr", "vars", "locals", "globals",
)

# 禁止访问的属性名（防 __class__.__subclasses__ 等绕过）
_BLOCKED_ATTRS: Tuple[str, ...] = (
    "__class__", "__bases__", "__base__", "__mro__", "__subclasses__",
    "__globals__", "__dict__", "__getattribute__", "__getattr__",
    "__init__", "__new__", "__call__", "__import__", "__builtins__",
    "__module__", "__closure__", "__code__", "__defaults__", "__func__",
    "__self__", "__weakref__", "__reduce__", "__reduce_ex__",
)

# 允许的 AST 节点类型（白名单）
_ALLOWED_AST_NODES: Tuple[type, ...] = (
    ast.Module, ast.Expression,
    ast.Expr, ast.FunctionDef,
    ast.Return, ast.Assign, ast.AnnAssign, ast.AugAssign,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Raise, ast.Assert, ast.Delete, ast.Global, ast.Nonlocal,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.IfExp,
    ast.Dict, ast.Set, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.Compare, ast.Call, ast.FormattedValue, ast.JoinedStr,
    ast.Constant, ast.Attribute, ast.Subscript, ast.Starred,
    ast.Name, ast.List, ast.Tuple,
    ast.Slice, ast.Index if hasattr(ast, "Index") else ast.Constant,
    ast.Load, ast.Store, ast.Del,
    ast.And, ast.Or,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.Invert, ast.Not, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.comprehension, ast.ExceptHandler, ast.arguments, ast.arg, ast.keyword,
    ast.alias, ast.Import, ast.ImportFrom,
    ast.withitem if hasattr(ast, "withitem") else ast.Constant,
)

# 允许使用 with 语句（例如 requests.Session() 上下文）
_ALLOW_WITH = True
if _ALLOW_WITH:
    _ALLOWED_AST_NODES += (ast.With, ast.Try)

# 受限 builtins：仅保留纯函数/常量，移除一切能接触运行环境的入口
_SAFE_BUILTINS: Dict[str, Any] = {
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "bytearray": bytearray, "bytes": bytes, "chr": chr, "complex": complex,
    "dict": dict, "dir": dir, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format, "frozenset": frozenset,
    "hasattr": hasattr, "hash": hash, "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "oct": oct,
    "ord": ord, "pow": pow, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "type": type, "zip": zip, "print": print,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
    "AttributeError": AttributeError, "StopIteration": StopIteration,
    "ArithmeticError": ArithmeticError, "LookupError": LookupError,
    "NameError": NameError, "NotImplementedError": NotImplementedError,
}

# Worker 空闲超过该秒数后，下次获取时回收重建（避免长期占用进程资源）
_WORKER_IDLE_SECONDS = 15 * 60

# 每次调用在脚本超时之外额外等待的缓冲（用于进程间通信与网络收尾）
_WORKER_EXTRA_SECONDS = 5

# 初始化握手超时（模块级代码若做网络请求，应自带 timeout）
_WORKER_INIT_TIMEOUT = 60


# ════════════════════════════════════════════
#  AST 校验
# ════════════════════════════════════════════

class ScriptSecurityError(Exception):
    """脚本未通过安全校验"""


def _check_attr(node: ast.Attribute) -> None:
    if node.attr in _BLOCKED_ATTRS:
        raise ScriptSecurityError(f"禁止访问属性: {node.attr}")


def validate_script(source: str, extra_modules: Optional[Any] = None) -> None:
    """对脚本源码做静态安全检查。

    校验内容：
    1. 必须是合法 Python 语法。
    2. AST 节点类型在白名单内（禁止类定义、异步、yield 等）。
    3. import / from ... import 默认放行，仅拦危险模块黑名单（_BLOCKED_MODULES）。
    4. 禁止调用危险函数（eval/exec/open 等）。
    5. 禁止访问危险属性（__class__/__subclasses__ 等）。

    extra_modules: 历史兼容参数（白名单时代的接口自定义追加列表），现已无意义，仅保留签名兼容。
    """

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        raise ScriptSecurityError(f"语法错误: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Expression):
            continue
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ScriptSecurityError(
                f"脚本包含不允许的语法: {type(node).__name__}（第 {getattr(node, 'lineno', '?')} 行）"
            )

        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_MODULES:
                    raise ScriptSecurityError(f"禁止导入模块: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _BLOCKED_MODULES:
                raise ScriptSecurityError(f"禁止导入模块: {node.module}")

        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
                raise ScriptSecurityError(f"禁止调用函数: {node.func.id}()")
            if isinstance(node.func, ast.Attribute):
                _check_attr(node.func)

        elif isinstance(node, ast.Attribute):
            _check_attr(node)

    return None


# ════════════════════════════════════════════
#  脚本辅助工具（注入到脚本命名空间）
# ════════════════════════════════════════════

class _HttpResponse:
    """脚本内 HTTP 返回的简化响应对象"""

    def __init__(self, resp: requests.Response):
        self.status_code = resp.status_code
        self.text = resp.text
        self.content = resp.content
        self.headers = dict(resp.headers)
        self.url = resp.url
        self._json: Any = None
        self._json_parsed = False

    def json(self) -> Any:
        if not self._json_parsed:
            try:
                self._json = json.loads(self.text)
            except Exception:
                self._json = None
            self._json_parsed = True
        return self._json


def _http_get(url: str, params: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None, timeout: int = 30):
    resp = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
    return _HttpResponse(resp)


def _http_post(url: str, data: Optional[Any] = None, json_body: Optional[Any] = None,
               headers: Optional[Dict[str, str]] = None, timeout: int = 30):
    kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout, "verify": False}
    if json_body is not None:
        kwargs["json"] = json_body
    elif data is not None:
        kwargs["data"] = data
    resp = requests.post(url, **kwargs)
    return _HttpResponse(resp)


def _md5(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.md5(s).hexdigest()  # noqa: S324


def _sha1(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha1(s).hexdigest()  # noqa: S324


def _base64_encode(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode("ascii")


def _base64_decode(s: str) -> str:
    return base64.b64decode(s).decode("utf-8", errors="replace")


def _url_encode(s: str) -> str:
    return urllib.parse.quote(s)


def _url_decode(s: str) -> str:
    return urllib.parse.unquote(s)


def _timestamp() -> int:
    return int(time.time())


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _get_nested(data: Any, path: str) -> Any:
    """按 'a.b.0.c' 路径取值"""
    if not path:
        return None
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _build_script_globals() -> Dict[str, Any]:
    """构造脚本运行时的安全全局命名空间"""
    g = dict(_SAFE_BUILTINS)

    # 常用标准库模块（无需 import 即可使用）
    g["json"] = json
    g["re"] = re
    g["hashlib"] = hashlib
    g["base64"] = base64
    g["time"] = time
    g["datetime"] = _dt
    g["urllib"] = urllib
    g["hmac"] = hmac
    g["random"] = random
    g["string"] = string
    g["math"] = math
    g["binascii"] = binascii
    g["html"] = html
    g["collections"] = collections
    g["itertools"] = itertools
    g["functools"] = functools

    # 网络请求：requests 已直接可用（等价于 import requests，但无需写 import）
    g["requests"] = requests

    # 工具函数
    g["http_get"] = _http_get
    g["http_post"] = _http_post
    g["md5"] = _md5
    g["sha1"] = _sha1
    g["base64_encode"] = _base64_encode
    g["base64_decode"] = _base64_decode
    g["url_encode"] = _url_encode
    g["url_decode"] = _url_decode
    g["timestamp"] = _timestamp
    g["timestamp_ms"] = _timestamp_ms
    g["get_nested"] = _get_nested

    return g


def _safe_serialize(val: Any) -> Any:
    """把脚本返回值序列化为可跨进程传输的对象"""
    try:
        return json.loads(json.dumps(val, ensure_ascii=False, default=str))
    except Exception:
        return str(val)


# ════════════════════════════════════════════
#  常驻 Worker（单进程，加载脚本一次后反复调用）
# ════════════════════════════════════════════

def _worker_main(conn, source: str) -> None:
    """子进程入口：加载脚本，循环处理父进程下发的函数调用。"""
    g = _build_script_globals()
    try:
        exec(compile(source, "<script>", "exec"), g)  # noqa: S102
    except Exception as e:
        try:
            conn.send({"_init_error": f"{type(e).__name__}: {e}"})
        except (BrokenPipeError, OSError):
            pass
        return

    # 初始化成功，回执握手
    try:
        conn.send({"_init_ok": True})
    except (BrokenPipeError, OSError):
        return

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            break
        except (BrokenPipeError, OSError):
            break
        if msg is None:
            break  # 关闭信号

        cid = msg.get("id")
        fn = msg.get("fn")
        args = msg.get("args") or ()
        try:
            func = g.get(fn)
            if not callable(func):
                raise RuntimeError(
                    f"脚本未定义主函数: parse（请像参考项目一样定义 def parse(params): 函数）"
                )
            val = func(*args)
            result = {"id": cid, "ok": True, "value": _safe_serialize(val)}
        except Exception as e:
            result = {"id": cid, "ok": False, "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}"}
        try:
            conn.send(result)
        except (BrokenPipeError, OSError):
            break


class ScriptWorker:
    """单个常驻子进程（含一条双向管道）。"""

    def __init__(self, source: str, timeout: int):
        self.source = source
        self.timeout = timeout
        self._lock = threading.Lock()
        self._parent_conn: Any = None
        self._process: Optional[multiprocessing.Process] = None
        self._init_error: Optional[str] = None
        self._last_used = time.time()
        self._start()

    def _start(self):
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        process = ctx.Process(
            target=_worker_main,
            args=(child_conn, self.source),
            daemon=True,
        )
        process.start()
        self._parent_conn = parent_conn
        self._process = process
        self._init_error = None

        # 等待初始化握手
        if not parent_conn.poll(_WORKER_INIT_TIMEOUT):
            self._kill()
            raise RuntimeError("脚本初始化超时（模块级代码可能阻塞，请检查 import / 网络请求是否带 timeout）")
        resp = parent_conn.recv()
        if resp.get("_init_error"):
            self._init_error = resp["_init_error"]
            self._kill()
            raise ScriptSecurityError(self._init_error)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def ensure_alive(self) -> bool:
        with self._lock:
            if self._init_error:
                return False
            return self.is_alive()

    def _kill(self):
        try:
            if self._process is not None and self._process.is_alive():
                self._process.terminate()
                self._process.join(2)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(2)
        except Exception:
            pass
        try:
            if self._parent_conn is not None:
                self._parent_conn.close()
        except Exception:
            pass

    def shutdown(self):
        with self._lock:
            try:
                if self._parent_conn is not None:
                    self._parent_conn.send(None)
            except (BrokenPipeError, OSError):
                pass
            self._kill()

    def call(self, function_name: str, args: Tuple[Any, ...], timeout: int) -> Any:
        with self._lock:
            if self._init_error:
                raise ScriptSecurityError(self._init_error)
            if not self.is_alive():
                raise RuntimeError("脚本进程已失效，请重试")

            self._last_used = time.time()
            cid = uuid.uuid4().hex
            self._parent_conn.send({"id": cid, "fn": function_name, "args": args})

            wait = max(1, int(timeout)) + _WORKER_EXTRA_SECONDS
            if not self._parent_conn.poll(wait):
                self._kill()
                raise TimeoutError(f"脚本执行超时（{timeout} 秒）")

            resp = self._parent_conn.recv()

        if resp.get("id") != cid:
            raise RuntimeError("脚本返回结果与请求不匹配")
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "脚本执行失败")
        return resp.get("value")


class ScriptWorkerPool:
    """常驻 Worker 池：同一脚本源复用若干进程，支持并发调用与空闲回收。"""

    def __init__(self, source: str, timeout: int, max_workers: int = 4):
        self.source = source
        self.timeout = timeout
        self.max_workers = max(max_workers, 1)
        self._slots: List[List[Any]] = []  # [[ScriptWorker, Lock], ...]
        self._create_lock = threading.Lock()

    def _acquire(self) -> List[Any]:
        while True:
            with self._create_lock:
                now = time.time()
                for slot in self._slots:
                    wk: ScriptWorker = slot[0]
                    lk: threading.Lock = slot[1]
                    if lk.acquire(False):
                        if wk.ensure_alive() and (now - wk._last_used) < _WORKER_IDLE_SECONDS:
                            return slot
                        # worker 失效或空闲过久：回收并重建
                        try:
                            wk.shutdown()
                        except Exception:
                            pass
                        try:
                            slot[0] = ScriptWorker(self.source, self.timeout)
                            return slot
                        except Exception:
                            # 重建失败（脚本初始化报错）：移除坏槽位并向上抛出，避免死循环
                            try:
                                self._slots.remove(slot)
                            except ValueError:
                                pass
                            lk.release()
                            raise
                if len(self._slots) < self.max_workers:
                    wk = ScriptWorker(self.source, self.timeout)
                    lk = threading.Lock()
                    lk.acquire()
                    self._slots.append([wk, lk])
                    return self._slots[-1]
            # 已达上限：等待某个槽位释放
            time.sleep(0.02)

    def call(self, function_name: str, args: Tuple[Any, ...], timeout: Optional[int] = None) -> Any:
        slot = self._acquire()
        wk: ScriptWorker = slot[0]
        lk: threading.Lock = slot[1]
        try:
            return wk.call(function_name, args, timeout or self.timeout)
        finally:
            lk.release()

    def shutdown(self):
        with self._create_lock:
            for slot in self._slots:
                try:
                    slot[0].shutdown()
                except Exception:
                    pass
            self._slots.clear()


# ════════════════════════════════════════════
#  接口封装
# ════════════════════════════════════════════

_POOL_CACHE: Dict[Tuple[str, int], ScriptWorkerPool] = {}
_POOL_CACHE_LOCK = threading.Lock()


def _pool_key(source: str, timeout: int, extra_modules: Optional[Any] = None) -> Tuple[str, int]:
    # extra_modules 已弃用（黑名单导入模式后不再影响运行环境），不参与 key，避免无意义分裂池缓存
    return (hashlib.sha256(source.encode("utf-8")).hexdigest(), int(timeout))


class ScriptInterface:
    """对脚本的 ``def parse(params):`` 进行统一封装（与参考项目 py 的脚本契约一致）。

    约定（完整请求脚本 / full 形式，脚本自己发请求，无需引擎代发）：
    - search   : params = {keyword, encoded_keyword, timestamp, timestamp_sec, page}
                 返回 书籍列表 list[dict]，每项必含 id、bookTitle
    - chapters : params = {bookId, page, page0, size, count, timestamp, timestamp_sec, **自定义字段}
                 返回 章节列表 list[dict]，每项必含 chapter_id、title
    - audio    : params = {bookId, chapterId, trackId, rid, timestamp, timestamp_sec, **自定义字段}
                 返回 音频直链字符串（http/https 开头）或 {url: ...} 字典

    transient=True 时创建独立、用完即销毁的 Worker 池（用于在线调试），
    生产调用默认复用按 (源码, 超时) 缓存的常驻池，脚本只加载一次。
    """

    def __init__(self, source: str, script_type: str, timeout: int = DEFAULT_TIMEOUT,
                 transient: bool = False, extra_modules: Optional[Any] = None):
        if script_type not in ("search", "chapters", "audio"):
            raise ValueError(f"未知的 script_type: {script_type}")
        self.source = source
        self.script_type = script_type
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        self.transient = transient
        self.extra_modules = list(extra_modules or [])
        validate_script(source, self.extra_modules)  # 静态安全校验（快速失败，拦掉 import os 等危险导入）
        if transient:
            self._pool = ScriptWorkerPool(source, self.timeout)
        else:
            key = _pool_key(source, self.timeout, self.extra_modules)
            with _POOL_CACHE_LOCK:
                pool = _POOL_CACHE.get(key)
                if pool is None:
                    pool = ScriptWorkerPool(source, self.timeout)
                    _POOL_CACHE[key] = pool
                self._pool = pool

    def _call(self, function_name: str, args: Tuple[Any, ...]) -> Any:
        return self._pool.call(function_name, args, self.timeout)

    def execute(self, data: Any) -> Any:
        """调用脚本的 ``parse(data)``，并按 script_type 校验返回结构。"""
        value = self._call("parse", (data,))
        self._validate_return(value, self.script_type)
        return value

    @staticmethod
    def _validate_return(value: Any, script_type: str) -> None:
        """按脚本类型校验返回值结构（与参考项目 py 的 _validate_return_format 一致）。"""
        if script_type == "search":
            if not isinstance(value, list):
                raise RuntimeError("搜索脚本必须返回列表（list[dict]）")
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    raise RuntimeError(f"搜索结果第 {i + 1} 项必须是字典")
                if "id" not in item:
                    raise RuntimeError(f"搜索结果第 {i + 1} 项缺少必填字段: id")
                if "bookTitle" not in item:
                    raise RuntimeError(f"搜索结果第 {i + 1} 项缺少必填字段: bookTitle")
        elif script_type == "chapters":
            if not isinstance(value, list):
                raise RuntimeError("章节脚本必须返回列表（list[dict]）")
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    raise RuntimeError(f"章节结果第 {i + 1} 项必须是字典")
                if "chapter_id" not in item:
                    raise RuntimeError(f"章节结果第 {i + 1} 项缺少必填字段: chapter_id")
                if "title" not in item:
                    raise RuntimeError(f"章节结果第 {i + 1} 项缺少必填字段: title")
        elif script_type == "audio":
            if value is None:
                raise RuntimeError("音频脚本返回值不能为空")
            if isinstance(value, dict):
                url = value.get("url") or value.get("audio_url") or ""
                if not isinstance(url, str) or not url.strip():
                    raise RuntimeError("音频脚本返回的字典中 url 不能为空")
                if not url.startswith(("http://", "https://")):
                    raise RuntimeError("音频 URL 必须以 http:// 或 https:// 开头")
            elif isinstance(value, str):
                if not value.strip():
                    raise RuntimeError("音频 URL 不能为空")
                if not value.startswith(("http://", "https://")):
                    raise RuntimeError("音频 URL 必须以 http:// 或 https:// 开头")
            else:
                raise RuntimeError("音频脚本必须返回字符串（URL）或 {'url': ...} 字典")
        else:
            raise RuntimeError(f"未知的脚本类型: {script_type}")

    def shutdown(self):
        """释放底层 worker 池。transient 池直接销毁；生产池从全局缓存摘除并销毁
        （pool 对象本身可自愈重建，残留引用不会崩，但会新建进程——故必须从缓存摘除防泄漏）。"""
        if self._pool is None:
            return
        if self.transient:
            self._pool.shutdown()
        else:
            key = _pool_key(self.source, self.timeout, self.extra_modules)
            with _POOL_CACHE_LOCK:
                pool = _POOL_CACHE.pop(key, None)
            if pool is not None:
                pool.shutdown()
