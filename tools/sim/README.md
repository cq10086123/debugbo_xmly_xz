# xmly_xz 本地仿真环境（开发工具，不进镜像）

用来**在真服务上复现用户报的两个现象、并验证修复真的改变了行为**。
它不替换 pytest，pytest 负责单元/接口契约，这里负责"跨进程、带故障、有时间轴"的行为对照。

```bash
# 仓库根目录执行；需要 venv（含 fastapi/sqlalchemy/httpx）
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
SLOT_TTL=45 python tools/sim/harness.py S1 S2 S4 S5 S7 S8 S9     # 指定场景
SLOT_TTL=45 python tools/sim/harness.py                          # 全量（约 15 分钟）
python tools/sim/harness.py S3 -v                                # 打印事件流
S3_COLD=1 python tools/sim/harness.py S3                             # 同上但走「冷列表」分支（见下表 S3）
```

环境变量：`SLOT_TTL`（本地租约秒数，默认 60；真实默认 300）、`SCEN_TIMEOUT`（单场景上限，默认 240s）、
`--keep`（跑完不关服务，便于手工 curl 看状态）、`BASE_PORT`（默认 6500，代理 6600）。
产物写在 `tools/sim/data/`（独立 SQLite）与 `tools/sim/state/<场景>/dl/`（假音频），随便删。

## 组成

| 文件 | 作用 |
| --- | --- |
| `harness.py` | 起**真** `python app.py`（独立 `DATA_DIR`）→ 播种卡密/会话/接口 → 跑场景断言 → 优雅收尾并检查残留锁 |
| `run.py` | `FakePlugin`：复刻 `extension/background.js` 的 claim/心跳/派发/收尾状态机；`FaultProxy`：按 path 注入 `http502 / refused / hang / latency_ms`；`verify_against_plugin()`：用 node 跑真实 `classifyBeat` 比对决策表 |
| —— | 下载字节是假的、没有 Chrome API、代理在同机；**租约/claim/心跳/列表全是真 HTTP + 真 SQLite + 真 `core/download_slot.py`** |

## 场景（每个都是断言，不是"看一眼"）

| 场景 | 测什么 | 期望 |
| --- | --- | --- |
| `S1` | 单机单卡正常下载 30 集 | 全 done、零提示、零残留锁（任何改动后的第一道保险） |
| `S2` | 反代 502 持续 40s（租约没到期） | 0.7.1 谎报「其他设备」并掐断；0.7.2 零提示且恢复后下完 |
| `S3` | 服务进程 `kill -9` 后 20s 重启 | 默认「热列表」⇒ 0.7.1/PR-2.5 拿陈旧列表里的 running 猜归属 ⇒ 谎报「其他设备正在下载此任务」；`S3_COLD=1`「冷列表」⇒ 0.7.1 猜成「服务器已完成」⇒ 半本被标 `done`。0.7.2 两种都必须无归属谎报且不 finalize |
| `S4` | 锁刚过期、sweep 未跑（用户报的主窗口） | `slot_renew_relaxed=1` 原地续命不打扰；`=0` 精确重现 0.7.1 误报 |
| `S5` | 同卡双设备互斥与真接管 | B 被挡、A 离线后 B 接管、A 回来被拦，**同集双写必须为 0** |
| `S6` | 六卡密并发 + 心跳延迟分布 | 全部下完、零误报；顺带量 `/api/extension/*` 的 p95（PR-3 的收益证据要在线上取） |
| `S7` | 超 `_MAX_TRACKS_PER_TASK` 的截断可见性 | `truncated` 字段 + 文案必须说明少了多少、怎么办 |
| `S8` | 同专辑重复推送 | 曲目不同必须新建；完全相同才幂等 |
| `S9` | 换格式 / 换音质的去重 | 换 fmt 必须新建；只换音质必须给出解释 |

## 加场景的规矩

1. **成对跑**：同场景用 `policy="0.7.1"` 与 `"pr2.6"`（以及 `cfg_set("slot_renew_relaxed","0"/"1")`）各跑一遍——只证明"新行为对"没意义，必须同时证明"旧行为确实会犯"；
2. 断言写"不变式"而不是"实现细节"：如 `双设备同集文件被两边各下一份 == 0`、`半本被标 done == False`；
3. 时序别靠 sleep 撞后台任务：`app.py` 的 `_slot_maintenance_loop` 每 30s 会 sweep，会把"刚过期"窗口抹掉——需要该窗口时直接拨 `lease_until`（见 `S4`）；
4. 每场景开头 `wipe_tasks()`，结尾 `p.stop.set()`，让 `report()` 决定红绿灯。
