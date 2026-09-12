"""插件心跳判定的回归测试：直接执行 extension/background.js 里的 classifyBeat（不复制逻辑）。

为什么值得单独钉住：用户报的两句提示（「下载中断（租约失效）」/「其他设备正在下载此任务」）
在 0.7.1 里其实是**同一个判定**产生的——心跳只要不是 200 就当 claim 失效。这个纯函数就是
那条分界线，一旦改回去（把 5xx 又当失效、或把 hard-lost 又当可重试），本文件立刻红。

不引 Node 测试框架：用 node 直接跑从源码里抽出来的函数体，保证测的是线上那份代码。
"""
import json
import os
import pathlib
import tempfile
import re
import shutil
import subprocess
import textwrap

import pytest

JS = pathlib.Path(__file__).resolve().parent.parent / "extension" / "background.js"


def _extract_fn(src: str, name: str) -> str:
    """按大括号配对抽出顶层 function 定义（含 async 前缀）。

    ⚠️ 函数体的 `{` 不能直接取"参数串之后的第一个大括号"——参数里有解构默认值
    （`function f(a, { b = 1 } = {}) {}`）时会截到参数的 `}`。必须先配平圆括号。
    """
    m = re.search(rf"^(?:async )?function {name}\(", src, flags=re.M)
    assert m, f"background.js 里找不到 {name}()（改名/删除了？测试需同步更新）"
    i = src.index("(", m.start())
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                break
    else:
        raise AssertionError(f"{name}() 圆括号不配对")
    j = src.index("{", k)
    depth = 0
    for e in range(j, len(src)):
        if src[e] == "{":
            depth += 1
        elif src[e] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():e + 1]
    raise AssertionError(f"{name}() 大括号不配对")


node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="需要 node 才能执行插件源码")


def _run(cases):
    """cases: [(res, 期望分类)]  →  在 node 里逐个断言，返回失败列表"""
    script = textwrap.dedent(f"""
        const src = {json.dumps(JS.read_text(encoding='utf-8'))};
        const body = {json.dumps(_extract_fn(JS.read_text(encoding='utf-8'), 'classifyBeat'))};
        // eslint-disable-next-line no-new-func
        const classifyBeat = new Function('return (' + body + ')')();
        const cases = {json.dumps(cases, ensure_ascii=False)};
        const bad = [];
        for (const [res, want] of cases) {{
          const got = classifyBeat(res);
          if (got !== want) bad.push({{res, want, got}});
        }}
        console.log(JSON.stringify({{bad}}));
    """)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])["bad"]


# ════════════════ 服务器明确说的：才允许中断 ════════════════
def test_server_denied_is_hard_lost():
    bad = _run([
        # 老服务端只回 409 不带 code ⇒ 仍按失效处理（向后兼容，保护不打折）
        ({"ok": False, "status": 409, "data": {"success": False, "error": "租约已失效"}}, "hard-lost"),
        # 新服务端明确 lost（真被别的会话接管）⇒ 必须停
        ({"ok": False, "status": 409, "data": {"code": "lost"}}, "hard-lost"),
        # 任务已被删除 ⇒ 继续下没意义
        ({"ok": False, "status": 404, "data": {"code": "task_gone"}}, "hard-lost"),
    ])
    assert not bad, bad


def test_requeued_means_reclaim_not_stop():
    """服务器说「打回重新排队」⇒ 重 claim 同一本，绝不能 cancel + 冻结一个租约周期。"""
    bad = _run([({"ok": False, "status": 409, "data": {"code": "requeued"}}, "requeue")])
    assert not bad, bad


# ════════════════ 基础设施异常：绝不许当成所有权丢失 ════════════════
def test_infra_failures_are_soft():
    bad = _run([
        # 反代 502：HTML 错误页，data 解析不出来（用户环境里容器重启就是这个形状）
        ({"ok": False, "status": 502, "data": None}, "soft-lost"),
        ({"ok": False, "status": 504, "data": None}, "soft-lost"),
        ({"ok": False, "status": 500, "data": {"detail": "Internal Server Error"}}, "soft-lost"),
        # 连接层失败（serverApi 里 fetch 抛出后被归一化成 status 0）
        ({"ok": False, "status": 0, "data": None, "error": "Failed to fetch"}, "soft-lost"),
        # 参数/校验类错误：重试无益，但更不许 finalize
        ({"ok": False, "status": 400, "data": {"detail": "bad request"}}, "soft-lost"),
        ({"ok": False, "status": 422, "data": None}, "soft-lost"),
        (None, "soft-lost"),
    ])
    assert not bad, bad


def test_success_and_degraded_are_renewed():
    bad = _run([
        ({"ok": True, "status": 200, "data": {"success": True, "lease_until": "2026-09-12T04:00:00"}},
         "renewed"),
        # 服务器读不到下载槽时回 200+degraded：租约没被确认，但绝对不算失效
        ({"ok": True, "status": 200, "data": {"success": True, "degraded": True}}, "renewed"),
        # 200 但 body 不是 {success:true}（被网关改写）⇒ 不能当续约成功
        ({"ok": True, "status": 200, "data": None}, "soft-lost"),
        ({"ok": True, "status": 200, "data": {"success": False, "code": "lost"}}, "soft-lost"),
    ])
    assert not bad, bad


def test_401_is_relogin_not_lost():
    """401 只清登录态（重登后由 claim 状态机自愈），不该走进「其他设备正在下载」分支。"""
    bad = _run([({"ok": False, "status": 401, "data": {"detail": "登录已失效"}}, "relogin")])
    assert not bad, bad


# ════════════════ 结构约束：分界线只能有这五种出口 ════════════════
CANON = {"renewed", "relogin", "requeue", "hard-lost", "soft-lost"}


def test_outcome_vocabulary_is_closed():
    """出口只能是这五种，且 heartbeatClaim 必须每种都有处置。

    约束的是"将来有人加第六种 return 却忘了在调用方处理"——那种漏分支的表现是
    服务器回了新 code 而插件什么都不做（下载卡住且无提示），比报错更难查。
    """
    body_all = JS.read_text(encoding="utf-8")
    src = _extract_fn(body_all, "classifyBeat")
    # 'requeued' 是服务器 code 字面量，不是函数出口
    words = set(re.findall(r"'([a-z][a-z-]{2,})'", src)) - {"requeued"}
    assert words == CANON, f"心跳出口集合漂移：{words ^ CANON}"
    seg = body_all[body_all.index("async function heartbeatClaim"):
                   body_all.index("async function maybeHeartbeat")]
    for kind in CANON:
        assert f"'{kind}'" in seg, f"heartbeatClaim 未处理 {kind} 分支"


def test_stalled_gate_exists():
    """soft-lost 的落点必须真的挡住派发，并在恢复时解除（否则"暂停"是假的）。"""
    body = JS.read_text(encoding="utf-8")
    pump_seg = body[body.index("async function pump()"):body.index("async function pump()") + 1200]
    gate = pump_seg[pump_seg.index("if (beatStalled) {"):]
    gate = gate[:gate.index("\n  }")]
    assert "return" in gate, "pump() 未按 beatStalled 收口"
    assert "await heartbeatClaim" not in gate, \
        "暂停探测不能 await：服务器连而不答时会把整条派发链卡死（改成后台探测 + 解除后重新 pump）"
    resumed = body[body.index("if (kind === 'renewed')"):body.index("async function maybeHeartbeat")]
    assert "beatStalled = false" in resumed, "续约成功后没有解除暂停（会永久卡住）"
    assert "listFresh" in body, "handleClaimLost 丢了「列表不可知则不 finalize」的保护"


def test_heartbeat_fetch_timeout_is_wired():
    """心跳必须自带超时，且只有心跳带（其余调用维持"慢也等完"的原语义）。

    背景：0.7.2 起 pump() 在"暂停"态会后台探测一次心跳。老 serverApi 没有超时，
    服务器"连而不答"（反代挂起）时 fetch 永不返回 ⇒ 探测永远 pending、claim 状态机也被
    同一个 await 卡住 ⇒ 整个下载停摆。所以这一拍必须有上限，并且按"联系不上"处理。
    """
    body_all = JS.read_text(encoding="utf-8")
    fn = _extract_fn(body_all, "serverApi")
    assert "timeoutMs" in fn, "serverApi 丢了 timeoutMs 形参（心跳会重新变成无限等）"
    script = f"""
const cfg = async () => ({{ serverUrl: 'http://x', token: 't' }})
const SRC = {json.dumps(fn)}
const serverApi = new Function('cfg', 'return (' + SRC + ')')(cfg)
const out = []
let seen = null
globalThis.fetch = (u, o) => {{ seen = o; return new Promise((_r, rej) => {{
  o.signal.addEventListener('abort', () => rej(new Error('Aborted')))
}}) }}
const t0 = Date.now()
let outcome = 'resolved'
try {{ await serverApi('/hb', {{ method: 'POST', body: {{}}, timeoutMs: 120 }}) }} catch (e) {{ outcome = 'rejected' }}
out.push({{ outcome, dt: Date.now() - t0 }})
seen = null
globalThis.fetch = async (u, o) => {{ seen = o; return {{ ok: true, status: 200, json: async () => ({{ success: true }}) }} }}
await serverApi('/other', {{ method: 'POST', body: {{}} }})
out.push({{ signal: (seen && seen.signal !== undefined) ? 'present' : 'none' }})
console.log(JSON.stringify(out))
"""
    # node 的 --input-type 对 -e 不稳；写成临时 .mjs 用文件方式跑，跨 node 版本都一致
    fd, tmp = tempfile.mkstemp(suffix=".mjs")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp)
    assert out.returncode == 0, out.stderr[-1500:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res[0]["outcome"] == "rejected", "超时没有让 fetch 失败（会永久挂住派发链）"
    assert 100 <= res[0]["dt"] < 5000, f"超时时刻不合理：{res[0]['dt']}ms"
    assert res[1]["signal"] == "none", "未传 timeoutMs 的调用被强加了超时 ⇒ 慢服务器上的正常请求会被误杀"


def test_beat_caller_passes_timeout():
    body = JS.read_text(encoding="utf-8")
    seg = body[body.index("async function heartbeatClaim"):body.index("async function maybeHeartbeat")]
    assert "timeoutMs: BEAT_TIMEOUT_MS" in seg, "心跳调用点没带上超时参数"
    assert "BEAT_TIMEOUT_MS = 20" in body, "BEAT_TIMEOUT_MS 被改动，请确认仍 < 租约与 3 次重试的乘积"


# ════════════════ claim 失效处置：顺序就是语义 ════════════════
LOST_CASES = [
    # 列表不可信（服务器 502/断连时就是这样）⇒ 不许猜任何结论
    ({"listFresh": False, "localStatus": "running", "serverStatus": "unknown"}, "pause-unknown"),
    # 本地已终态 ⇒ 绝不因"列表说 pending"就把下完的书打回重下
    ({"listFresh": True, "localStatus": "done", "serverStatus": "pending"}, "keep-terminal"),
    ({"listFresh": True, "localStatus": "cancelled", "serverStatus": "running"}, "keep-terminal"),
    # 只有列表可信时才按服务器状态分派
    ({"listFresh": True, "localStatus": "running", "serverStatus": "pending"}, "resume-pending"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "running"}, "freeze-other-device"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "gone"}, "finalize-server-done"),
    ({"listFresh": True, "localStatus": "running", "serverStatus": "done"}, "finalize-server-done"),
]


def test_claim_lost_decision():
    """`decideClaimLost` 的分支顺序：**列表不可信必须排在"按列表状态分派"之前**。

    0.7.1 的「其他设备正在下载此任务」误报就是这么来的：拉列表也失败了，于是拿**上一次**
    的列表判断，而列表里那条 `running` 往往正是本机自己在下的那本。
    把 `!listFresh` 放到 run/pending 分支后面（本次审查抓到的真实缺陷），这句谎报会原样保留。
    """
    body_all = JS.read_text(encoding="utf-8")
    fn = _extract_fn(body_all, "decideClaimLost")
    script = (f"const SRC = {json.dumps(fn)}\n"
              "const f = new Function('return (' + SRC + ')')();\n"
              f"const cases = {json.dumps(LOST_CASES, ensure_ascii=False)};\n"
              "const bad = cases.filter(([r, w]) => f(r) !== w).map(([r, w]) => JSON.stringify(r) + ' → ' + f(r) + ' ≠ ' + w);\n"
              "console.log(bad.length ? 'MISMATCH ' + bad.join(' | ') : 'MATCH');\n")
    fd, tmp = tempfile.mkstemp(suffix=".mjs")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp)
    assert out.returncode == 0, out.stderr[-1500:]
    line = out.stdout.strip().splitlines()[-1]
    assert line == "MATCH", line


def test_handle_claim_lost_uses_the_pure_decision():
    """处置必须由纯函数决定，且不许留一份"自己看 lastServerTasks 猜"的旁路。"""
    body = JS.read_text(encoding="utf-8")
    seg = body[body.index("async function handleClaimLost"):body.index("async function completeClaim")]
    assert "decideClaimLost({" in seg, "handleClaimLost 又改成内联判断了（无法单测，且顺序易错）"
    assert "serverPending.includes" not in seg and "serverRunning.includes" not in seg, \
        "残留的 serverRunning/serverPending 分支会绕过 listFresh 的顺序约束"


# ════════════════════════════════════════
#  D8：解析阶段必须有上限（否则一集挂住 ⇒ 下载槽被无限期占住）
# ════════════════════════════════════════
def test_resolve_is_time_bounded():
    body = JS.read_text(encoding="utf-8")
    assert "RESOLVE_TIMEOUT_MS" in body, "解析超时上限被删了（D8 会复活）"
    seg = body[body.index("async function runTrack"):] if "async function runTrack" in body else body
    i = seg.index("resolveForTrack(task, tr")
    assert "withTimeout(" in seg[max(0, i - 160):i], "解析调用点又变成裸 await 了"
    # 常量行尾带中文注释，取 // 之前的部分再求值
    consts = dict(re.findall(r"const (TRACK_TIMEOUT|RESOLVE_TIMEOUT_MS) = ([^\n]+)", body))
    nums = {k: eval(v.split("//")[0].strip().replace(" ", "")) for k, v in consts.items()}
    assert nums["RESOLVE_TIMEOUT_MS"] < nums["TRACK_TIMEOUT"], \
        "解析上限必须小于整集超时，否则槽位仍会被长期占住"
    assert nums["RESOLVE_TIMEOUT_MS"] <= 120_000, "解析上限设得过长：卡住的集会长时间占着单下载槽"


def test_with_timeout_semantics():
    """withTimeout 三条契约：正常值透传 / 挂起必拒 / 原错误不被吞（ClaimLostError 依赖它）。"""
    body_all = JS.read_text(encoding="utf-8")
    fn = _extract_fn(body_all, "withTimeout")
    script = f"""
const SRC = {json.dumps(fn)}
const withTimeout = new Function('return (' + SRC + ')')()
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
const out = []
out.push('value:' + (await withTimeout(Promise.resolve('mp3-url'), 500, '解析')))
const t0 = Date.now()
try {{ await withTimeout(new Promise(() => {{}}), 120, '解析直链'); out.push('hang:resolved') }}
catch (e) {{ out.push('hang:' + (/超时/.test(e.message) ? 'rejected' : 'wrong-err:' + e.message) + ':' + (Date.now() - t0 >= 100 ? 'in-time' : 'too-early')) }}
try {{ await withTimeout(Promise.reject(new Error('claim-lost')), 500, '解析'); out.push('rej:swallowed') }}
catch (e) {{ out.push('rej:' + e.message) }}
await sleep(60)                       // 让 finally 的 clearTimeout 有机会把 timer 清掉
console.log(JSON.stringify(out))
"""
    fd, tmp = tempfile.mkstemp(suffix=".mjs")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp)
    assert out.returncode == 0, out.stderr[-1200:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == ["value:mp3-url", "hang:rejected:in-time", "rej:claim-lost"], res
