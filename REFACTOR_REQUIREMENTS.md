# 项目重构需求文档（有声书下载器）

> 本文档是重构本项目的唯一依据。任何 AI 实现前必须先完整阅读本文档，
> 如有歧义按本文档为准；本文档未提及的细节，实现者按“保留现有功能行为”处理。

---

## 0. 项目背景与现状

当前项目为「喜马拉雅有声书下载器」，单体 Web 应用：

- **后端**：FastAPI，入口 `app.py`，挂载 6 个路由模块
  （login / download / search / settings / log / thirdparty）。
- **前端**：`static/` 目录下的原生 HTML + JS，由后端直接渲染（非前后端分离）。
- **数据存储**（全部是本地文件，需迁移到数据库）：
  - `config.json` —— 全局配置 + 第三方 API 接口与密钥（itingshu_* 等）
  - `accounts.json` —— 喜马拉雅 VIP 多账号
  - `cookie.txt` —— 默认账号 Cookie
  - `.workbuddy/batch_tasks.json`、`.workbuddy/thirdparty_tasks.json` —— 下载任务持久化
- **下载引擎**（两个，都必须保留）：
  1. 官方接口：Selenium 无头浏览器生成 `xm-sign`，配合登录 Cookie 下载。
  2. 第三方免登录：`itingshu` API（接口地址与密钥在 `config.json` 中）。
- **下载目录**：默认 `downloads/`，可用环境变量 `DOWNLOAD_DIR` / `AUDIO_DIR` 覆盖。
- **部署**：Docker（Dockerfile + docker-compose.yml），端口 6500。

---

## 1. 重构目标

1. 前后端分离：前端独立为 Vue3 工程，后端只提供 REST API。
2. 数据入库：所有配置、密钥、接口地址、任务、记录迁入 SQLite 数据库。
3. 引入「卡密」授权体系：
   - 前端用卡密登录；
   - 后端可生成卡密、设置有效期；
   - 每个卡密数据相互独立；
   - 卡密过期后该卡密的数据清零（仅数据库记录，磁盘文件保留）。
4. 新增「一键下载到本地」：已下载的单张专辑可打包 ZIP 供浏览器下载。
5. 支持多个卡密同时登录、同时下载，互不干扰。

---

## 2. 总体架构

```
┌─────────────────────┐        ┌──────────────────────────┐
│  前端 (Vue3 + Vite) │  REST  │  后端 (FastAPI)          │
│  frontend/          │ ─────► │  api/ (纯 API, 无页面)   │
│  - 卡密登录页        │ ◄───── │  core/ (下载/解密/登录)  │
│  - 搜索/下载/文件页   │        │  db/  (SQLAlchemy 模型)  │
│  - 管理后台          │        │  ┌──────────────────┐   │
└─────────────────────┘        │  │ SQLite (app.db)  │   │
                               │  └──────────────────┘   │
                               └──────────────────────────┘
```

- 前端构建产物由后端静态托管（`app.mount("/", frontend/dist)`），也支持独立部署到任意静态服务器。
- 开发模式下前端通过 Vite proxy 转发 `/api` 到后端。
- 所有业务数据存 SQLite 单文件 `data/app.db`；下载的音频文件仍落磁盘（保留现有 `DOWNLOAD_DIR` 机制）。

---

## 3. 数据库设计（SQLite）

使用 SQLAlchemy ORM，表如下（字段可微调，语义必须对齐）：

### 3.1 `cards` —— 卡密表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| code | TEXT UNIQUE NOT NULL | 卡密号，后端生成，如 `XM-XXXX-XXXX-XXXX`，用户可见 |
| status | TEXT | `active` / `used` / `disabled` / `expired`（由系统自动更新） |
| expiry_type | TEXT | `fixed`（固定到期）或 `days`（激活后 N 天） |
| expires_at | DATETIME NULL | expiry_type=fixed 时的到期时间 |
| valid_days | INTEGER NULL | expiry_type=days 时的有效天数 |
| activated_at | DATETIME NULL | 首次登录时间（激活时间） |
| note | TEXT NULL | 备注 / 使用者 |
| created_at | DATETIME | 生成时间 |
| last_login_at | DATETIME NULL | 最近一次登录时间 |

### 3.2 `sessions` —— 登录会话表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| token | TEXT UNIQUE NOT NULL | 登录后发放给前端的随机 token（建议 32 位以上随机串，不采用无状态 JWT，便于即时踢下线） |
| card_id | INTEGER FK→cards | 所属卡密 |
| created_at | DATETIME | |
| last_active_at | DATETIME | |
| is_active | BOOLEAN | 登出或过期后置 False |

### 3.3 `api_config` —— 接口与密钥配置表（替代 config.json）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| cfg_key | TEXT UNIQUE NOT NULL | 配置键，如 `itingshu_host`、`itingshu_toket`、`itingshu_device_key`、`itingshu_user`、`itingshu_sign`、`itingshu_book_token`、`itingshu_app_version`、`auto_retry_enabled` 等 |
| cfg_value | TEXT | 配置值（接口地址、密钥、token、开关等） |
| category | TEXT | `thirdparty` / `settings` 等 |
| updated_at | DATETIME | |

> 迁移规则：`config.json` 全部键值迁入本表，启动时若表空则从 `config.json` 导入一次，之后一律读数据库。

### 3.4 `download_tasks` —— 下载任务表（替代两个 batch_tasks.json）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| task_id | TEXT UNIQUE | 对外暴露的任务 ID（保留原 8 位 uuid 风格） |
| card_id | INTEGER FK→cards | 任务所属卡密，**数据隔离的唯一依据** |
| engine | TEXT | `official`（官方接口）或 `thirdparty`（itingshu） |
| album_id | TEXT | 专辑 ID / 书籍 ID |
| album_title | TEXT | 专辑名 |
| start_episode / end_episode | INTEGER | 下载范围 |
| fmt | TEXT | mp3 / m4a |
| quality | INTEGER | 官方引擎音质 |
| concurrency | INTEGER | 第三方引擎并发数 |
| status | TEXT | `running` / `done` / `failed` / `cancelled` / `interrupted` |
| total / current / completed / skipped_count | INTEGER | 进度 |
| current_title | TEXT | 当前集标题 |
| failed_list | TEXT(JSON) | 失败集列表 |
| completed_files | TEXT(JSON) | 已完成文件列表 |
| error | TEXT | |
| retry_episodes | TEXT(JSON) NULL | 重试子任务专用 |
| parent_task_id | TEXT NULL | 重试子任务指向父任务 |
| created_at / finished_at | DATETIME | |

### 3.5 `download_records` —— 下载记录表（每集一条）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| card_id | INTEGER FK→cards | |
| task_id | TEXT NULL | 来源任务（可为空，如单集下载） |
| album_id / album_title | TEXT | |
| track_id | TEXT | 单集 ID |
| title | TEXT | 单集标题 |
| episode_num | INTEGER | |
| file_path | TEXT | 磁盘路径 |
| file_size | INTEGER | |
| created_at | DATETIME | |

### 3.6 `ximalaya_accounts` —— 喜马拉雅账号表（替代 accounts.json，迁移可选但建议）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| nickname / uid / mobile | TEXT | |
| cookie_str | TEXT | 登录 Cookie |
| is_vip | BOOLEAN | |
| rate_limited_until | DATETIME NULL | 限流冷却至（原 24h 冷却逻辑保留） |
| added_at | DATETIME | |

### 3.7 `admins` —— 管理员表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| username | TEXT UNIQUE | |
| password_hash | TEXT | bcrypt 哈希 |
| created_at | DATETIME | |

### 3.8 `card_logs` —— 卡密操作日志（可选）
记录登录、过期清零、管理员操作等关键动作（action / card_id / detail / created_at）。

---

## 4. 卡密规则（核心，必须严格遵守）

1. **独立性**：一切业务数据（下载任务、下载记录、文件列表）都以 `card_id` 作为隔离键。
   卡密 A 的任何查询、SSE 推送、文件管理都**不得**看到卡密 B 的数据。
2. **状态流转**：
   - 生成 → `active`（未使用）→ 首次登录 → `used`；
   - `disabled` 由管理员手动设置，禁用期间不可登录；
   - 过期 → 自动标记 `expired`。
3. **有效期判定**（两种类型，生成时二选一）：
   - `fixed`：`now > expires_at` 即过期；
   - `days`：首次登录设置 `activated_at`，`now > activated_at + valid_days` 即过期。
4. **过期后的行为**：
   - 登录接口拒绝并提示已过期；
   - 已有 token 的请求在鉴权时同样判定过期 → 返回 401，并清除该卡密所有 `sessions`；
   - **数据清零**：删除该卡密在 `download_tasks`、`download_records` 中的所有记录；
     磁盘上的音频文件**保留不删**；
   - 由后台定时任务（如每分钟一次）统一扫描并执行清零，同时**也**在登录/鉴权时做惰性判定，确保即使定时任务未跑也拦截。
5. **多卡密并发**：
   - 多个卡密可同时在线、同时发起下载；
   - 任务表、进度 SSE、去重锁（`track_lock`）、下载目录都必须按卡密隔离；
   - 同一张卡密可在多个浏览器同时登录（允许多个 session），但会话各自独立，登出一个不影响其他会话。

### 4.6 独立下载（重点，务必实现到位）

「不同卡密登录后独立下载」包含**四层含义，缺一不可**：

1. **下载目录独立**：每张卡密下载到自己的目录 `{DOWNLOAD_DIR}/{card_code}/`。
   即使两张卡密下载**同一张专辑**，也各自得到一份**完整独立的副本**，互不覆盖、互不读取。
   > 例：卡密 A、B 同时下载专辑《三国演义》，
   > 磁盘上必须出现两份独立文件：
   > `downloads/XM-AAAA/三国演义/第1集.mp3` 与 `downloads/XM-BBBB/三国演义/第1集.mp3`。
2. **任务独立**：每张卡密的下载任务（批量/单集/重试）只在自己的任务列表里创建、更新、删除。
   卡密 A 发起的任务进度，只有卡密 A 的 SSE 连接能收到；卡密 B 的任务列表里永远看不到它。
3. **并发独立**：多张卡密可同时发起多个下载任务并行运行，互不阻塞、互不等待。
   同一时刻「卡密 A 跑 3 个批量任务 + 卡密 B 跑 2 个批量任务」完全允许。
4. **去重与跳过只在本卡密目录内生效**：判断「某集是否已下载」只扫描**本卡密**目录，
   **绝不**因为卡密 A 已下载过某集，就把卡密 B 的同一集判为「已存在，跳过」——B 的目录里必须真的生成文件。
   去重锁 `track_lock` 的锁键**必须**包含 `card_id`，不同卡密之间**不得互斥**。

---

## 5. 下载目录规划

- 目录结构按卡密隔离，这是「独立下载」的**物理基础**，必须按此结构落盘：
  ```
  {DOWNLOAD_DIR}/{card_code}/{album_title}/第X集.mp3
  ```
- 两张卡密下载同一张专辑 = 两份独立副本（见 §4.6），绝不共享、绝不覆盖。
- 旧的非分区文件不强制迁移（保持兼容，迁移脚本可选）；**新下载一律写入按卡密分区的目录**。
- 环境变量 `DOWNLOAD_DIR` / `AUDIO_DIR` 只决定分区**根目录**，卡密子目录由后端按 `card_code` 自动创建。

---

## 6. 后端 API 设计

所有业务接口**必须**鉴权（除登录/管理登录外），鉴权方式：`Authorization: Bearer <token>`，
后端解析 token → 校验 session → 校验卡密未过期未禁用 → 注入 `card_id` 到请求上下文。

### 6.1 卡密登录 `/api/auth`
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 入参 `{code}`；校验卡密存在、可用、未过期；首次登录写入 `activated_at` 并置 `used`；发放 token，返回卡密信息（有效期、剩余天数等） |
| POST | `/api/auth/logout` | 使当前 token 失效 |
| GET | `/api/auth/me` | 返回当前卡密信息（code、过期时间、剩余天数、状态） |

### 6.2 管理端 `/api/admin`
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/admin/login` | 管理员登录（username + password，bcrypt 校验），发放 admin token |
| POST | `/api/admin/cards/generate` | 入参 `{count, expiry_type, expires_at|valid_days, note}`；批量生成卡密，返回列表 |
| GET | `/api/admin/cards` | 卡密列表（支持状态/分页/搜索 code） |
| GET | `/api/admin/cards/{id}` | 卡密详情 |
| PATCH | `/api/admin/cards/{id}` | 修改备注、状态（禁用/启用）、续期（改 expires_at / valid_days） |
| DELETE | `/api/admin/cards/{id}` | 删除卡密（可选，连同其任务/记录） |
| GET/PUT | `/api/admin/config` | 接口与密钥配置 CRUD（读写 `api_config` 表） |
| GET | `/api/admin/accounts` | 喜马拉雅账号管理（沿用现有多账号逻辑） |

### 6.3 下载与搜索（沿用现有功能，全部按 card_id 隔离）
- `GET /api/search` —— 搜索（保留现有逻辑）
- `POST /api/download/track`、`POST /api/download/chapter`、`POST /api/download/album-list`
- `POST /api/download/batch`、`GET /api/download/batch`、`GET /api/download/batch/{task_id}`
- `POST /api/download/batch/{task_id}/cancel|resume|retry`、`DELETE .../{task_id}`
- `GET /api/download/batch/stream` —— SSE 进度（**按卡密过滤**，每个连接只推送自己卡密的任务）
- `POST /api/thirdparty/download/batch` 等第三方引擎接口同样改造

> 任务列表的「仅返回当前卡密任务」是硬性要求，不得全局返回。

### 6.4 文件管理 + 一键下载到本地
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/files` | 列出当前卡密已下载的文件（只扫描 `{DOWNLOAD_DIR}/{card_code}/`） |
| GET | `/api/files/album-zip?album=xxx` | **新增**：把指定专辑目录下音频文件打包成 ZIP，流式返回，浏览器直接下载 |
| DELETE | `/api/files/{path}` | 删除单个文件（沿用现有） |
| POST | `/api/files/cleanup-duplicates` | 去重（沿用现有） |

**ZIP 打包要求**：
- 使用流式打包（如 `zipstream`），禁止一次性读入内存，专辑可能数百集；
- 文件名保持 `第X集.mp3` 格式；
- 权限校验：`album` 必须属于当前卡密目录，防止路径穿越访问其他卡密文件。

---

## 7. 前端设计（Vue3 + Vite）

工程放 `frontend/` 目录。技术栈：Vue 3 + Vite + Vue Router + Pinia + Axios。

### 7.1 页面
| 路由 | 页面 | 说明 |
|------|------|------|
| `/login` | 卡密登录 | 输入卡密号 → 调 `/api/auth/login` → 存 token → 跳首页 |
| `/` | 首页/搜索 | 搜索专辑、看章节列表（沿用现有交互） |
| `/tasks` | 下载任务 | 批量下载、进度（SSE）、取消/重试/恢复（沿用现有交互） |
| `/files` | 文件管理 | 已下载列表，**每张专辑提供「下载 ZIP」按钮** |
| `/admin/login` | 管理登录 | |
| `/admin/cards` | 卡密管理 | 生成卡密、列表、禁用/续期 |
| `/admin/config` | 接口配置 | 读写 `api_config` |

### 7.2 前端规范
- Axios 拦截器统一附带 `Authorization: Bearer <token>`，token 存 localStorage（key：`auth_token`）；
- 收到 401 统一跳转 `/login`（admin token 单独存，收到 401 跳 `/admin/login`）；
- 路由守卫：未登录禁止进入业务页；admin 路由校验 admin token。

---

## 8. 保留与改造现有功能清单

| 现有功能 | 处理 |
|---------|------|
| 喜马拉雅官方接口搜索/章节/下载 | 保留，逻辑不变，仅加鉴权与卡密隔离 |
| Selenium 生成 xm-sign | 保留（core/sign、downloader 不变） |
| AES-128-ECB 解密 VIP 音频 URL | 保留（core/crypto 不变） |
| 多 VIP 账号 + 限流自动切换 + 24h 冷却 | 保留，数据从 accounts.json 迁到 `ximalaya_accounts` 表 |
| 失败自动重试（按分钟轮次） | 保留，任务状态迁到数据库 |
| SSE 实时进度 | 保留，改为按卡密过滤推送 |
| 任务去重锁（track_lock） | 保留，锁键中加入 card_id |
| 二维码扫码登录喜马拉雅账号 | 保留（属于管理端账号管理） |

---

## 9. 安全要求

1. 密码用 bcrypt 哈希存储，绝不存明文。
2. token 用 `secrets.token_urlsafe(32)` 生成，数据库唯一索引。
3. 卡密号生成避免可预测顺序（如 `XM-` + 随机块），生成后 MD5/哈希副本存库用于校验也可（按实现便利选择）。
4. 所有文件接口做路径校验，禁止穿越（`/api/files`、`/api/files/album-zip`、下载文件接口）。
5. 管理接口与业务接口的 token 必须分离（admin token 不能调业务接口、业务 token 不能调管理接口，或至少角色校验）。
6. SQLAlchemy 参数化查询，禁止字符串拼接 SQL。

---

## 10. 部署与工程结构

```
yousheng_xiazai/
├── app.py                 # FastAPI 入口（挂载 api + 前端 dist）
├── db/                    # 数据库模型 + 连接 + 初始化 + 迁移脚本
├── api/                   # 路由（auth/admin/download/search/files）
├── core/                  # 下载/解密/登录/sign/track_lock（尽量少改）
├── frontend/              # Vue3 + Vite 工程
│   ├── src/
│   └── dist/              # 构建产物（后端托管）
├── data/                  # 挂载卷：app.db、accounts 迁移前兼容等
└── downloads/             # 按卡密分区的下载目录
```

- Docker：`Dockerfile` 分两段（node 构建前端 → python 运行后端），
  `docker-compose.yml` 挂载 `./data:/app/data`（SQLite 持久化）与下载目录 volume。
- SQLite 连接开 WAL 模式，启动时自动建表 + 执行一次 config.json 迁移。
- 数据清理定时任务用 FastAPI lifespan + `asyncio` 后台任务（不引入过重依赖，APScheduler 可选）。

---

## 11. 验收标准（开发完必须逐条自测）

1. 用卡密 A 登录后，能搜索、批量下载、看到自己的任务与文件；用卡密 B 登录，完全看不到 A 的任何数据。
2. **卡密 A、B 同时在线、同时下载同一张专辑**：
   - 各自任务互不可见、SSE 进度只推给自己；
   - 磁盘各自生成 `downloads/{A卡密}/{专辑}/...` 与 `downloads/{B卡密}/{专辑}/...` 两份完整副本；
   - A 先下载过的集，B 不会被判为「已存在」而跳过，B 的目录里必须真的出现对应文件；
   - 两张卡密各跑各的批量任务，互不阻塞、互不覆盖。
3. 生成「固定到期」卡密并把到期时间设为过去 → 登录被拒，token 失效，其任务与记录被清空，磁盘文件仍在。
4. 生成「激活后 N 天」卡密 → 首次登录后计时，用脚本把 `activated_at` 改到 N 天前模拟过期 → 同上行为。
5. 文件管理页对某专辑点「下载 ZIP」，能正确下载并解压出全部音频，且该接口用另一张卡密的 token 访问同名专辑返回 403/404。
6. 管理后台能生成、禁用、续期、删除卡密；能修改 `itingshu_*` 配置并立即生效（无需重启）。
7. 无卡密 token 访问任意业务接口返回 401。
8. 后端重启后：未完成任务标记 `interrupted`、自动重试逻辑恢复、登录态（token 有效性）保持。

---

## 12. 非目标（本次明确不做）

- 不做用户注册体系（只有卡密和管理员两种身份）。
- 不做在线试听/流媒体播放。
- 不做卡密购买/支付。
- 不强制迁移旧的 `downloads/` 未分区文件到新目录。
- 不换下载引擎，官方 + itingshu 两套保留。