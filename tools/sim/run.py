"""下载租约 / 多设备误报的端到端仿真环境。

真进程 + 真 HTTP + 真 SQLite：
  uvicorn(app.py) :6500  ←  FaultProxy :6600（故障注入：502 / 断连 / 注入延迟）  ←  FakePlugin × N

FakePlugin 按扩展 0.7.1 的 background.js 逐条复刻策略（不是"理想客户端"）：
  · 心跳节流 30s；网络异常 → beatFails++，累计 3 次才 handleClaimLost
  · **收到任何非 2xx（含 502/504/500）⇒ 立刻 handleClaimLost**（0.7.1 的真实行为，一次就掐）
  · handleClaimLost：清 claim → 拉一次 /tasks（拉失败就用陈旧的 lastServerTasks）→
      服务器 pending → 本地回 pending
      服务器 running → frozen + 「其他设备正在下载此任务」
      列表里没有该任务 → status=done + 「下载中断（租约失效），任务可能已由服务器侧完成」
  · 下载：每集写一个文件（分 4 段写，取消会留下半截文件），本地"扫描已存在即跳过"
policy 变体用于对比：
  · 0.7.1  = 现状线上插件
  · pr2.5  = 只把「409 且 code=lost（或老服务端无 code 的 409）」当硬失效；5xx/超时按 comm-fail
  · v072   = 在 pr2.5 之上：requeued ⇒ 立即重 claim；frozen 判定用 mine；半本不 finalize

用法：./.venv-xz/bin/python sim/run.py --scenario S2 --verbose
"""
import argparse
import json
import os
import shutil
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import http.client

SIM = Path(__file__).resolve().parent
def _find_repo(base):
    """兼容两种放置位置：仓库内 tools/sim/ 与仓库外同级目录。"""
    for cand in (base.parent.parent, base.parent, base):
        if (cand / "app.py").exists():
            return cand
    return base.parent


REPO = _find_repo(SIM)

DATA = SIM / "data"
DLDIR = SIM / "dl"
STATE = SIM / "state"
FAULT = SIM / "fault.json"
APP_PORT, PROXY_PORT = 6500, 6600


# ════════════════════════════════════════════════════════════════
#  故障注入代理
# ════════════════════════════════════════════════════════════════
def set_fault(**kw):
    """fault: mode=ok|http502|refused|delay, path=子串匹配, seconds=持续, latency_ms"""
    cur = {"mode": "ok", "path": "", "seconds": 0, "latency_ms": 0, "until": 0}
    if FAULT.exists():
        cur.update(json.loads(FAULT.read_text() or "{}"))
    cur.update(kw)
    if cur.get("seconds"):
        cur["until"] = time.time() + cur["seconds"]
    elif "mode" in kw:
        cur["until"] = 0
    FAULT.write_text(json.dumps(cur))
    return cur


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # 静音
        pass

    def _fault_active(self, path):
        try:
            f = json.loads(FAULT.read_text() or "{}")
        except Exception:
            return {"mode": "ok"}
        if f.get("until") and time.time() > f["until"]:
            set_fault(mode="ok", seconds=0, until=0)
            return {"mode": "ok"}
        if f.get("path") and f["path"] not in path:
            return {"mode": "ok"}
        return f

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        f = self._fault_active(self.path)
        mode = f.get("mode", "ok")
        if mode == "http502":                       # 反代拿到上游失败时给的样子（HTML + 非 JSON）
            payload = b"<html><head><title>502 Bad Gateway</title></head><body>502</body></html>"
            self.send_response(502)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if mode == "refused":                       # 上游进程没了：连接被拒 → 客户端 fetch 抛异常
            self.close_connection = True
            try:
                self.wfile.close()
            except Exception:
                pass
            return
        if mode == "hang":                          # 上游 hang 住：nginx 60s 后 504，这里模拟"一直不回"
            time.sleep(min(120, int(f.get("seconds") or 30)))
            payload = b"<html>504 Gateway Time-out</html>"
            self.send_response(504)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if f.get("latency_ms"):
            time.sleep(int(f["latency_ms"]) / 1000.0)
        conn = http.client.HTTPConnection("127.0.0.1", APP_PORT, timeout=90)
        try:
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "connection")}
            hdrs["Host"] = f"127.0.0.1:{APP_PORT}"
            conn.request(self.command, self.path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            payload = f'{{"success":false,"error":"proxy error: {e}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def start_proxy():
    srv = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), _ProxyHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ════════════════════════════════════════════════════════════════
#  假插件（复刻 background.js 的 0.7.1 策略）
# ════════════════════════════════════════════════════════════════
# ── PR-2.6 的决策表：必须与 extension/background.js 的 classifyBeat 完全一致 ──
# 假插件是"复刻"而不是"执行"插件代码，所以这张表就是两者的契约；harness 启动时用 node
# 跑一遍真实 JS（verify_against_plugin），任何一侧改了而另一侧没跟上就会炸。
# decideClaimLost 的决策表（与 background.js 同名纯函数逐条比对，见 verify_against_plugin）
LOST_TABLE = [
    ({"listFresh": False, "localStatus": "running", "serverStatus": "unknown"}, "pause-unknown"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "running"}, "freeze-other-device"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "pending"}, "resume-pending"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "gone"}, "finalize-server-done"),
    ({"listFresh": False, "localStatus": "done", "serverStatus": "unknown"}, "keep-terminal"),
    ({"listFresh": True, "localStatus": "cancelled", "serverStatus": "running"}, "keep-terminal"),
]

BEAT_TABLE = [
    ({"ok": True, "status": 200, "data": {"success": True}}, "renewed"),
    ({"ok": True, "status": 200, "data": {"success": True, "degraded": True}}, "renewed"),
    ({"ok": True, "status": 200, "data": None}, "soft-lost"),
    ({"ok": False, "status": 401, "data": {"detail": "x"}}, "relogin"),
    ({"ok": False, "status": 409, "data": {"error": "租约已失效"}}, "hard-lost"),
    ({"ok": False, "status": 409, "data": {"code": "lost"}}, "hard-lost"),
    ({"ok": False, "status": 409, "data": {"code": "requeued"}}, "requeue"),
    ({"ok": False, "status": 404, "data": {"code": "task_gone"}}, "hard-lost"),
    ({"ok": False, "status": 502, "data": None}, "soft-lost"),
    ({"ok": False, "status": 504, "data": None}, "soft-lost"),
    ({"ok": False, "status": 0, "data": None}, "soft-lost"),
    (None, "soft-lost"),
]


def classify_beat(res):
    if not res:
        return "soft-lost"
    if res.get("status") == 401:
        return "relogin"
    if res.get("ok") and isinstance(res.get("data"), dict) and res["data"].get("success"):
        return "renewed"
    code = (res.get("data") or {}).get("code") if isinstance(res.get("data"), dict) else None
    st = res.get("status")
    if st == 409:
        return "requeue" if code == "requeued" else "hard-lost"
    return "hard-lost" if st == 404 else "soft-lost"


def verify_against_plugin():
    """用 node 执行 background.js 里那份 classifyBeat，逐条比对 BEAT_TABLE。"""
    import re as _re
    import subprocess
    js = REPO / "extension" / "background.js"
    if not js.exists() or not shutil.which("node"):
        return "跳过（缺 background.js 或 node）"
    src = js.read_text(encoding="utf-8")
    m = _re.search(r"^function classifyBeat\(", src, _re.M)
    if not m:
        raise AssertionError("background.js 里没有 classifyBeat()，PR-2.6 逻辑可能被删")
    # 先配平圆括号再找函数体的 {（参数里带解构默认值时，直接找 { 会截错位置）
    k = src.index("(", m.start())
    d = 0
    for q in range(k, len(src)):
        d += (src[q] == "(") - (src[q] == ")")
        if d == 0:
            break
    i, depth = src.index("{", q), 0
    for j in range(i, len(src)):
        depth += (src[j] == "{") - (src[j] == "}")
        if depth == 0:
            fn = src[m.start():j + 1]
            break
    groups = [("classifyBeat", fn, BEAT_TABLE)]
    m2 = _re.search(r"^function decideClaimLost\(", src, _re.M)
    if m2:
        k = src.index("(", m2.start())
        d = 0
        for q in range(k, len(src)):
            d += (src[q] == "(") - (src[q] == ")")
            if d == 0:
                break
        j2 = src.index("{", q)
        d = 0
        for e2 in range(j2, len(src)):
            d += (src[e2] == "{") - (src[e2] == "}")
            if d == 0:
                groups.append(("decideClaimLost", src[m2.start():e2 + 1], LOST_TABLE))
                break
    else:
        raise AssertionError("background.js 里缺 decideClaimLost()（claim 失效处置被改回内联判断？）")
    js_parts = ["const out = [];"]
    for nm, body, table in groups:
        js_parts.append("const %s = new Function('return (' + %s + ')')();" % (nm, json.dumps(body)))
        js_parts.append("const c_%s = %s;" % (nm, json.dumps(table)))
        js_parts.append("for (const [r, w] of c_%s) { const g = %s(r); if (g !== w) out.push('%s:' + w + '!=' + g) }" % (nm, nm, nm))
    js_parts.append("console.log(out.length ? 'MISMATCH:' + out.join(',') : 'MATCH');")
    js_src = "\n".join(js_parts)
    tmp = Path(tempfile.mkstemp(suffix=".js")[1])
    tmp.write_text(js_src, encoding="utf-8")
    script = str(tmp)
    try:
        out = subprocess.run(["node", script], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    tail = (out.stdout or "").strip().splitlines()[-1:] or [""]
    if tail[0] != "MATCH":
        raise AssertionError(f"假插件决策表与插件源码不一致 → {tail[0]} {out.stderr[-300:]}")
    return "假插件判定 == background.js:" + " + ".join(nm for nm, _, _ in groups) + " ✅"


class Api:
    """对应 serverApi()：返回 {ok, status, data}；连接层失败才抛（与 fetch 语义一致）。"""

    def __init__(self, token, base):
        self.token, self.base = token, base

    def call(self, path, method="GET", body=None):
        host, port = self.base.split("//")[1].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=25)
        try:
            data = json.dumps(body) if body is not None else None
            hdrs = {"Authorization": "Bearer " + self.token}
            if data:
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            raw = r.read()
            try:
                parsed = json.loads(raw.decode() or "null")
            except Exception:
                parsed = None          # 502 的 HTML 走这里：data=None，但 status 照实返回
            return {"ok": 200 <= r.status < 300, "status": r.status, "data": parsed}
        finally:
            conn.close()


class FakePlugin(threading.Thread):
    BEAT_INTERVAL = 30.0        # SERVER_BEAT_INTERVAL_MS
    COMM_FAIL_LIMIT = 3         # BEAT_COMM_FAIL_LIMIT
    TRACK_SEC = 0.02            # 每集"下载"耗时（×4 段）

    def __init__(self, name, token, base, *, policy="0.7.1", ttl_hint=300, log=None,
                 track_sec=None, beat_interval=6.0):
        super().__init__(daemon=True)
        self.name, self.policy = name, policy
        self.api = Api(token, base)
        self.ttl_hint = ttl_hint
        self.track_sec = track_sec if track_sec is not None else self.TRACK_SEC
        self.beat_interval = beat_interval
        self._offline_until = 0.0                 # 模拟设备离线：心跳与派发一起停
        self.log = log if log is not None else []
        self.claim_state = None                   # {taskId, claimId, leaseSeconds, lastBeatAt, beatFails}
        self.last_server_tasks = []               # lastServerTasks（陈旧缓存 = 误报来源）
        self.tasks = {}                           # 本地任务表：task_id → {...tracks}
        self.stop = threading.Event()
        self.messages = []                        # 用户可见文案
        self.events = []                          # 结构化事件
        self.inflight = None
        self._lock = threading.RLock()   # 可重入：状态读写会嵌套（progress / complete）
        self.dir = STATE / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ev(f"init policy={policy}")

    # ── 工具 ──
    def ev(self, kind, **kw):
        rec = {"t": round(time.time() % 100000, 3), "dev": self.name, "kind": kind}
        rec.update(kw)
        self.events.append(rec)
        self.log.append(rec)

    def say(self, text):
        if text and (not self.messages or self.messages[-1] != text):
            self.messages.append(text)
            self.ev("UI_MESSAGE", text=text)

    def _file(self, task_id, ep):
        d = self.dir / "dl" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{ep:05d}.mp3"

    # ── 服务端交互 ──
    def refresh_tasks(self, quiet=False):
        try:
            r = self.api.call("/api/extension/tasks")
        except Exception as e:
            if not quiet:
                self.ev("tasks_fetch_fail", err=repr(e))
            return False
        if r["ok"] and isinstance(r["data"], dict) and isinstance(r["data"].get("tasks"), list):
            self.last_server_tasks = r["data"]["tasks"]
            return True
        return False

    def push_task(self, album, n_tracks, start=1):
        """对应网页端「推送到插件」：POST /api/extension/task"""
        tracks = [{"track_id": f"{album}-{i}", "episode_num": start + i - 1,
                   "title": f"第{start + i - 1}集"} for i in range(n_tracks)]
        return self.api.call("/api/extension/task", "POST",
                             {"source": "third_party", "album_id": str(album),
                              "album_title": f"模拟书{album}", "quality": 0,
                              "fmt": "mp3", "tracks": tracks})

    def claim(self, task_id=None):
        path = f"/api/extension/tasks/{task_id}/claim" if task_id else "/api/extension/tasks/claim"
        try:
            r = self.api.call(path, "POST", {})
        except Exception as e:
            return {"error": f"conn: {e}"}
        if r["ok"] and isinstance(r["data"], dict) and r["data"].get("task"):
            c = r["data"]["task"]
            self.claim_state = {"taskId": c["task_id"], "claimId": c["claim_id"],
                                "leaseSeconds": c.get("lease_seconds") or self.ttl_hint,
                                "lastBeatAt": time.time(), "beatFails": 0}
            with self._lock:
                self.tasks[c["task_id"]] = {
                    "task_id": c["task_id"], "album_id": c.get("album_id"),
                    "status": "running", "frozen": False, "errorMsg": "",
                    "tracks": [{"track_id": t["track_id"], "episode": t.get("episode_num"),
                                "status": "pending", "error": ""} for t in c.get("tracks", [])],
                }
            self.ev("claim_ok", task=c["task_id"], n=len(c.get("tracks", [])))
            return {"ok": True, "task": c}
        d = r["data"] or {}
        self.ev("claim_fail", status=r["status"], code=d.get("code"), err=d.get("error"))
        return {"error": d.get("error") or f"http {r['status']}", "status": r["status"],
                "code": d.get("code")}

    def heartbeat(self):
        cs = self.claim_state
        if not cs:
            return
        try:
            r = self.api.call(f"/api/extension/tasks/{cs['taskId']}/heartbeat", "POST",
                              {"claim_id": cs["claimId"]})
        except Exception as e:                      # fetch reject（连接被拒/超时）
            cs["beatFails"] = cs.get("beatFails", 0) + 1
            self.ev("beat_comm_fail", n=cs["beatFails"], err=repr(e))
            if cs["beatFails"] >= self.COMM_FAIL_LIMIT:
                if self.policy in ("pr2.6", "v072"):
                    # 关键差异：不 cancel、不清 claimState、不 finalize —— 只是停下来等
                    self.ev("beat_unreachable_pause_only", n=cs["beatFails"])
                    self._stalled_guard()
                    return
                self.handle_claim_lost("心跳长期不可达，租约已过期")
            return
        if r["status"] == 401:
            self.ev("beat_401")
            self.claim_state = None
            return
        if r["ok"] and isinstance(r["data"], dict) and r["data"].get("success"):
            cs["lastBeatAt"] = time.time()
            cs["beatFails"] = 0
            if r["data"].get("lease_seconds"):
                cs["leaseSeconds"] = r["data"]["lease_seconds"]
            if r["data"].get("degraded"):
                self.ev("beat_degraded")
            if r["data"].get("renewed_after_expiry"):
                self.ev("beat_renewed_after_expiry")
            return
        d = r["data"] or {}
        code = d.get("code")
        hard_lost = r["status"] == 409 and (code == "lost" or (self.policy == "0.7.1" and code is None)
                                           or (self.policy != "0.7.1" and code is None))
        if self.policy == "pr2.6" and not r["ok"] and r["status"] != 409:
            # 非 409（502/504/500/网络层）一律按"联系不上"处理，交给下面的 soft 分支
            cs["beatFails"] = cs.get("beatFails", 0) + 1
            self.ev("beat_soft_fail", status=r["status"], n=cs["beatFails"])
            self._stalled_guard()
            return
        if self.policy == "0.7.1":
            # 现状：任何非 2xx（502/504/500/409）都当 claim 失效 ⇒ 立刻掐
            self.handle_claim_lost(d.get("error") or f"心跳被拒 ({r['status']})")
            return
        # PR-2.5 / PR-2.6 / v0.7.2：只有服务器明确说 lost 才硬停
        if code == "requeued" and self.policy in ("v072", "pr2.6"):
            self.ev("beat_requeued")
            self.claim_state = None
            out = self.claim(cs["taskId"])
            if not out.get("ok"):
                self.say("任务已重新排队，稍后自动重试")
            return
        if hard_lost:
            self.handle_claim_lost(d.get("error") or f"心跳被拒 ({r['status']})")
            return
        cs["beatFails"] = cs.get("beatFails", 0) + 1
        self.ev("beat_soft_fail", status=r["status"], code=code, n=cs["beatFails"])
        # 软失效：不 cancel、不清 claimState；按剩余租约决定是否继续
        left = cs["lastBeatAt"] + cs["leaseSeconds"] - time.time()
        if left <= 0 and cs["beatFails"] >= self.COMM_FAIL_LIMIT:
            self.say("无法联系服务器，已暂停派发新章节（保留已下载部分，等待恢复）")
            self.ev("beat_pause_dispatch_only")

    def complete(self, task_id):
        with self._lock:
            t = self.tasks.get(task_id) or {}
            tracks = list(t.get("tracks", []))
        failed = [{"episode": x["episode"], "title": x["track_id"], "error": x["error"]}
                  for x in tracks if x["status"] == "failed"]
        done = sum(1 for x in tracks if x["status"] == "done")
        cs = self.claim_state or {}
        r = self.api.call(f"/api/extension/tasks/{task_id}/complete", "POST",
                          {"claim_id": cs.get("claimId"),
                           "progress": {"total": len(tracks), "done": done, "failed": len(failed)},
                           "failed_list": failed, "error": ""})
        self.ev("complete", status=r["status"], body=(r["data"] or {}).get("code"))
        self.claim_state = None
        with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id]["status"] = "done"
        return r

    def cancel(self, task_id):
        cs = self.claim_state or {}
        r = self.api.call(f"/api/extension/tasks/{task_id}/cancel", "POST",
                          {"claim_id": cs.get("claimId")})
        self.ev("cancel_api", status=r["status"])
        self.claim_state = None
        with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id]["status"] = "cancelled"
        return r

    def handle_claim_lost(self, reason):
        """background.js:478-517 逐条复刻（含「拉列表失败就用陈旧列表」这个关键缺陷）"""
        if not self.claim_state:
            return
        task_id = self.claim_state["taskId"]
        self.claim_state = None
        fetched = self.refresh_tasks(quiet=True)
        self.ev("claim_lost", reason=reason, list_fresh=fetched,
                n_list=len(self.last_server_tasks))
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            for tr in task["tracks"]:
                if tr["status"] == "downloading":
                    tr["status"] = "pending"
                    tr["error"] = "已取消"          # ← 0.7.1 把用户/系统取消记进"已取消"
            pend = [t["task_id"] for t in self.last_server_tasks if t.get("status") == "pending"]
            run = [t["task_id"] for t in self.last_server_tasks if t.get("status") == "running"]
            if task["status"] in ("done", "cancelled"):
                pass
            elif not fetched and self.policy in ("pr2.6", "v072"):
                # 列表不可信 ⇒ 任何基于列表的结论都不下（既不猜「其他设备」也不猜「已完成」）。
                # ⚠️ 顺序关键：这条必须在 pend/run **之前**，否则陈旧列表里那条 running（往往是
                #    本机自己下的那本）会先命中，误报就还在。
                task["frozen"] = False
                task["errorMsg"] = "无法联系服务器，已暂停（下载内容未受影响，恢复后自动继续）"
                self.say(task["errorMsg"])
                self.ev("keep_state_no_finalize", done=sum(1 for x in task["tracks"] if x["status"] == "done"))
            elif task_id in pend:
                task["status"] = "pending"
                task["frozen"] = False
                task["errorMsg"] = ""
            elif task_id in run:
                task["status"] = "running"
                task["frozen"] = True
                task["errorMsg"] = "其他设备正在下载此任务"
            else:
                # 列表里没有 ⇒ 「服务器侧已完成」——半本也会被标成 done（历史缺陷）
                task["status"] = "done"
                task["frozen"] = False
                task["errorMsg"] = task["errorMsg"] or "下载中断（租约失效），任务可能已由服务器侧完成"
            self.say(task["errorMsg"])
            self.ev("local_state", status=task["status"], frozen=task["frozen"],
                    pending_tracks=sum(1 for x in task["tracks"] if x["status"] == "pending"))

    # ── 主循环 ──
    def run(self):
        while not self.stop.is_set():
            now = time.time()
            if now < self._offline_until:          # 离线窗口：一个请求都不发（真实断网/合盖就是这样）
                time.sleep(0.2)
                continue
            cs = self.claim_state
            if cs and now - cs.get("lastBeatAt", 0) >= self.beat_interval:
                self.heartbeat()
            self._dispatch_once()
            time.sleep(0.05)

    def _stalled_guard(self):
        """联系不上服务器时：暂停派发，但保留 claimState 与已完成状态（恢复后自动继续）。"""
        with self._lock:
            if self.claim_state and self.tasks.get(self.claim_state["taskId"]):
                t = self.tasks[self.claim_state["taskId"]]
                if not t.get("stalled"):
                    t["stalled"] = True
                    self.say("无法联系服务器，已暂停新章节派发（已下载部分保留，恢复后自动继续）")
                    self.ev("stalled")

    def _dispatch_once(self):
        if self.inflight:
            self._pump(self.inflight)
            return
        if time.time() < self._offline_until:
            return
        if self.inflight is None and self.claim_state:
            t = self.tasks.get(self.claim_state["taskId"])
            if t is not None and t.get("stalled"):
                try:                    # 探活：只有服务器确实回了才恢复派发
                    alive = self.api.call(
                        f"/api/extension/tasks/{self.claim_state['taskId']}/heartbeat", "POST",
                        {"claim_id": self.claim_state["claimId"]}).get("ok")
                except Exception:
                    alive = False
                if alive:
                    t["stalled"] = False
                    self.claim_state["beatFails"] = 0
                    self.claim_state["lastBeatAt"] = time.time()
                    self.ev("unstalled")
                    self.say("已重新连上服务器，继续下载")
                return
        with self._lock:
            for tid, task in self.tasks.items():
                if task["status"] != "running" or task.get("frozen"):
                    continue
                if not self.claim_state or self.claim_state["taskId"] != tid:
                    continue
                nxt = next((x for x in task["tracks"] if x["status"] == "pending"), None)
                if nxt is not None:
                    nxt["status"] = "downloading"
                    self.inflight = (tid, nxt["track_id"], nxt["episode"], 0)
                    self._cur_file = self._file(tid, nxt["episode"])
                    if self._cur_file.exists():          # 本地已有 ⇒ 跳过（真实插件行为）
                        nxt["status"] = "done"
                        nxt["error"] = ""
                        self.inflight = None
                        self.ev("skip_existing", ep=nxt["episode"])
                    break
        if self.inflight is None:
            self._maybe_finish()


    def _pump(self, ref):
        """一段一段写文件：取消/断电时会留下半截文件（真实世界的"部分文件"）"""
        tid, track_id, ep, step = ref
        f = self._cur_file
        try:
            with open(f, "a") as fh:
                fh.write("AUDIODATA" * 8)
                fh.flush()
        except Exception as e:
            self.ev("write_fail", err=repr(e))
        time.sleep(self.track_sec)
        step += 1
        if step >= 4:
            with self._lock:
                t = self.tasks.get(tid)
                if t:
                    x = next((y for y in t["tracks"] if y["track_id"] == track_id), None)
                    if x is not None:
                        # 取消可能发生在写最后一段之前：文件已完整则 done
                        x["status"] = "done" if f.stat().st_size >= 4 * 72 else "pending"
            self.inflight = None
            self._maybe_finish()
        else:
            self.inflight = (tid, track_id, ep, step)

    def _maybe_finish(self):
        """全部集落定 → 上报 complete。HTTP 必须在锁外发（否则与 progress/claim 互锁死锁）。"""
        ready = []
        with self._lock:
            for tid, task in list(self.tasks.items()):
                if task["status"] != "running" or task.get("frozen"):
                    continue
                if self.claim_state and self.claim_state["taskId"] == tid and \
                        not any(x["status"] in ("pending", "downloading") for x in task["tracks"]):
                    ready.append(tid)
        for tid in ready:
            with self._lock:
                tr = self.tasks[tid]["tracks"]
            self.ev("all_tracks_terminal", task=tid,
                    done=sum(1 for x in tr if x["status"] == "done"),
                    failed=sum(1 for x in tr if x["status"] == "failed"))
            self.complete(tid)

    def offline(self, seconds):
        """模拟「合盖 / 关浏览器 / 断网」：心跳与派发一起停 seconds 秒"""
        self._offline_until = time.time() + seconds
        self.ev("offline", seconds=seconds)

    @property
    def progress(self):
        with self._lock:
            out = {}
            for tid, t in self.tasks.items():
                out[tid] = {"status": t["status"], "frozen": t["frozen"], "msg": t["errorMsg"],
                            "done": sum(1 for x in t["tracks"] if x["status"] == "done"),
                            "pending": sum(1 for x in t["tracks"] if x["status"] == "pending"),
                            "failed": sum(1 for x in t["tracks"] if x["status"] == "failed"),
                            "total": len(t["tracks"])}
            return out
