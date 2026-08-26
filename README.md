# 喜马拉雅有声书下载器（卡密授权版）

基于 FastAPI + Vue3 的喜马拉雅音频下载工具。前后端分离，所有配置/任务/账号统一入库（SQLite），
引入**卡密授权体系**：用户用卡密登录，每张卡密数据相互隔离，过期自动清零（磁盘文件保留）。

## 功能特性

- 🔑 **卡密授权** — 管理员生成卡密（固定到期 / 激活后 N 天），用户用卡密登录
- 🔒 **数据隔离** — 每张卡密下载目录、任务、进度、文件列表完全独立，互不可见
- 🔍 **搜索** — 官方接口 + 第三方免登录双引擎搜索专辑
- ⬇️ **下载** — 单集/批量下载，支持 MP3/M4A，VIP 音频自动解密，多卡密可同时并发下载
- 📦 **一键下载到本地** — 专辑音频打包 ZIP 流式返回，浏览器直接下载
- 👤 **多账号管理** — 二维码扫码登录，Cookie 持久化，VIP 账号优先，限流自动切换（24h 冷却）
- 📁 **文件管理** — 按卡密隔离浏览、ZIP 打包下载、删除、去重清理
- ☁ **同步到夸克** — 管理员按卡密授权后，可把已下完的书复制到飞牛夸克挂载目录（默认关；成功后保留本地）
- 🐳 **Docker 部署** — 两段式构建（Node 构建前端 → Python 运行后端），内置 Chromium
- 🔀 **卡密下载模式** — 按卡密限定下载通道（`server` 仅服务器 / `local` 仅本地 / `both` 都允许）
- 🛡️ **后台局域网开关** — 管理后台与文档页可配置「仅局域网」或放开公网访问，即时生效
- 🔄 **供体账号池** — 用户扫码登录自动同步 Cookie，管理员可一键注入免前端扫码

## 快速部署

### 1. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
```

### 2. 配置内网镜像仓库

```bash
cat > /etc/docker/daemon.json << 'EOF'
{
  "insecure-registries": ["192.168.100.105:6551"]
}
EOF
systemctl restart docker
```

### 3. 克隆项目

```bash
git clone http://192.168.100.105:6551/cq10086123/yousheng_xiazai.git
cd yousheng_xiazai
```

### 4. 构建并启动

```bash
docker compose up -d --build
```

> 采用两段式构建：Node 先构建 `frontend/dist`，再拷贝进 Python 运行镜像。
> 首次构建会从 npm 拉取前端依赖，耗时稍长，属正常。

### 5. 首次使用（卡密授权）

1. 打开 `http://服务器IP:6500`，先进入**管理后台**（`/admin/login`）用默认管理员登录：
   - 账号：`admin`　密码：`admin123`
2. 在「卡密管理」生成卡密（固定到期 / 激活后 N 天），复制卡密号。
3. **务必在管理后台修改默认管理员密码**（安全起见）。
4. 回到首页用卡密登录，即可搜索、批量下载、管理文件。

> ⚠️ 默认管理员密码仅用于首次部署，请尽快修改。

### 6. 查看日志

```bash
docker compose logs -f
```

## 日常运维命令

| 场景 | 命令 |
|------|------|
| 更新代码 | 见下方「更新代码（服务器部署）」章节 |
| 无缓存构建 | `docker compose build --no-cache && docker compose up -d` |
| 修改 .env 后 | `docker compose up -d --force-recreate` |
| 修改端口后 | `docker compose up -d --force-recreate` |
| 查看日志 | `docker compose logs -f` |
| 查看状态 | `docker compose ps` |
| 重启服务 | `docker compose restart` |
| 停止服务 | `docker compose down` |

### 更新代码（服务器部署）

重构后运行时数据全部落在 `data/app.db`（SQLite）与 `downloads/` 分区目录，不再改写受版本控制的源码文件，
因此服务器更新可以直接对齐远程并重建镜像：

```bash
cd yousheng_xiazai

# 1. 对齐远程最新版本（data/ 与 downloads/ 已通过 volume 挂载，不会被覆盖）
git fetch origin
git reset --hard origin/master

# 2. 重新构建镜像（--no-cache 确保新代码 + 前端 dist 进入镜像），再启动
docker compose build --no-cache
docker compose up -d
```

> ⚠️ **为什么必须 `--no-cache`？**
> 镜像是构建型（两段式 Dockerfile），`git pull` 只更新宿主机文件，容器里跑的是旧镜像的代码。
> `docker compose build --no-cache` 才会把新代码与重新构建的前端打进镜像；只 `restart` / 普通 `build` 不会生效。
>
> 💡 **数据安全**：`data/app.db`（卡密 / 任务 / 配置 / 账号）与 `downloads/`（音频文件）均通过 volume 持久化，
> `git reset --hard` 与重建容器都不会触碰它们。

如果只是改了 `.env` 或端口，不需要重建镜像，见下方对应小节。

### 修改下载路径（.env）

```bash
vi .env  # 修改 DOWNLOAD_DIR 对应的宿主机映射（默认 ./downloads）
docker compose up -d --force-recreate  # 必须重建容器才生效，不需要重新构建镜像
```

### 修改端口

编辑 `docker-compose.yml`，修改 `ports` 左侧端口号（容器内固定 6500 不用改）：

```yaml
ports:
  - "8080:6500"  # 宿主机8080 → 容器6500
```

```bash
docker compose up -d --force-recreate
```

### 查看容器状态

```bash
docker compose ps          # 查看运行状态
docker compose logs -f     # 实时日志
docker compose restart     # 重启服务
docker compose down        # 停止并删除容器
```

## 目录说明

| 宿主机 | 容器内 | 用途 |
|--------|--------|------|
| `./downloads` | `/app/downloads` | 分区下载根目录（`/app/downloads/{卡密}/`） |
| `./data` | `/app/data` | SQLite 数据库 `app.db`（卡密/任务/配置/账号） |
| `/vol02/1000-1-92d69cac/yousheng` | `/app/quark` | 飞牛夸克挂载（仅开通卡密可同步；目标 `yousheng/{书名}/`） |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATA_DIR` | SQLite 数据库所在目录 | `/app/data` |
| `DOWNLOAD_DIR` | 容器内下载根目录（卡密子目录自动创建） | `/app/downloads` |
| `QUARK_SYNC_DIR` | 容器内夸克挂载路径；空则关闭同步 | `/app/quark` |
| `TZ` | 时区 | `Asia/Shanghai` |

## 技术栈

| 组件 | 说明 |
|------|------|
| FastAPI | 后端 Web 框架，纯 REST API |
| Vue3 + Vite + Pinia + Vue Router + Axios | 前端 SPA（构建产物 `frontend/dist` 由后端托管） |
| SQLAlchemy + SQLite (WAL) | 数据持久化（配置/卡密/任务/账号/日志） |
| Selenium + Chromium | 无头浏览器生成 xm-sign |
| requests | HTTP 请求（官方接口） |
| pycryptodome | AES-128-ECB 解密 VIP 音频 URL |
| bcrypt | 管理员密码哈希 |
| Docker | 两段式构建，内置 Chromium |

## 卡密授权机制

- 卡密格式：`XM-XXXX-XXXX-XXXX`（随机生成，不可预测）。
- 两种有效期：`fixed`（固定到期时间）/ `days`（首次登录后 N 天）。
- 每张卡密数据完全隔离：下载目录 `{DOWNLOAD_DIR}/{卡密}/`、任务、进度 SSE、文件列表均按卡密维度隔离。
- 卡密过期：登录与鉴权时惰性判定 → 返回 401 并清除会话；后台定时任务（每分钟）扫描并清零过期卡密的数据库记录，**磁盘音频文件保留不删**。
- 管理 token 与业务 token 完全分离：管理员接口无法用卡密 token 调用，反之亦然。
- **网络绑定（一卡一IP，同IP不限设备）**：见下方「进阶能力」第 5 节，默认关闭，后台一键开启。

## 进阶能力与配置

### 1. 卡密下载模式限制（`download_mode`）

每张卡密可限制其可用的下载通道，防止「只买了服务器算力」的卡密跑去用本地下载（或反之）：

| 取值 | 含义 |
|------|------|
| `both`（默认 / 列值为 NULL） | 服务器下载 + 本地下载都允许 |
| `server` | 仅允许**服务器离线下载**（官方 `/api/download/batch`、第三方 `/api/intf/{name}/batch`） |
| `local`  | 仅允许**浏览器插件本地下载**（`/api/extension` 本地下载、`xm-cookie` 接口） |

- 设置入口：管理后台「卡密管理」生成表单与编辑弹窗的下拉框；不设置 / 选「都允许」均存 NULL → 后端归一为 `both`。
- 后端校验：`api/deps.py` 的 `ensure_download_mode_allowed(auth, "server" | "local")`，`both`/缺失/非法值短路放行，限定模式下访问不匹配的下载入口返回 403。
- 前端联动：`Search.vue` 按卡密模式禁用不被允许的下载方式 radio，并在单一允许模式下自动把默认下载方式切到该模式。
- 向后兼容：历史卡与未设置卡列值为 NULL → 一律按 `both` 处理，行为不变。

### 2. 后台局域网访问限制（`admin_lan_only`）

后台管理 API 与文档页（`/api/{admin_path}`、`/docs`、`/redoc`、`/openapi.json`）默认**仅允许私网 / 回环 IP** 访问，公网请求一律 403。

- 开关位置：后台「系统配置」→「局域网访问限制」bool 开关（默认开启）。
- 关闭后：公网也可访问后台面板与文档页（请务必改强密码并尽量走 HTTPS）。
- 即时生效，无需重启；开关只作用于 admin 前缀与文档页，**不影响**业务路由（auth/accounts/search/download/files/interfaces/extension）——搜索、下载、插件等功能不受该开关限制。
- 取值：配置键 `api_config.admin_lan_only`，`"1"/"true"/"yes"`→开启，`"0"/"false"/""`→关闭。

### 3. 供体账号池（`backend_xm_accounts`）

为「免前端扫码」场景准备的官方账号副本池：

- 用户扫码登录成功（`_handle_poll`）或后续「重新校验」（`verify_account`）时，后端会调用 `add_backend_account` 把该账号 cookie **同步进供体池**（按 `uid` 去重：已有则更新 cookie，不新增行）。
- 管理员可在「喜马拉雅账号」管理页把供体池中的账号**注入**到指定卡密下（`inject_backend_cookie_to_card`，同一卡密下该 uid 已存在则跳过）。
- 仅服务器下载链路使用供体池；插件的本地下载仍走用户本地 Chrome，不经过供体池。

### 4. 浏览器插件与第三方音源接入

插件位于 `extension/`，构建产物由后端静态托管；其服务端地址在 `extension/config.js` 中内置（默认 `https://fm.yunos.eu.cc/`）。

第三方音源（仅「服务器端下载」之外的本地插件下载需要）通过 `extension/sources/` 下的脚本扩展，契约：

```js
// 注册名 name 必须等于后端「接口管理」里该接口的真实 name（如 A、B），否则报「未实现解析器」
registerSource({ name: 'A', displayName: '免密A' }, async (chapter, ctx) => {
  // chapter: { albumId, chapterId, ... }；ctx: 模块级共享（含 ctx.albumId 等）
  // 返回音频直链字符串 或 { url, headers }
});
```

- 文件名建议与注册名一致（如 `A.js` / `B.js`），并在 `extension/sources/config.js` 的脚本清单里列出。
- 脚本经过混淆（`A.js` / `B.js` 已混淆，`registerSource` 注册名仍以原始 `A`/`B` 保留）。
- 仅「需要插件本地下载」的第三方音源才写 `sources/*.js` + 注册；只走服务器端下载的音源无需动插件。

### 5. 网络绑定（一卡一 IP / 顶号换绑）

防止一张卡密多人共用，同时对家庭用户宽松：卡密绑定**出口网络**而非具体设备。

**机制**：
- 客户端 IP 归一化为「网络键」：IPv4 按 /24、IPv6 按 /48 归一，局域网/内网统一视为同一网络（`lan`）。家庭宽带重拨的小幅跳变、IPv6 后缀轮换不会误踢。
- 同一网络下**不限设备与浏览器数量** —— 同一家庭宽带里 Chrome / Edge / 手机 / 插件随便用，互不顶号。
- **双栈友好**：网络名额按协议族（IPv4 / IPv6 / 内网）分别计算，每族允许 `max_devices` 个网络（默认 1，可调 1~10）。双栈家庭的 v4 与 v6 各占各的名额、互不淘汰，浏览器在两族间切换（Happy Eyeballs）不会被误踢；风控的多网段判定同样分族计数。
- 顶号换绑：同族名额已满时在**另一个公网 IP** 登录 → 淘汰该族最早绑定的网络，其全部会话立即失效；旧网络下次请求得到 401「已在其他网络登录」。网页 60s 心跳、插件 1 分钟轮询，分钟级感知被踢。
- 防外带：token 拿到**未绑定的网络**使用 → 401「网络环境已变更」（仅拒绝不顶号，回已绑定网络自动恢复；只有在新网络"登录"才触发换绑）。
- 换网/售后：管理后台「卡密管理 → 网络」可查看绑定详情（网络段/绑定时间/最后活跃/最近 IP），支持解绑单个、解绑全部、踢下线；用户换网络重新登录即自动换绑，无需找管理员。

**配置项**（后台「系统配置」，即时生效）：

| 配置键 | 说明 | 默认 |
|--------|------|------|
| `device_binding_enabled` | 网络绑定总开关；关闭 = 完全恢复历史行为（一键回退） | 关 |
| `trust_proxy_header` | 信任反向代理传递的 `X-Forwarded-For` / `X-Real-IP`（**部署在 nginx 等可信反代后必须开启**，否则所有用户 IP 都是代理 IP，网络绑定会失效；直连部署务必保持关闭防伪造）。解析取 XFF **最右侧**合法 IP（可信代理追加的真实来源，防止客户端自带伪造头），适用于单层可信代理 | 关 |
| `session_idle_days` | 会话空闲过期天数 | 7 |
| `token_multi_ip_kick` | 同 token 10 分钟内多公网网段并用 → 强制下线（绑定关闭时的兜底防线） | 开 |

**平滑过渡**：开关关闭时登录不落网络键、鉴权跳过校验；升级部署时已登录的存量会话（含旧版按设备绑定产生的会话）一律放行，下次登录起自然换轨，不误踢在线用户。

**实现位置**：绑定策略集中在 `core/device_binding.py`，风控在 `api/risk_control.py`；业务路由（搜索/下载/重试等）零改动，仅 `api/deps.py` 的 `_auth_card` 追加一处校验，全部事件写入 `card_logs`。相关测试：`tests/test_device_binding.py`（纯函数+集成）、`tests/e2e_device_binding.py`（端到端）。

## 注意事项

- 环境变量 `DOWNLOAD_DIR` 决定分区**根目录**，卡密子目录由后端按卡密号自动创建
- 如果挂载目录是 rclone/FUSE 远程挂载，需要 `privileged: true`（docker-compose.yml 已配置）
- `shm_size: "2g"` 是 Chromium 运行所需的最小共享内存
- 修改 `.env` 后必须运行 `docker compose up -d --force-recreate`（只需重建容器，不需要 `--build`）
- `--no-cache` 是 `docker compose build` 的参数，不能直接用于 `docker compose up`
- Dockerfile 里的 `EXPOSE 6500` 只是文档声明，实际端口由 `docker-compose.yml` 的 `ports` 控制
- 前端使用 Hash 路由（`/#/login` 等），刷新任意子页面都不会 404

## 访问地址

| 服务 | 地址 |
|------|------|
| WebUI | `http://服务器IP:6500` |
| Gitea 仓库 | `http://192.168.100.105:6551/cq10086123/yousheng_xiazai` |
| 镜像仓库 | `192.168.100.105:6551/cq10086123/ximalaya-webui:latest` |
