# 浏览器插件「本地下载」方案（详细规划 + POC）

> 目标：把**音频获取接口（track API）**从服务器转移到用户浏览器插件执行，达成两点——
> 1. **请求走用户公网 IP**（缓解服务器 IP 维度风控，官方/第三方音源都受益）；
> 2. **音频字节直接落用户本地磁盘**（不占服务器空间、不消耗服务器带宽）。
>
> 服务器职责收窄为：卡密鉴权 + 专辑/章节元数据下发 + 任务派发与 ack。

---

## 一、整体架构

```
┌─────────────┐   ①卡密登录    ┌──────────────────────────┐
│  浏览器插件   │ ─────────────►│  后端 /api/auth/login      │
│ (MV3 扩展)   │               │  返回 token               │
└──────┬──────┘               └──────────────────────────┘
       │                               ▲
       │ ②轮询 /api/extension/tasks    │ ④前端「本地下载」推送
       │   (章节元数据，不含直链)        │   POST /api/extension/task
       ▼                               │
┌─────────────┐   ③插件自行解析直链      │
│ 插件内解析器  │ ── track API ──► 音源   │
│ (走用户IP)   │ ◄── 音频直链 ────       │
└──────┬──────┘                        │
       │ ⑤chrome.downloads 下载         │
       ▼                               │
  用户本地磁盘                     ┌────┴─────────────────────┐
                                  │ 前端批量下载页 Search.vue  │
                                  │ 新增 离线/本地 单选开关    │
                                  └──────────────────────────┘
```

**关键原则**：同一时刻的一次"音频接口请求"，**要么全在服务器、要么全在插件**，无法既要走用户 IP 又把解析逻辑完全不透给用户。本方案选择"解析下沉到插件"，因此**接口实现（含第三方）会随插件分发给用户**——这是你已确认的取舍。

---

## 二、前端改造（已完成）

`frontend/src/views/Search.vue` 批量下载区新增两个单选：

| 选项 | 行为 |
|------|------|
| 💾 离线下载（到服务器） | 保持原逻辑：`/download/batch` 或 `/intf/{name}/batch`，服务器下载 |
| 🌐 本地下载（浏览器插件） | 仅把裁剪后的章节元数据 POST 到 `/api/extension/task`，不下载、不占空间 |

本地模式提交逻辑：`album.tracks` 按起止集裁剪 → 映射为 `{track_id, episode_num, title, fmt}` → 携带 `source`(official 或接口名) 推送。

---

## 三、后端改造（已完成）

新增 `api/extension.py`（路由前缀 `/api/extension`，全部 `get_current_card` 鉴权）：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/task` | POST | 前端推送章节元数据，建 `local_tasks` 待下载任务（限 50 待处理/卡，5000 集/任务） |
| `/tasks` | GET | 插件轮询拉取当前卡密所有 `pending` 任务（含 tracks 明细） |
| `/tasks/{id}/ack` | POST | 插件下载完成后标记 `done`，不再被拉取 |

数据表 `local_tasks`（见 `db/models.py`）：`task_id / card_id / source / album_id / album_title / quality / fmt / tracks(JSON) / status / created_at`。
`app.py` 已 `include_router(extension_router)`。

> 设计取舍：后端**不解析直链、不下载字节**。它只信任卡密身份并下发"该下哪些集"。

---

## 四、插件实现（已完成 POC，`extension/`）

| 文件 | 作用 |
|------|------|
| `manifest.json` | MV3：权限 `storage/downloads/alarms/offscreen`，`host_permissions: <all_urls>`（POC 放宽，生产可收窄到具体域名） |
| `popup.html/js` | 服务器地址 + 卡密登录（复用 `/api/auth/login`），展示待下载任务，一键触发 |
| `background.js` | Service Worker：每 25s 闹钟轮询 → 逐集解析直链 → `chrome.downloads.download` → ack |
| `offscreen.html/js` | 加载官方 `dws1.6.8.js`，用**专属浏览器指纹**计算 `xm-sign`（替代服务器 Selenium Chrome） |
| `crypto.js` | AES-128-ECB 解密（密钥 `aaad3e4fd540b0f79dca95606e72bf93`，与后端一致；免费音频为明文直返） |
| `resolvers.js` | 各音源解析器注册表 `RESOLVERS`。`official` 已完整实现；第三方为占位框架 |

**官方源在插件内的完整链路**（验证"用户 IP + 省服务器 Chrome"）：
`background` → 向 `offscreen` 取 `xm-sign` → `fetch mobile.ximalaya.com/mobile/download/v2/track/{id}`（用户 IP）→ 拿到 `downloadAacUrl` → `crypto.js` 解密 → `chrome.downloads` 落盘。

> 注意：`chrome.downloads.download` 只允许白名单请求头（Authorization/Cookie/Referer/Origin/User-Agent 等），**自定义头（如 xm-sign）无法透传**。因此 xm-sign 只用于"取直链"那次 fetch（fetch 不受限），最终 CDN 直链下载无需自定义头——官方流程天然契合。

---

## 五、第三方接口如何接入（框架已留好）

1. 在后端「接口管理」确认接口 `name`（如 `tingshu8`）。
2. 把该接口 audio 阶段的 `parse` 逻辑用 JS 重写，挂到插件 `RESOLVERS['tingshu8']`：
   ```js
   RESOLVERS['tingshu8'] = async (chapter, ctx) => {
     const r = await fetch(`https://api.xxx.com/audio?id=${chapter.track_id}`, { headers: {...} })
     const j = await r.json()
     return j.data.url            // 或 { url, headers: { Referer: '...' } }
   }
   ```
3. 前端「本地下载」推送时 `source` 已是接口名，插件据此路由到对应解析器。

**破解风险与缓解**（你之前的核心顾虑）：
- 第三方解析器随插件分发给用户，理论上可读。缓解阶梯：
  1. **混淆打包**（esbuild/rollup + obfuscator）提高逆向成本；
  2. **密钥不留前端**：若某第三方需要服务端密钥，把"取直链"拆成"插件请求中转接口、服务端用密钥换直链回传"——但这会让该次请求回到服务器 IP（权衡点）；
  3. **私有分发**：插件不公开上架，仅发给付费卡密用户（本来就卡密授权）；
  4. **多 IP 轮换**：第三方仍留服务器时，用多个出口 IP 轮换，降低单 IP 风控。
- 官方源无此问题：`cid/KFp` 是公开 SDK 常量，`xm-sign` 用浏览器指纹现算，无密钥可泄露。

---

## 六、部署与测试步骤

1. **后端**：`docker compose build && up -d`（含新建表 `local_tasks`、`extension_router`）。
2. **前端**：`cd frontend && npm install && npm run build`（已加开关）。
3. **插件加载**：Chrome → `chrome://extensions` → 开发者模式 → 加载已解压的 `extension/` 目录。
4. **登录**：插件弹窗填服务器地址 + 卡密登录。
5. **推送**：网页端批量下载选「🌐 本地下载」→ 插件自动（或手动"立即下载"）解析并下载到本机"下载/专辑名/"目录。

---

## 七、待办 / 已知限制

- [ ] 官方 VIP 加密音源若需 cookie 鉴权：当前 POC 免费音源免 cookie；会员音源需把卡密对应 cookie 随任务下发（HTTPS+token 保护，已在备注）。
- [ ] 第三方 `tingshu8` 等真实接口待抓包后补全 `RESOLVERS`（框架已就绪）。
- [ ] `host_permissions` 当前为 `<all_urls>`，生产应收窄到具体音源域名。
- [ ] 无并发/限速控制，POC 顺序下载；生产可在 `background.js` 加滑动窗口。
- [ ] 失败重试/断点续传未做（插件侧可后续补）。
```
