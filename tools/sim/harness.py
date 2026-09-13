"""仿真驱动：起真服务 + 假插件，跑场景，输出「修复前 / 修复后」对照结论。

用法：
  ./.venv-xz/bin/python sim/harness.py                 # 全跑
  ./.venv-xz/bin/python sim/harness.py S2 S4          # 只跑指定场景
  ./.venv-xz/bin/python sim/harness.py --keep        # 跑完不停服务（便于手玩）
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import urllib.request
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SIM = Path(__file__).resolve().parent
def _find_repo(base):
    """兼容两种放置位置：仓库内 tools/sim/ 与仓库外同级目录。"""
    for cand in (base.parent.parent, base.parent, base):
        if (cand / "app.py").exists():
            return cand
    return base.parent


REPO = _find_repo(SIM)

DATA = SIM / "data"
STATE = SIM / "state"
FAULT = SIM / "fault.json"
LOG = SIM / "server.log"
APP_PORT, PROXY_PORT = 6500, 6600
BASE = f"http://127.0.0.1:{PROXY_PORT}"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SIM))
os.environ["DATA_DIR"] = str(DATA)
os.environ["DOWNLOAD_DIR"] = str(SIM / "dl")
os.environ["DOWNLOAD_SLOT_LOCAL_TTL"] = os.environ.get("SLOT_TTL", "60")   # 场景里加快租约衰减
os.environ["DOWNLOAD_SLOT_SERVER_TTL"] = "30"

from run import FakePlugin, set_fault, start_proxy          # noqa: E402
from db.init_db import init_db                              # noqa: E402
from db.session import SessionLocal                         # noqa: E402
from db.models import Card, Session as CardSession, ApiConfig, LocalTask, CardDownloadLock  # noqa: E402

RUN = os.environ.get("SIM_RUN") or uuid.uuid4().hex[:6]      # 每次跑换后缀，避免与别的 run 串数据
VERBOSE = False


# ════════════════════════════════════════════════════════════════
#  服务与数据
# ════════════════════════════════════════════════════════════════
class Server:
    def __init__(self, env_extra=None):
        self.proc = None
        self.env_extra = env_extra or {}

    def _ready(self, timeout_s=40.0):
        """端口能连 ≠ 应用可用：uvicorn 先 bind、后跑 startup（init_db 建表 / 载入配置）。
        抢跑会撞 DDL（sqlite: table already exists），或把请求打到还没建表的服务上。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/", timeout=1.5) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def start(self):
        # 端口被别的进程占用 ⇒ 报错。否则场景会打到"上一次没关干净的仿真服务"上，
        # 拿到 401 / 串数据这类**假结果**（本轮就被这个坑过一次）。
        # 允许最多 12s 的释放窗口：S3 会 kill -9 后立刻重启，SIGKILL 到端口真正 release 之间有空档。
        deadline = time.time() + 12
        while True:
            with socket.socket() as s:
                s.settimeout(0.5)
                busy = s.connect_ex(("127.0.0.1", APP_PORT)) == 0
            if not busy:
                break
            if time.time() > deadline:
                raise RuntimeError(f"端口 {APP_PORT} 被残留进程占用：先 pkill -f app.py 再跑")
            time.sleep(0.4)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env_extra.items()})
        env["PYTHONUNBUFFERED"] = "1"
        self.logf = open(LOG, "a")
        self.logf.write(f"\n═══ server start {datetime.now()} env={self.env_extra} ═══\n")
        self.logf.flush()
        self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(REPO), env=env,
                                     stdout=self.logf, stderr=subprocess.STDOUT,
                                     preexec_fn=os.setsid)
        if not self._ready():
            raise RuntimeError("服务未能起来（见 server.log）")
        return True

    def _port_busy(self):
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", APP_PORT)) == 0

    def stop(self, graceful=True):
        if not self.proc:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM if graceful else signal.SIGKILL)
            self.proc.wait(timeout=15)
        except Exception:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        # stop() 必须"真的停了"：uvicorn 收到 SIGTERM 后还要排空后台任务，端口可能仍被占
        # 好几秒；S3 这类 kill→restart 场景如果不等 release 就会把新服务挤掉（假 401）。
        deadline = time.time() + 25
        while self._port_busy() and time.time() < deadline:
            time.sleep(0.3)
        if self._port_busy():
            try:    # 兜底：直接找监听进程下刀
                out = subprocess.run(["ss", "-lntp"], capture_output=True, text=True, timeout=5).stdout
                import re as _re
                for line in out.splitlines():
                    if f":{APP_PORT}" in line:
                        for pid in _re.findall(r"pid=(\d+)", line):
                            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
            except Exception:
                pass
            for _ in range(20):
                if not self._port_busy():
                    break
                time.sleep(0.3)
        try:
            self.logf.close()
        except Exception:
            pass
        self.proc = None


def seed(ttl_note=""):
    init_db()
    db = SessionLocal()
    try:
        cards, toks = {}, {}
        for i in range(1, 9):
            code = f"SIM-{RUN}-{i}"
            c = db.query(Card).filter_by(code=code).first()
            if not c:
                c = Card(code=code, status="active")
                db.add(c)
                db.flush()
            cards[i] = c.id
            tok = f"tok-{RUN}-{i}"
            if not db.query(CardSession).filter_by(token=tok).first():
                db.add(CardSession(card_id=c.id, token=tok, is_active=True,
                                   client_type="extension", device_id="", ip="192.168.1.10"))
            toks[i] = tok
        for k, v in (("admin_lan_only", "0"), ("device_binding_enabled", "0"),
                     ("auto_retry_enabled", "0"), ("slot_renew_relaxed", "1"),
                     ("heartbeat_infra_error_mode", "success_degraded")):
            row = db.query(ApiConfig).filter_by(cfg_key=k).first()
            if row:
                row.cfg_value = v
            else:
                db.add(ApiConfig(cfg_key=k, cfg_value=v, category="settings",
                                 updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.commit()
        return cards, toks
    finally:
        db.close()


def cfg_set(key, value):
    db = SessionLocal()
    try:
        row = db.query(ApiConfig).filter_by(cfg_key=key).first()
        if row:
            row.cfg_value = str(value)
        else:
            db.add(ApiConfig(cfg_key=key, cfg_value=str(value), category="settings",
                             updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.commit()
    finally:
        db.close()
    # 服务端有 5s 配置缓存：等它自然过期，保证场景不受旧值影响
    time.sleep(5.4)


def server_tasks():
    db = SessionLocal()
    try:
        rows = db.query(LocalTask).all()
        return {r.task_id: {"status": r.status, "card": r.card_id, "claim_id": r.claim_id,
                             "claim_session": r.claim_session, "progress": r.progress,
                             "error": r.error} for r in rows}
    finally:
        db.close()


def locks():
    db = SessionLocal()
    try:
        rows = db.query(CardDownloadLock).all()
        return {r.card_id: {"task_id": r.task_id, "holder": r.holder_type,
                             "lease_until": str(r.lease_until)} for r in rows}
    finally:
        db.close()


def wipe_tasks():
    db = SessionLocal()
    try:
        db.query(LocalTask).delete()
        db.query(CardDownloadLock).delete()
        db.commit()
    finally:
        db.close()
    shutil.rmtree(STATE, ignore_errors=True)


class ScenTimeout(Exception):
    """场景级超时。必须是独立类型并且**在模块顶层**：`wait_until` 里的 `except Exception`
    会把普通超时吞掉，整轮挂在轮询里出不来（本轮被这个坑咬过一次）。"""


def wait_until(fn, timeout=90, step=0.3, desc=""):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except ScenTimeout:
            raise
        except Exception:
            pass
        time.sleep(step)
    if VERBOSE:
        print(f"    ‼ 等待超时: {desc}")
    return False


def new_plugin(name, token, **kw):
    p = FakePlugin(name, token, BASE, log=([] if not VERBOSE else None),
                   beat_interval=kw.pop("beat_interval", 6.0), **kw)
    p.start()
    return p


# ════════════════════════════════════════════════════════════════
#  场景
# ════════════════════════════════════════════════════════════════
RESULTS = []


def report(scen, ok, detail):
    RESULTS.append((scen, ok, detail))
    print(f"\n{'✅' if ok else '❌'} [{scen}] {detail}")


def S1_baseline(cards, toks):
    """单机单卡正常下载：30 集全程心跳，不许任何提示、不许漏集。"""
    wipe_tasks()
    p = new_plugin("S1", toks[1])
    r = p.push_task("A100", 30)
    assert r["ok"], r
    tid = list(server_tasks())[0]
    c = p.claim(tid)
    assert c.get("ok"), c
    ok = wait_until(lambda: p.progress.get(tid, {}).get("done") == 30, 60, desc="下完 30 集")
    ok = ok and wait_until(lambda: server_tasks().get(tid, {}).get("status") == "done", 20,
                           desc="服务器标 done")
    no_msg = not p.messages
    empty_lock = not locks()
    p.stop.set()
    report("S1 正常下载不被打扰", ok and no_msg and empty_lock,
           f"done={p.progress.get(tid, {}).get('done')}/30 提示={p.messages or '无'} 残留锁={locks()}")


def S2_502(cards, toks):
    """反代 502 窗口（容器重启/nginx 抖）：老插件 vs PR-2.5。"""
    out = {}
    for policy in ("0.7.1", "pr2.5", "pr2.6"):
        wipe_tasks()
        p = new_plugin(f"S2-{policy}", toks[2], policy=policy)
        p.push_task(f"A200{policy}", 60)
        tid = [k for k, v in server_tasks().items() if v["status"] == "pending"][0]
        assert p.claim(tid).get("ok"), "claim 失败"
        # 让心跳发生一次后，注入只打 heartbeat 的 502，持续 40s（跨过 3 个心跳周期）
        time.sleep(3)
        p.refresh_tasks(quiet=True)      # 让 lastServerTasks 变"热"：真实插件都是先拉到列表再断的
        set_fault(mode="http502", path="heartbeat", seconds=40)
        time.sleep(24)
        set_fault(mode="ok", path="", seconds=0)
        time.sleep(16)
        prog = p.progress
        hard = [e for e in p.events if e["kind"] == "claim_lost"]
        p.stop.set()
        out[policy] = {"msg": p.messages, "lost": len(hard),
                       "done": sum(x["done"] for x in prog.values()),
                       "total": sum(x["total"] for x in prog.values())}
    a, b, c = out["0.7.1"], out["pr2.5"], out["pr2.6"]
    old_bad = bool(a["msg"]) or a["lost"] > 0
    # 判据不是"完全不提示"，而是"不许谎报归属"：真联系不上时提示「已暂停，恢复后自动继续」是对的
    false_ownership = [m for m in c["msg"] if "其他设备" in m or "租约失效" in m]
    new_clean = not false_ownership and c["lost"] == 0 and c["done"] == c["total"]
    report("S2 服务器 502 窗口 40s（租约未过期）", old_bad and new_clean,
           f"0.7.1→提示={a['msg'] or '无'}/掐断{a['lost']}次 ; "
           f"PR-2.5(仅状态码)→提示={b['msg'] or '无'}/掐断{b['lost']}次 ; "
           f"已发版 0.7.2→提示={c['msg'] or '无'}/掐断{c['lost']}次，恢复后 {c['done']}/{c['total']} 集")


def S3_refused_then_restart(cards, toks, srv):
    """真把服务进程 kill 掉再拉起（Docker 重启 / NAS 更新窗口），对比 0.7.1 / PR-2.5 / 0.7.2。

    两种列表状态都要跑，因为它们各自对应一条历史缺陷：
      · 热列表（默认）⇒ 拿**陈旧**列表里的 running 猜归属 → 谎报「其他设备正在下载此任务」
      · 冷列表（`S3_COLD=1`）⇒ 列表里啥也没有 → 猜成「服务器侧已完成」→ 只下一半被标成 done

    连接被拒时 0.7.1 走 comm-fail 计数（3 次才动手），但一旦动手就去拉列表：列表也拉不到
    ⇒ 拿陈旧/空列表判断 ⇒ 热列表谎报「其他设备」、冷列表把半本标成 done。两种都必须不出现。
    """
    out = {}
    for policy in ("0.7.1", "pr2.5", "pr2.6"):
        # 注意：服务已由 main() 起好。这里**不能**再 start()（会 bind 失败留下"看起来活着"的
        # 假进程，然后 stop() 把真服务杀掉、端口却被占死）。只有中途 kill 掉之后才 start()。
        wipe_tasks()
        cfg_set("slot_renew_relaxed", "1")
        p = new_plugin(f"S3-{policy}", toks[3], policy=policy)
        p.push_task("A300", 50)
        tid = list(server_tasks())[0]
        assert p.claim(tid).get("ok")
        time.sleep(3)
        before = sum(x["done"] for x in p.progress.values())
        if os.environ.get("S3_COLD") == "1":
            p.last_server_tasks = []   # 冷列表：本次会话从没成功拉到列表 ⇒ 插件据"空列表"猜成「服务器已完成」
        else:
            p.refresh_tasks(quiet=True)  # 热列表（真实常态）：插件据陈旧 running 猜成「其他设备在下」
        srv.stop(graceful=False)                    # kill -9：等价于容器被停
        set_fault(mode="refused", seconds=24)       # 代理也不给响应（连接直接断）
        time.sleep(20)
        mid_msg, mid_lost = list(p.messages), len([e for e in p.events if e["kind"] == "claim_lost"])
        set_fault(mode="ok", seconds=0, path="")
        srv.start()                                 # 容器起来（进程重启，DB 保留）
        cfg_set("slot_renew_relaxed", "1")
        ok_resume = wait_until(lambda: sum(x["done"] for x in p.progress.values()) > before,
                               60, desc="恢复后继续下载")
        time.sleep(6)
        prog = p.progress
        out[policy] = {"msg": mid_msg, "lost": mid_lost, "resumed": ok_resume,
                       "done": sum(x["done"] for x in prog.values()),
                       "total": sum(x["total"] for x in prog.values())}
        p.stop.set()
    def half_done(pol):
        """本地被标成 done 但实际只下了一部分 = 数据完整性事故"""
        return out[pol]["msg"] and "服务器侧完成" in out[pol]["msg"][0] and \
            out[pol]["done"] < out[pol]["total"]
    old_bad = bool(out["0.7.1"]["msg"])
    # pr2.5 只作为"只改状态码判定够不够"的对照观察，不硬断言（服务端演进后它的表现会变）
    print(f"  [对照] pr2.5：谎报={bool(out['pr2.5']['msg'])} 被打回pending={out['pr2.5']['lost']}", flush=True)
    p26_clean = not [m for m in out["pr2.6"]["msg"] if "租约失效" in m or "其他设备" in m] \
        and not half_done("pr2.6") and out["pr2.6"]["done"] == out["pr2.6"]["total"]
    report("S3 服务进程被 kill 后重启 20s", old_bad and p26_clean,
           f"0.7.1→提示={out['0.7.1']['msg'] or '无'} 半本标完成={half_done('0.7.1')}；"
           f"PR-2.5(只改非2xx)→提示={out['pr2.5']['msg'] or '无'} 半本标完成={half_done('pr2.5')}；"
           f"PR-2.6(再加comm-fail不finalize)→提示={out['pr2.6']['msg'] or '无'} "
           f"半本标完成={half_done('pr2.6')} 最终 {out['pr2.6']['done']}/{out['pr2.6']['total']}")


def S4_late_heartbeat(cards, toks):
    """「锁刚过期、sweep 还没跑到」这个窗口 —— 历史上 100% 被误判成租约失效的正是它。

    用直接拨租约的方式造出该状态（不靠 sleep 撞 sweep 的 30s 节拍，时序确定）：
      · slot_renew_relaxed=1（修复后默认）⇒ 心跳原地续命，插件不掐、不弹提示
      · slot_renew_relaxed=0（回退开关关闭 = 0.7.1 行为）⇒ 心跳 409 → 列表里任务还 running
        → 老插件据此谎报「其他设备正在下载此任务」
    """
    out = {}
    for flag in ("1", "0"):
        cfg_set("slot_renew_relaxed", flag)
        wipe_tasks()
        p = new_plugin(f"S4-{'宽松' if flag == '1' else '严格'}", toks[4], policy="v072")
        p.push_task("A400", 40)
        tid = list(server_tasks())[0]
        assert p.claim(tid).get("ok")
        time.sleep(2)
        before = sum(x["done"] for x in p.progress.values())
        p.refresh_tasks(quiet=True)                # 列表转热（陈旧列表里的 running 就是本机自己）
        # 把「锁 + 任务」两份租约一起拨到刚过期 5s（此刻距下次 sweep 还有 ≥25s，窗口可控）
        db = SessionLocal()
        try:
            past = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5))
            db.query(LocalTask).filter_by(task_id=tid).update({"lease_until": past})
            db.connection().exec_driver_sql(
                "UPDATE card_download_locks SET lease_until = :p WHERE card_id = :c",
                {"p": past.strftime("%Y-%m-%d %H:%M:%S.%f"), "c": [v["card"] for v in server_tasks().values()][0]})
            db.commit()
        finally:
            db.close()
        # 立刻逼一次心跳
        if p.claim_state:
            p.claim_state["lastBeatAt"] = 0
        wait_until(lambda: any(e["kind"] in ("beat_renewed_after_expiry", "claim_lost",
                                             "beat_soft_fail", "beat_degraded") for e in p.events),
                   15, desc="心跳到达")
        time.sleep(2)
        after = sum(x["done"] for x in p.progress.values())
        out[flag] = {"msg": list(p.messages),
                     "lost": len([e for e in p.events if e["kind"] == "claim_lost"]),
                     "kinds": sorted({e["kind"] for e in p.events}),
                     "resumed": after > before}
        p.stop.set()
    cfg_set("slot_renew_relaxed", "1")
    relaxed_ok = not out["1"]["msg"] and out["1"]["lost"] == 0 and out["1"]["resumed"]
    strict_reproduces = bool(out["0"]["msg"]) or out["0"]["lost"] > 0
    report("S4 租约刚过期（sweep 前窗口）", relaxed_ok and strict_reproduces,
           f"修复后(relaxed=1)→提示={out['1']['msg'] or '无'}、继续下载={out['1']['resumed']} ; "
           f"回退开关关(relaxed=0，等价 0.7.1)→提示={out['0']['msg'] or '无'}、掐断 {out['0']['lost']} 次")


def S5_two_devices_same_card(cards, toks):
    """同卡双设备：A 下载中 B 应被挡（正确）；A 掉线后 B 接管；A 回来不得双写。"""
    wipe_tasks()
    a = new_plugin("S5-A", toks[5])
    b = new_plugin("S5-B", toks[5])       # 同一张卡密的第二个会话（同局域网另一台机器）
    a.push_task("A500", 40)
    tid = list(server_tasks())[0]
    assert a.claim(tid).get("ok")
    r_b = b.claim(tid)
    blocked = r_b.get("status") == 409
    time.sleep(4)
    print(f"  [观察] A 掉线前已完成 {sum(x['done'] for x in a.progress.values())} 集", flush=True)
    # A 整机掉线：停止心跳（不 stop 线程，仅让它不再推进）
    a.offline(3600)                                # 设备离线：一个请求都不发
    ttl = int(os.environ["DOWNLOAD_SLOT_LOCAL_TTL"])
    swept = wait_until(lambda: server_tasks().get(tid, {}).get("status") == "pending",
                       ttl + 45, desc="sweep 把离线任务打回 pending")
    r_b2 = b.claim(tid)
    took_over = r_b2.get("ok", False)
    time.sleep(6)
    b_prog = b.progress.get(tid, {})
    # A 回来：它本地的 claim 已失效 ⇒ 心跳被拒；必须停下来而不是与 B 并行落盘
    a._offline_until = 0
    time.sleep(14)
    a_files = set((STATE / "S5-A" / "dl" / tid).glob("*")) if (STATE / "S5-A" / "dl" / tid).exists() else set()
    b_files = set((STATE / "S5-B" / "dl" / tid).glob("*")) if (STATE / "S5-B" / "dl" / tid).exists() else set()
    both_writing = len(a_files & b_files) > 2      # 同一集被两台设备同时落盘 = 槽被破坏
    a_msg = list(a.messages)
    a.stop.set()
    b.stop.set()
    report("S5 同卡双设备互斥", blocked and swept and took_over and not both_writing,
           f"B 首次被挡={blocked}；A 离线后 sweep={swept} 且 B 接管={took_over}"
           f"（B 已下 {b_prog.get('done')}/{b_prog.get('total')}）；A 回来提示={a_msg or '无'}；"
           f"同集双写下架={both_writing}")


def S6_multi_card(cards, toks):
    """多卡密并发（用户诉求②：不同卡密绝不能互相干扰）+ 心跳延迟测量。"""
    wipe_tasks()
    N = 6
    plugins = []
    for i in range(1, N + 1):
        p = new_plugin(f"S6-{i}", toks[i], policy="v072")
        p.push_task(f"B60{i}", 25)
        plugins.append(p)
    time.sleep(1.2)
    for p in plugins:
        assert p.claim().get("ok") or p.claim(list(server_tasks())[0]).get("ok")
    beats = []
    import http.client
    t_end = time.time() + 40
    while time.time() < t_end:
        t0 = time.time()
        c = http.client.HTTPConnection("127.0.0.1", APP_PORT, timeout=10)
        c.request("POST", "/api/extension/tasks/nope/heartbeat",
                  body=json.dumps({"claim_id": "x"}),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
        c.getresponse().read()
        c.close()
        beats.append((time.time() - t0) * 1000)
        time.sleep(0.6)
    fin = {}
    for p in plugins:
        tid = list(p.progress)[0]
        fin[p.name] = p.progress[tid]
    # 每卡密的完成状态互不影响
    sv = server_tasks()
    by_card = {}
    for tid, v in sv.items():
        by_card.setdefault(v["card"], []).append(v["status"])
    all_done = all(s == "done" for lst in by_card.values() for s in lst)
    msgs = {n: p.messages for n, p in zip([x.name for x in plugins], plugins) if p.messages}
    for p in plugins:
        p.stop.set()
    beats.sort()
    p95 = beats[int(len(beats) * 0.95)] if beats else 0
    worst = beats[-1] if beats else 0
    report("S6 多卡密并发互不干扰", all_done and not msgs,
           f"{N} 卡 × 25 集全部 done={all_done}，误报={msgs or '无'}；"
           f"期间打到服务端的请求 p95={p95:.0f}ms 最大={worst:.0f}ms"
           f"{'（写锁排队 → PR-3 待办）' if worst > 1500 else ''}")


def S7_track_cap(cards, toks):
    """>5000 集的书：截断必须让用户看见，不能静默少下。"""
    wipe_tasks()
    p = new_plugin("S7", toks[6])
    n = 5003
    r = p.push_task("A700", n)
    body = r.get("data") or {}
    sv = server_tasks()
    tid = list(sv)[0] if sv else None
    stored = 0
    db = SessionLocal()
    try:
        row = db.query(LocalTask).filter_by(task_id=tid).first()
        stored = len(json.loads(row.tracks or "[]")) if row else 0
    finally:
        db.close()
    told = bool(body.get("truncated")) or "超出" in (body.get("message") or "")
    p.stop.set()
    report("S7 章节数超上限的可见性", stored < n and told and body.get("truncated") == n - stored,
           f"推送 {n} 集 → 落库 {stored} 集；响应明确说明截断={told}"
           f"（truncated={body.get('truncated')}，message={body.get('message')!r}）")


def S8_repush(cards, toks):
    """同专辑重复推送：曲目不同必须新建任务；完全相同才幂等吞并。"""
    wipe_tasks()
    from run import Api
    api = Api(toks[7], BASE)

    def push(album, tids, fmt="mp3", quality=0):
        return api.call("/api/extension/task", "POST", {
            "source": "third_party", "album_id": album, "album_title": f"卷-{album}",
            "quality": quality, "fmt": fmt,
            "tracks": [{"track_id": t, "episode_num": int(t.split("-")[1]), "title": t}
                       for t in tids]})["data"]

    a = push("A800", [f"V1-{i}" for i in range(1, 11)])            # 第一卷
    push("A800", [f"V2-{i}" for i in range(11, 21)])                 # 第二卷（不同曲目）
    c = push("A800", [f"V1-{i}" for i in range(1, 11)])             # 完全相同 ⇒ 应幂等
    sv = server_tasks()
    eps = {}
    db = SessionLocal()
    try:
        for tid, v in sv.items():
            row = db.query(LocalTask).filter_by(task_id=tid).first()
            eps[tid] = [x.get("track_id") for x in json.loads(row.tracks or "[]")]
    finally:
        db.close()
    n_tasks = len(sv)
    has_v2 = any(any(x.startswith("V2-") for x in ids) for ids in eps.values())
    dup_ok = bool(c.get("duplicated")) and c.get("task_id") == a.get("task_id")
    report("S8 同专辑重复推送（曲目不同必须新建）", n_tasks == 2 and has_v2 and dup_ok,
           f"三次推送 → 服务器任务数 {n_tasks}（期望 2）；第二卷被建出来={has_v2}；"
           f"完全相同被幂等={dup_ok}")


def S9_format_quality(cards, toks):
    """同曲目换格式/换音质：换格式必须新建任务；换音质要给可解释的说明（不能悄悄返回旧任务）。"""
    wipe_tasks()
    from run import Api
    api = Api(toks[8], BASE)
    tids = [f"S9-{i}" for i in range(1, 6)]
    tracks = [{"track_id": t, "episode_num": i + 1, "title": t} for i, t in enumerate(tids)]
    base = {"source": "third_party", "album_id": "A900", "album_title": "格式测试",
            "tracks": tracks}
    r1 = api.call("/api/extension/task", "POST", {**base, "quality": 0, "fmt": "mp3"})["data"]
    r2 = api.call("/api/extension/task", "POST", {**base, "quality": 0, "fmt": "m4a"})["data"]
    r3 = api.call("/api/extension/task", "POST", {**base, "quality": 2, "fmt": "mp3"})["data"]
    sv = server_tasks()
    fmt_new = r2.get("task_id") != r1.get("task_id") and not r2.get("duplicated")
    q_explained = bool(r3.get("quality_diff")) and "音质" in (r3.get("message") or "")
    report("S9 换格式/换音质的去重", fmt_new and q_explained,
           f"换 m4a → 新建任务={fmt_new}（{len(sv)} 个任务）；换音质 → 明确解释="
           f"{q_explained}（duplicated={bool(r3.get('duplicated'))}, msg={r3.get('message')!r}）")


SCENARIOS_ALL = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scen", nargs="*")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    globals()["VERBOSE"] = args.verbose

    DATA.mkdir(parents=True, exist_ok=True)
    set_fault(mode="ok", seconds=0, path="")
    print(f"═══ 仿真环境启动（TTL={os.environ['DOWNLOAD_SLOT_LOCAL_TTL']}s）═══")
    srv = Server()
    srv.start()
    start_proxy()
    cards, toks = seed()
    from run import verify_against_plugin
    print(f"✅ 服务 :{APP_PORT} / 代理 :{PROXY_PORT} / 卡密 8 张 (run={RUN})")
    print("   " + verify_against_plugin())

    all_scen = {
        "S1": lambda: S1_baseline(cards, toks),
        "S2": lambda: S2_502(cards, toks),
        "S3": lambda: S3_refused_then_restart(cards, toks, srv),
        "S4": lambda: S4_late_heartbeat(cards, toks),
        "S5": lambda: S5_two_devices_same_card(cards, toks),
        "S6": lambda: S6_multi_card(cards, toks),
        "S7": lambda: S7_track_cap(cards, toks),
        "S8": lambda: S8_repush(cards, toks),
        "S9": lambda: S9_format_quality(cards, toks),
    }
    names = args.scen or list(all_scen)
    # 每个场景一个 SIGALRM 上限，避免某个 wait 卡死整轮（假插件/服务任一环节出问题都能被截断成 FAIL）
    import signal

    def _to(signum, frame):
        raise ScenTimeout("场景超时")
    signal.signal(signal.SIGALRM, _to)
    try:
        for n in names:
            print(f"\n──── {n} ────", flush=True)
            try:
                signal.alarm(int(os.environ.get("SCEN_TIMEOUT", "240")))
                all_scen[n]()
                signal.alarm(0)
            except Exception as e:
                import traceback
                traceback.print_exc()
                report(n, False, f"场景执行异常: {type(e).__name__}: {e}")
            finally:
                signal.alarm(0)
    finally:
        if not args.keep:
            srv.stop()
        else:
            print(f"\n(服务保留：:  {APP_PORT}，Ctrl-C 结束)")
            try:
                while True:
                    time.sleep(5)
            except KeyboardInterrupt:
                srv.stop()
    print("\n══════ 汇总 ══════", flush=True)
    for s, ok, d in RESULTS:
        print(f"{'✅' if ok else '❌'} {s}: {d}")


main()
