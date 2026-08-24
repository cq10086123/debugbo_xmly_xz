/**
 * background.js — MV3 service worker：轮询任务、解析直链、调用 chrome.downloads 下载
 * 关键：所有任务状态与进度持久化到 chrome.storage，popup 关闭后不会丢失。
 * 
 * 重构说明（2026-08-16）：
 * - 存储模型从"单 key 全量数组"改为"分片存储"：
 *   - xm_task:<taskId> 存储单个任务对象（含 tracks[]）
 *   - xm_taskIndex 存储轻量索引（任务 ID 列表 + 基本元数据）
 * - 消除了 O(N²) 写入放大问题：每次状态变更只读写单个任务
 * - 保留队列级锁 withQueue，锁内改为单任务读写，改动最小、风险最低
 * - popup 契约不变：getQueue 仍返回 {queue: [...]} 结构
 *
 * 稳定性重构（2026-08-23）：
 * - 修复进度卡住、重复集数、并发数异常等核心问题：
 *   1) activeCount/inflight 内存态在 SW 重启后丢失导致并发超限和重复派发 → 重构为仅用 inflight Set，
 *      并在 reconcileDownloads 中根据真实下载状态重建 inflight
 *   2) reconcileDownloads 之前在 withQueue 锁内串行执行 chrome.downloads.search，导致锁长时间占用，
 *      阻塞所有进度更新和派发，表现为进度卡住、并发异常 → 改为两阶段：先快照，再锁外查询，最后锁内批量更新
 *   3) handleDownloadChanged 之前未清理 inflight，SW 重启后旧下载完成时 inflight 仍残留或缺失 → 现在完成时清理 inflight
 *   4) setTaskAndUpdateIndex 在 withQueue 内重复读写索引导致旧快照覆盖 → 新增锁内专用版本，统一索引更新路径
 *   5) 重复集数：原去重仅检查其他任务已完成集，且仅在锁外检查，竞态下可能重复 → 改为检查所有非 error 状态，
 *      并在锁内二次校验，且后端推送时增加本地重复任务过滤
 *   6) pendingTerminals 无界增长后直接 clear 导致终端事件丢失，runTrack 超时 30 分钟才恢复 → 改为 LRU 淘汰
 *   7) getXmCookie 缓存 5 分钟，冷却标记后仍使用旧列表 → 冷却时主动失效缓存
 *   8) ensureOffscreen 在旧 Chrome 下可能永久失效 → 增加容错重试和重置逻辑
 *   9) keepAlive 0.5 分钟被 Chrome 钳制到 1 分钟，且不可靠 → 改为 1 分钟，并增加心跳日志
 *  10) buildFilename 未限制长度，可能超 OS 限制 → 增加截断
 *  11) pump 的 activeCount 与 inflight 可能不一致 → 统一使用 inflight.size
 */
importScripts('crypto.js', 'resolvers.js', 'sources.config.js')

// 按 sources.config.js 动态加载第三方音源（单文件失败隔离，不影响其余音源与官方源）
for (const f of (globalThis.PLUGIN_SOURCES || [])) {
  try {
    importScripts('sources/' + f)
  } catch (e) {
    console.error('[plugin] 第三方音源加载失败:', f, e && e.message)
  }
}

// ── 存储 key 定义 ──
const STORAGE_QUEUE = 'xm_taskQueue'      // 保留旧 key 用于向后兼容/迁移
const STORAGE_INDEX = 'xm_taskIndex'      // 新：轻量索引
const STORAGE_SETTINGS = 'xm_settings'
const TASK_KEY_PREFIX = 'xm_task:'        // 新：单个任务分片前缀

const DEFAULT_SETTINGS = {
  downloadPrefix: '有声下载',
  concurrency: 1,          // 同一本书内的并发集数（默认 1；多本书永远排队逐本下载）
  autoDownload: true,
  silentDownload: true,
  downloadDelayMs: 500,    // 每集开始前的间隔延迟（毫秒），防音源风控
  maxRetry: 3,             // 单集「解析直链/创建下载」失败自动重试次数
}

const MAX_RETRY = 3                     // 重试次数兜底默认值（设置里可调）
const TRACK_TIMEOUT = 30 * 60 * 1000    // 单集下载终端态等待上限（超时标记失败，防槽位泄漏）

let offscreenReady = false
let keepAliveActive = false

// ── 队列读写互斥 ──
// ⚠️ 死锁红线：withQueue 的回调里绝对不允许再调用 withQueue（自己等自己，永久卡死）。
//    锁内需要"检查任务是否完成/重算进度"时，只能用下面的纯函数 finalizeTask / recomputeProgress。
let queueLock = Promise.resolve()
function withQueue(fn) {
  const p = queueLock.then(fn, fn)
  queueLock = p.catch(() => {})
  return p
}

// ── Cookie 注入互斥（官方源并发下载时防止 cookie 串号）──
// MV3 禁止手动设置 Cookie 头，只能通过 chrome.cookies.set 写入全局 cookie 商店
// 若两个官方 track 并发下载时交叉安装 cookie，会导致 A 用了 B 的账号
// 用独立锁串行化官方解析的 cookie 安装 + fetch 阶段
let cookieLock = Promise.resolve()
function withCookieLock(fn) {
  const p = cookieLock.then(fn, fn)
  cookieLock = p.catch(() => {})
  return p
}

// ── 新分片存储 API ──

// 索引操作（⚠️ 这些函数不经过 withQueue 锁，调用方必须确保在锁内或无需锁保护）
async function getIndex() {
  return new Promise(r => chrome.storage.local.get([STORAGE_INDEX], o => r(o[STORAGE_INDEX] || [])))
}

async function setIndex(idx) {
  return new Promise(r => chrome.storage.local.set({ [STORAGE_INDEX]: idx }, r))
}

// 单个任务操作（⚠️ 这些函数不经过 withQueue 锁，调用方必须确保在锁内或无需锁保护）
function taskKey(taskId) { return TASK_KEY_PREFIX + taskId }

async function getTask(taskId) {
  return new Promise(r => chrome.storage.local.get([taskKey(taskId)], o => r(o[taskKey(taskId)] || null)))
}

async function setTask(task) {
  if (!task || !task.task_id) throw new Error('setTask: task_id is required')
  return new Promise(r => chrome.storage.local.set({ [taskKey(task.task_id)]: task }, r))
}

async function delTask(taskId) {
  return new Promise(r => chrome.storage.local.remove([taskKey(taskId)], r))
}

// 批量获取多个任务（用于需要全量队列的场景，如 popup getQueue）
async function getTasksBatch(taskIds) {
  if (!taskIds || !taskIds.length) return []
  const keys = taskIds.map(id => taskKey(id))
  return new Promise(r => chrome.storage.local.get(keys, o => {
    const tasks = []
    for (const id of taskIds) {
      const t = o[taskKey(id)]
      if (t) tasks.push(t)
    }
    r(tasks)
  }))
}

// 锁内更新索引条目（idx 已在锁内获取，避免重复 getIndex）
function upsertIndexEntry(idx, task) {
  const i = idx.findIndex(item => item.task_id === task.task_id)
  const entry = {
    task_id: task.task_id,
    album_id: task.album_id,
    album_title: task.album_title,
    source: task.source,
    status: task.status,
    paused: !!task.paused,
    frozen: !!task.frozen,   // 被其他设备 claim 的展示态任务（本地不下载）
    createdAt: task.createdAt || Date.now(),
  }
  if (i >= 0) idx[i] = { ...idx[i], ...entry }
  else idx.push(entry)
}

// 在锁外原子更新任务 + 索引（保证一致性）— 旧 API，保留给锁外调用
async function setTaskAndUpdateIndex(task) {
  if (!task || !task.task_id) throw new Error('setTaskAndUpdateIndex: task_id is required')
  await setTask(task)
  const idx = await getIndex()
  upsertIndexEntry(idx, task)
  await setIndex(idx)
}

// 向后兼容：从旧存储迁移到分片存储
async function migrateStorage() {
  const migrated = await new Promise(r => chrome.storage.local.get(['xm_storage_migrated'], o => r(o.xm_storage_migrated)))
  if (migrated) return
  try {
    const all = await new Promise(r => chrome.storage.local.get(null, r))
    const idx = await getIndex()
    const have = new Set(idx.map(i => i.task_id))
    let changed = false

    for (const [k, v] of Object.entries(all)) {
      if (!k.startsWith(TASK_KEY_PREFIX) || !v || !v.task_id) continue
      if (have.has(v.task_id)) continue
      idx.push({
        task_id: v.task_id, album_id: v.album_id, album_title: v.album_title,
        source: v.source, status: v.status, paused: !!v.paused,
        createdAt: v.createdAt || Date.now(),
      })
      have.add(v.task_id)
      changed = true
    }

    const old = all[STORAGE_QUEUE]
    if (old && old.length) {
      for (const t of old) {
        if (!t.task_id || have.has(t.task_id)) continue
        if (!all[taskKey(t.task_id)]) continue
        idx.push({
          task_id: t.task_id, album_id: t.album_id, album_title: t.album_title,
          source: t.source, status: t.status, paused: !!t.paused,
          createdAt: t.createdAt || Date.now(),
        })
        have.add(t.task_id)
        changed = true
      }
    }

    if (changed) await setIndex(idx)
    if (old && old.length) await new Promise(r => chrome.storage.local.remove([STORAGE_QUEUE], r))
    console.log('[plugin] 存储迁移/自愈完成')
  } catch (e) {
    console.error('[plugin] 存储迁移失败', e)
  }
  await new Promise(r => chrome.storage.local.set({ xm_storage_migrated: true }, r))
}

// 旧 API 兼容层（内部使用，逐步替换）
async function getQueue() {
  await migrateStorage()
  const idx = await getIndex()
  if (!idx.length) return []
  return getTasksBatch(idx.map(item => item.task_id))
}

async function setQueue(q) {
  await migrateStorage()
  if (!q || !q.length) {
    const idx = await getIndex()
    for (const item of idx) await delTask(item.task_id)
    await setIndex([])
    return
  }
  const idx = []
  for (const task of q) {
    if (!task.task_id) continue
    idx.push({
      task_id: task.task_id,
      album_id: task.album_id,
      album_title: task.album_title,
      source: task.source,
      status: task.status,
      paused: !!task.paused,
      createdAt: task.createdAt || Date.now(),
    })
    await setTask(task)
  }
  await setIndex(idx)
}

// ── 旧 storage 工具（保留）──
function cfg() { return new Promise(r => chrome.storage.local.get(['serverUrl', 'token'], r)) }
async function authHeaders() {
  const { token } = await cfg()
  if (!token) return null
  return { Authorization: 'Bearer ' + token }
}

// 401 统一处理：清本地登录态 + 记录原因（popup 打开时展示）+ 桌面通知。
// 触发场景：被新设备顶号、管理员解绑/踢下线、会话空闲过期、风控强制下线。
async function handleUnauthorized(reason) {
  const o = await new Promise(r => chrome.storage.local.get(['token', 'serverUrl'], r))
  if (!o.token) return
  const msg = reason || '登录已失效，请重新登录'
  await new Promise(r => chrome.storage.local.set({ token: null, card: null, auth_error: msg }, r))
  try {
    chrome.notifications.create('xm_auth_kicked', {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icon128.png'),
      title: '登录已失效',
      message: msg,
      priority: 2,
    }).catch(() => {})
  } catch (e) {}
  console.warn('[plugin] 登录失效：', msg)
}
function getSettings() { return new Promise(r => chrome.storage.local.get([STORAGE_SETTINGS], o => r({ ...DEFAULT_SETTINGS, ...(o[STORAGE_SETTINGS] || {}) }))) }
function setSettings(s) { return new Promise(r => chrome.storage.local.set({ [STORAGE_SETTINGS]: s }, r)) }
const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// 由 track 状态推导进度计数（纯函数，可在锁内安全调用）
function recomputeProgress(task) {
  task.progress = task.progress || { total: 0, done: 0, failed: 0 }
  task.progress.total = task.tracks.length
  task.progress.done = task.tracks.filter(t => t.status === 'done').length
  task.progress.failed = task.tracks.filter(t => t.status === 'error').length
}

// 锁内安全调用：若任务全部结束则置 done，返回"本次是否新完成"
function finalizeTask(task) {
  recomputeProgress(task)
  const total = task.tracks.length
  if (!total || task.status === 'done' || task.status === 'cancelled') return false
  const done = task.tracks.filter(t => t.status === 'done').length
  const failed = task.tracks.filter(t => t.status === 'error').length
  if (done + failed >= total) { task.status = 'done'; return true }
  return false
}

// ── 新分片版 updateTask（锁内原子更新任务+索引）──
async function updateTask(taskId, patch) {
  return withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    const updated = { ...task, ...patch }
    const idx = await getIndex()
    await setTask(updated)
    upsertIndexEntry(idx, updated)
    await setIndex(idx)
  })
}

// ── 新分片版 updateTrack（锁内原子更新任务+索引）──
async function updateTrack(taskId, trackId, patch) {
  let finished = null
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    const tr = task.tracks.find(t => t.track_id === trackId)
    if (!tr) return
    Object.assign(tr, patch)
    if (finalizeTask(task)) finished = { ...task }
    const idx = await getIndex()
    await setTask(task)
    upsertIndexEntry(idx, task)
    await setIndex(idx)
  })
  if (finished) notifyTaskDone(finished)
  maybeStopKeepAlive()
}

// ════════════════════════════════════════
//  全局下载槽（0.7.0）：claim → heartbeat → complete/cancel
//  同一卡密全局只允许一个任务真正下载；claimState 持久化到 chrome.storage，
//  SW 被杀重启后可恢复租约，避免每次重启都走一遍"租约过期→重新排队"。
// ════════════════════════════════════════
const STORAGE_CLAIM = 'xm_claim'
let claimState = null          // {taskId, claimId, leaseSeconds, lastBeatAt, beatFails}
let claimPending = false       // 用户显式请求（全部下载/开始）尚未兑现
let claimQueue = []            // 槽忙时排队的显式 claim（开始/重试失败集），FIFO
let lastServerTasks = []       // 最近一次 GET /tasks（pending+running），claim 失效时重新同步用
const SERVER_BEAT_INTERVAL_MS = 30 * 1000    // 心跳节流间隔（alarm 1 分钟兜底 + 进度事件随路）
// 连续心跳不可达 N 次 ⇒ 停止本地下载。租约 300s、心跳最慢 60s/次（alarm），
// N=3 ⇒ 约 90~180s 不可达即停：此时服务器侧租约多半已过期并可能把任务重新排队，
// 继续下载就会与"重新 claim 本任务的其他设备"并行落盘（全局槽的核心目标正是要防止这个）。
const BEAT_COMM_FAIL_LIMIT = 3

// 服务端判定 claim 失效（cookie/冷却接口被拒等），runTrack 捕获后停止整个任务
class ClaimLostError extends Error {}

// 统一服务端请求：拼 URL/鉴权头，401 统一走 handleUnauthorized
async function serverApi(path, { method = 'GET', body, params } = {}) {
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) return { ok: false, status: 0, data: null }
  let url = serverUrl + path
  if (params) {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== null && v !== undefined && v !== '')
      .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&')
    if (qs) url += '?' + qs
  }
  const headers = { Authorization: 'Bearer ' + token }
  if (body) headers['Content-Type'] = 'application/json'
  const r = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined })
  let data = null
  try { data = await r.json() } catch (e) {}
  if (r.status === 401) {
    await handleUnauthorized((data && data.detail) || '登录已失效，请重新登录')
    return { ok: false, status: 401, data }
  }
  return { ok: r.ok, status: r.status, data }
}

async function loadClaimState() {
  const o = await new Promise(r => chrome.storage.local.get([STORAGE_CLAIM], o => r(o)))
  claimState = o[STORAGE_CLAIM] || null
  if (claimState) console.log('[plugin] 恢复 claim 状态', claimState.taskId)
}
async function saveClaimState() {
  await new Promise(r => chrome.storage.local.set({ [STORAGE_CLAIM]: claimState }, r))
}
async function clearClaimState() {
  claimState = null
  await new Promise(r => chrome.storage.local.remove([STORAGE_CLAIM], r))
}

// claim 一本书（指定 task_id 或让服务端选最早的 pending）
async function claimTask(taskId) {
  const path = taskId
    ? `/api/extension/tasks/${encodeURIComponent(taskId)}/claim`
    : '/api/extension/tasks/claim'
  let res
  try {
    res = await serverApi(path, { method: 'POST', body: {} })
  } catch (e) {
    return { error: '无法连接服务器: ' + e.message }
  }
  if (res.status === 409) return { busy: true, holder: res.data && res.data.holder }
  if (!res.ok || !res.data || !res.data.success) {
    return { error: (res.data && (res.data.error || res.data.detail)) || `claim 失败 (${res.status})` }
  }
  const claimed = res.data.task
  if (!claimed || !claimed.task_id || !claimed.claim_id) {
    return { error: 'claim 响应异常（缺少 task/claim_id）' }
  }
  // 合并进本地任务：保留本地已完成/下载中的 track 状态（SW 重启续传 / 重 claim 场景）
  await withQueue(async () => {
    const old = await getTask(claimed.task_id)
    const tracks = (claimed.tracks || []).map(tr => {
      const ot = old && old.tracks
        ? old.tracks.find(x => String(x.track_id) === String(tr.track_id)) : null
      if (ot && ot.status !== 'pending') {
        return { ...tr, status: ot.status, downloadId: ot.downloadId || null,
                 filename: ot.filename || null, error: ot.error || '' }
      }
      return { ...tr, status: 'pending', error: '', downloadId: null }
    })
    const newTask = {
      ...claimed,
      tracks,
      status: 'running',
      paused: false,
      frozen: false,
      claim_id: claimed.claim_id,
      errorMsg: '',
      createdAt: (old && old.createdAt) || Date.now(),
    }
    recomputeProgress(newTask)
    const idx = await getIndex()
    await setTask(newTask)
    upsertIndexEntry(idx, newTask)
    await setIndex(idx)
  })
  claimState = {
    taskId: claimed.task_id,
    claimId: claimed.claim_id,
    leaseSeconds: claimed.lease_seconds || 300,
    lastBeatAt: Date.now(),
    beatFails: 0,
  }
  await saveClaimState()
  console.log('[plugin] claim 成功', claimed.task_id, 'claim=', claimed.claim_id)
  return { ok: true, task: claimed }
}

// 心跳续约。返回 ok | comm-fail（网络失败）| lost（已被 handleClaimLost 处理）| gone（登录失效）
async function heartbeatClaim() {
  if (!claimState) return 'no-claim'
  let res
  try {
    res = await serverApi(`/api/extension/tasks/${encodeURIComponent(claimState.taskId)}/heartbeat`,
      { method: 'POST', body: { claim_id: claimState.claimId } })
  } catch (e) {
    claimState.beatFails = (claimState.beatFails || 0) + 1
    if (claimState.beatFails >= BEAT_COMM_FAIL_LIMIT) {
      console.warn('[plugin] 心跳长期不可达，租约必然已过期')
      await handleClaimLost('心跳长期不可达，租约已过期')
    }
    return 'comm-fail'
  }
  if (res.status === 401) {   // handleUnauthorized 已清登录态
    await clearClaimState()
    return 'gone'
  }
  if (res.ok && res.data && res.data.success) {
    claimState.lastBeatAt = Date.now()
    claimState.beatFails = 0
    if (res.data.lease_seconds) claimState.leaseSeconds = res.data.lease_seconds
    await saveClaimState()
    return 'ok'
  }
  await handleClaimLost((res.data && (res.data.error || res.data.detail)) || `心跳被拒 (${res.status})`)
  return 'lost'
}

async function maybeHeartbeat(force = false) {
  if (!claimState) return
  if (!force && Date.now() - claimState.lastBeatAt < SERVER_BEAT_INTERVAL_MS) return
  await heartbeatClaim()
}

// claim 失效：停止该任务全部本地下载（另一设备可能接管同一任务，避免双份落盘），
// 并与服务器重新同步该任务的归属状态
async function handleClaimLost(reason) {
  if (!claimState) return
  const taskId = claimState.taskId
  await clearClaimState()
  // 先拉一次最新任务列表，保证同步判断准确
  try {
    const r = await serverApi('/api/extension/tasks', {})
    if (r.ok && r.data && Array.isArray(r.data.tasks)) lastServerTasks = r.data.tasks
  } catch (e) {}
  const cancelIds = []
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) cancelIds.push(tr.downloadId)
      if (tr.status === 'resolving' || tr.status === 'downloading') {
        tr.status = 'pending'; tr.error = ''; tr.downloadId = null
      }
      inflight.delete(taskId + '/' + tr.track_id)
    }
    const serverPending = lastServerTasks.filter(t => t.status === 'pending').map(t => t.task_id)
    const serverRunning = lastServerTasks.filter(t => t.status === 'running').map(t => t.task_id)
    if (serverPending.includes(taskId)) {
      task.status = 'pending'; task.claim_id = null; task.frozen = false
    } else if (serverRunning.includes(taskId)) {
      task.status = 'running'; task.claim_id = null; task.frozen = true
      task.errorMsg = '其他设备正在下载此任务'
    } else {
      // 服务器侧已终态（done/cancelled）：本地标记结束，避免残留
      task.status = 'done'; task.claim_id = null; task.frozen = false
      if (!task.errorMsg) task.errorMsg = '下载中断（租约失效），任务可能已由服务器侧完成'
      recomputeProgress(task)
    }
    const idx = await getIndex()
    await setTask(task)
    upsertIndexEntry(idx, task)
    await setIndex(idx)
  })
  for (const id of cancelIds) { try { await chrome.downloads.cancel(id) } catch (e) {} }
  console.warn('[plugin] claim 失效，停止本地下载:', taskId, reason)
}

// 当前 claim 的所有集都已终态 → 上报 complete（释放服务器侧下载槽）
async function completeClaim() {
  if (!claimState) return false
  const task = await getTask(claimState.taskId)
  if (!task) { await clearClaimState(); return true }
  recomputeProgress(task)
  const failedList = task.tracks.filter(t => t.status === 'error')
    .map(t => ({ episode: t.episode_num, title: t.title, error: t.error || '下载失败' }))
  let res
  try {
    res = await serverApi(`/api/extension/tasks/${encodeURIComponent(claimState.taskId)}/complete`, {
      method: 'POST',
      body: {
        claim_id: claimState.claimId,
        progress: task.progress,
        failed_list: failedList,
        error: failedList.length ? `${failedList.length} 集下载失败` : '',
      },
    })
  } catch (e) {
    console.warn('[plugin] complete 请求失败，下轮重试', e.message)
    return false
  }
  if (res.ok && res.data && res.data.success) {
    await updateTask(task.task_id, { status: 'done', claim_id: null, errorMsg: '' })
    notifyTaskDone(task)
    await clearClaimState()
    return true
  }
  if (res.status === 409) {
    await handleClaimLost((res.data && res.data.error) || 'complete 被服务器拒绝')
    return false
  }
  console.warn('[plugin] complete 被拒绝:', res.status, res.data && res.data.error)
  return false
}

// 每 tick 一次的 claim 状态机：
//   有 claim → 心跳 →（完成检测 → complete → 立即 claim 下一本）
//   无 claim → 按「显式队列 > 自动下载 > 用户显式请求」claim 下一本
async function tickClaim() {
  const { token } = await cfg()
  if (!token) return
  let guard = 0
  while (guard++ < 4) {   // 一拍内允许"完成→claim 下一本"链式处理（限次防死循环）
    if (claimState) {
      const hb = await heartbeatClaim()
      if (hb === 'lost' || hb === 'gone') return
      const task = await getTask(claimState.taskId)
      if (!task || task.status === 'done' || task.status === 'cancelled') {
        if (claimState) await clearClaimState()
        continue
      }
      if (task.status === 'running' && !task.paused && task.tracks.length) {
        const allTerminal = task.tracks.every(t => t.status === 'done' || t.status === 'error')
        if (allTerminal) {
          const done = await completeClaim()
          if (done) continue    // 立即 claim 下一本
          return                // complete 暂败（网络抖动）→ 下轮再报
        }
      }
      return                    // 当前书仍在下载
    }
    // 无活动 claim
    const s = await getSettings()
    if (!s.autoDownload && !claimPending && !claimQueue.length) return
    // 1) 显式队列（用户点过"开始/重试"但当时槽忙）
    if (claimQueue.length) {
      while (claimQueue.length) {
        const cand = claimQueue.shift()
        const r = await claimTask(cand)
        if (r.ok) { await pump(); return }
        if (r.busy) { claimQueue.unshift(cand); return }   // 槽仍忙：下 tick 再试
        // 其他错误（任务已终态/不存在）：丢弃该项
      }
      return
    }
    // 2) 服务端 pending 队列（自动下载 / 用户点"全部下载"）
    const pendingServer = (lastServerTasks || []).filter(t => t.status === 'pending')
    if (!pendingServer.length) { claimPending = false; return }
    const idx = await getIndex()
    const localPending = idx
      .filter(i => i.status === 'pending' && pendingServer.some(t => t.task_id === i.task_id))
      .sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))
    const pickId = localPending.length ? localPending[0].task_id : pendingServer[0].task_id
    const r = await claimTask(pickId)
    if (r.ok) { claimPending = false; await pump(); return }
    if (r.busy) return         // 槽忙：下 tick 重试（claimPending 保持）
    claimPending = false
    return
  }
}

// ── 事件注册 ──
chrome.alarms.onAlarm.addListener(handleAlarm)
chrome.runtime.onMessage.addListener(handleMessage)
chrome.downloads.onChanged.addListener(handleDownloadChanged)

// 启动：静默下载 UI → 恢复 claim 状态 → 拉取任务 → 校正历史下载状态（SW 被杀重启场景）→ claim 状态机 → 派发续传
applySilentMode()
pollAnnouncement().catch(() => {})
loadClaimState()
  .then(() => pollBackend())
  .then(() => reconcileDownloads())
  .then(() => tickClaim())
  .then(() => pump())
  .catch(e => console.error('[plugin] 启动流程 error', e))
chrome.alarms.create('poll', { periodInMinutes: 1 })

function handleAlarm(alarm) {
  if (alarm.name === 'poll') {
    pollAnnouncement().catch(() => {})
    pollBackend()
      .then(() => reconcileDownloads())
      .then(() => tickClaim())
      .then(() => pump())
      .catch(e => console.error('[plugin] poll error', e))
  }
  if (alarm.name === 'keepAlive') {
    // keepAlive 心跳，仅用于保持 SW 存活，无实际业务逻辑
    // console.log('[plugin] keepAlive tick, inflight=', inflight.size)
  }
}

// 统一应答：保证每个消息都有响应（popup 侧 swCall 不会再永久等待）
function respond(sendResponse, promise) {
  promise
    .then(r => sendResponse(r === undefined ? { ok: true } : r))
    .catch(e => sendResponse({ ok: false, error: (e && e.message) || String(e) }))
  return true
}

function handleMessage(msg, sender, sendResponse) {
  if (!msg || msg.target !== 'sw') return false
  switch (msg.type) {
    case 'pullTasks': return respond(sendResponse, pollBackend().then(() => ({ ok: true })))
    case 'downloadAll': return respond(sendResponse, approveAndProcess().then(() => ({ ok: true })))
    case 'getQueue': return respond(sendResponse, getQueue().then(q => ({ queue: q })))
    case 'getSettings': return respond(sendResponse, getSettings().then(s => ({ settings: s })))
    case 'setSettings': return respond(sendResponse, (async () => {
      const s = await getSettings()
      const patch = (msg.settings && typeof msg.settings === 'object') ? msg.settings : { [msg.key]: msg.value }
      await setSettings({ ...s, ...patch })
      if ('silentDownload' in patch) await applySilentMode()
      return { ok: true }
    })())
    case 'clearDone': return respond(sendResponse, clearTasks(['done']))
    case 'clearEnded': return respond(sendResponse, clearTasks(['done', 'cancelled']))
    case 'pauseTask': return respond(sendResponse, pauseTask(msg.taskId))
    case 'resumeTask': return respond(sendResponse, resumeTask(msg.taskId))
    case 'cancelTask': return respond(sendResponse, cancelTask(msg.taskId))
    case 'retryFailed': return respond(sendResponse, retryFailed(msg.taskId))
    case 'retryAllFailed': return respond(sendResponse, retryAllFailed())
    case 'pauseAll': return respond(sendResponse, pauseAll())
    case 'resumeAll': return respond(sendResponse, resumeAll())
    case 'startTask': return respond(sendResponse, startTask(msg.taskId))
    case 'pullAnnouncement': return respond(sendResponse, pollAnnouncement().then(a => ({ ok: true, announcement: a })))
    case 'ping': return respond(sendResponse, pingServer())
  }
  return false
}

// ── 公告拉取 ──
async function pollAnnouncement() {
  const { serverUrl } = await cfg()
  const headers = await authHeaders()
  if (!serverUrl || !headers) return null
  try {
    const r = await fetch(`${serverUrl}/api/announcements/current`, { headers })
    if (r.status === 401) { await handleUnauthorized((await r.json().catch(() => null))?.detail); return null }
    const data = await r.json()
    const ann = (data && data.success && data.announcement) ? data.announcement : null
    await new Promise(res => chrome.storage.local.set({ xm_announcement: ann }, res))
    return ann
  } catch (e) {
    return null
  }
}

// ── 后端轮询：拉取 pending（可 claim）与 running（已被 claim）任务 ──
// 0.7.0 新流程不再 ack：任务生命周期由 claim/heartbeat/complete/cancel 驱动，
// pending 任务会持续出现在列表中直到被 claim（本插件幂等去重，重复出现无副作用）。
async function pollBackend() {
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) return
  let data
  try {
    const r = await fetch(`${serverUrl}/api/extension/tasks`, { headers: { Authorization: 'Bearer ' + token } })
    if (r.status === 401) {
      // 被顶下线/解绑/过期/风控：清本地登录态并提示，停止本轮拉取
      await handleUnauthorized((await r.json().catch(() => null))?.detail)
      lastServerTasks = []
      return
    }
    data = await r.json()
    if (!data.success) { console.warn('[plugin] poll backend failed', data); return }
  } catch (e) { console.error('[plugin] poll error', e); return }

  lastServerTasks = data.tasks || []
  const claimedId = claimState ? claimState.taskId : null

  await withQueue(async () => {
    const idx = await getIndex()
    let changed = false

    // 1) 服务器 → 本地：新任务加入本地队列
    //    本设备 claim 的任务已在本地（running）；其他设备 claim 的任务以 frozen 展示
    for (const t of lastServerTasks) {
      if (idx.some(existing => existing.task_id === t.task_id)) continue
      const tracks = (t.tracks || []).map(tr => ({
        ...tr, status: 'pending', error: '', downloadId: null,
      }))
      const frozen = t.status === 'running' && t.task_id !== claimedId
      const newTask = {
        ...t, tracks,
        status: frozen ? 'running' : 'pending',
        paused: false,
        frozen: frozen,
        claim_id: null,
        errorMsg: frozen ? '其他设备正在下载此任务' : '',
        progress: { total: tracks.length, done: 0, failed: 0 },
        createdAt: Date.now(),
      }
      await setTask(newTask)
      idx.push({
        task_id: t.task_id,
        album_id: t.album_id,
        album_title: t.album_title,
        source: t.source,
        status: newTask.status,
        paused: false,
        frozen: frozen,
        createdAt: Date.now(),
      })
      changed = true
    }

    // 2) 服务器状态 → 本地同步（仅处理非本设备 claim 的任务）
    for (const item of [...idx]) {
      const st = lastServerTasks.find(t => t.task_id === item.task_id)
      const task = await getTask(item.task_id)
      if (!task) continue
      if (task.task_id === claimedId) continue
      if (!st) {
        // 服务器侧已消失（done/cancelled/被删）：本地 pending 的移除；
        // 本地 done/cancelled 保留展示；frozen 展示任务保留（等待用户处理）
        if (task.status === 'pending') {
          await delTask(item.task_id)
          idx.splice(idx.indexOf(item), 1)
          changed = true
        }
        continue
      }
      let taskDirty = false
      if (st.status === 'running' && task.status === 'pending') {
        // 其他设备 claim 了它：本地转 frozen 展示，不下载
        task.status = 'running'; task.frozen = true
        task.errorMsg = '其他设备正在下载此任务'
        item.status = 'running'; item.frozen = true
        taskDirty = true; changed = true
      } else if (st.status === 'pending' && task.status === 'running' && task.frozen) {
        // 其他设备释放了：回到本地队列，等待本设备 claim
        task.status = 'pending'; task.frozen = false; task.errorMsg = ''
        item.status = 'pending'; item.frozen = false
        taskDirty = true; changed = true
      }
      if (taskDirty) {
        await setTask(task)
        upsertIndexEntry(idx, task)
      }
    }
    if (changed) await setIndex(idx)
  })

  // 随路心跳（节流 30s）
  if (claimedId) await maybeHeartbeat().catch(e => console.warn('[plugin] poll 心跳异常', e))
  if (claimedId || lastServerTasks.length) pump()
}

// ── 启动续传：校正 SW 被杀前的下载状态 ──
// 修复：之前在 withQueue 锁内串行执行 chrome.downloads.search，导致锁长时间占用，进度卡住
// 新实现：两阶段，先快照需要检查的 track，再锁外查询，最后锁内批量更新，并重建 inflight
async function reconcileDownloads() {
  const finishedList = []
  let snapshot = [] // [{taskId, trackId, downloadId, status}]

  // 阶段1：快照（短锁）
  await withQueue(async () => {
    const idx = await getIndex()
    for (const item of idx) {
      if (item.status === 'done' || item.status === 'cancelled') continue
      const task = await getTask(item.task_id)
      if (!task) continue
      for (const tr of task.tracks) {
        if (tr.status === 'downloading' || tr.status === 'resolving') {
          snapshot.push({
            taskId: task.task_id,
            trackId: tr.track_id,
            downloadId: tr.downloadId,
            status: tr.status,
          })
        }
      }
    }
  })

  if (!snapshot.length) {
    // 即使没有需要校正的，也要重建 inflight 为空（SW 重启场景）
    inflight.clear()
    return
  }

  // 阶段2：锁外查询下载状态（避免阻塞队列锁）
  const searchResults = new Map() // downloadId -> downloadItem | null
  for (const s of snapshot) {
    if (s.downloadId == null) {
      searchResults.set(`${s.taskId}/${s.trackId}`, null)
      continue
    }
    try {
      const items = await chrome.downloads.search({ id: s.downloadId })
      searchResults.set(s.downloadId, items && items[0] ? items[0] : null)
    } catch (e) {
      searchResults.set(s.downloadId, null)
    }
  }

  // 阶段3：批量更新（短锁）+ 重建 inflight
  // 注意：stage1 和 stage3 之间可能有 handleDownloadChanged 抢先更新了状态
  // 因此这里要做防御性检查：若 track 已变为 done/error，则不再覆盖为 downloading/pending
  await withQueue(async () => {
    const idx = await getIndex()
    const newInflightFromSearch = new Set()
    let anyChanged = false

    for (const item of idx) {
      if (item.status === 'done' || item.status === 'cancelled') continue
      const task = await getTask(item.task_id)
      if (!task) continue
      let taskChanged = false

      for (const tr of task.tracks) {
        const key = task.task_id + '/' + tr.track_id
        const snap = snapshot.find(x => x.taskId === task.task_id && x.trackId === tr.track_id)
        if (!snap) continue

        if (tr.status === 'done' || tr.status === 'error') continue

        if (snap.downloadId == null) {
          if (tr.status === 'downloading' || tr.status === 'resolving') {
            tr.status = 'pending'; tr.error = ''; tr.downloadId = null
            taskChanged = true
          }
          continue
        }

        const it = searchResults.get(snap.downloadId)
        if (!it || it.state === 'interrupted') {
          if (tr.status === 'downloading' || tr.status === 'resolving') {
            tr.status = 'pending'; tr.downloadId = null; tr.error = ''
            taskChanged = true
          }
        } else if (it.state === 'complete') {
          if (tr.status !== 'done') { tr.status = 'done'; tr.error = ''; taskChanged = true }
        } else if (it.state === 'in_progress') {
          if (tr.status === 'downloading' || tr.status === 'resolving' || tr.status === 'pending') {
            if (tr.status !== 'downloading') { tr.status = 'downloading'; taskChanged = true }
            newInflightFromSearch.add(key)
          }
        }
      }

      if (finalizeTask(task)) { finishedList.push({ ...task }); taskChanged = true }
      if (taskChanged) {
        await setTask(task)
        upsertIndexEntry(idx, task)
        anyChanged = true
      }
    }

    // 重建 inflight：基于最终存储状态，包含所有仍为 downloading/resolving 的 track
    // 避免丢失刚派发但不在快照中的任务，同时避免旧快照覆盖导致泄漏
    const rebuilt = new Set()
    for (const item of idx) {
      if (item.status !== 'running' || item.paused) continue
      const task = await getTask(item.task_id)
      if (!task) continue
      for (const tr of task.tracks) {
        if (tr.status === 'downloading' || tr.status === 'resolving') {
          rebuilt.add(task.task_id + '/' + tr.track_id)
        }
      }
    }
    for (const k of newInflightFromSearch) rebuilt.add(k)

    inflight.clear()
    for (const k of rebuilt) inflight.add(k)

    if (anyChanged) {
      await setIndex(idx)
      console.log('[plugin] reconcileDownloads corrected state, inflight=', inflight.size)
    }
  })

  for (const t of finishedList) notifyTaskDone(t)
  maybeStopKeepAlive()
}

// 手动「全部下载」：请求 claim 下一本（全局下载槽：同一时间只有一本在下载），
// 其余 pending 由 tickClaim 在本本完成后自动接力
async function approveAndProcess() {
  claimPending = true
  await tickClaim()
  await pump()
}

// ── 下载调度器（pump 式：每次读取最新队列状态，暂停/取消立即生效）──
const inflight = new Set()          // `${taskId}/${trackId}`，防重复派发，唯一并发计数来源
let pumping = false
let pumpQueued = false

// ── 书籍级选取：全局下载槽 ⇒ 只有本设备 claim 到的任务（claimState）允许下载 ──
async function pickBookDispatch() {
  if (!claimState) return null
  const task = await getTask(claimState.taskId)
  if (!task || task.status !== 'running' || task.paused) return null
  for (const tr of task.tracks) {
    if (tr.status === 'pending' && !inflight.has(claimState.taskId + '/' + tr.track_id)) {
      return { taskId: claimState.taskId, trackId: tr.track_id }
    }
  }
  return null
}

async function pump() {
  if (pumping) { pumpQueued = true; return }
  pumping = true
  let dispatched = false
  try {
    const settings = await getSettings()
    const limit = Math.max(1, Math.min(8, settings.concurrency || 1))
    while (inflight.size < limit) {
      const pick = await pickBookDispatch()
      if (!pick) break
      const key = pick.taskId + '/' + pick.trackId
      if (inflight.has(key)) break // 双重保险，防重复
      inflight.add(key)
      dispatched = true
      // 先落盘为 resolving，杜绝 SW 重启/并发导致的重复派发
      await updateTrack(pick.taskId, pick.trackId, { status: 'resolving', error: '' })
      runTrack(pick.taskId, pick.trackId)
        .catch(e => console.error('[plugin] runTrack error', e))
        .finally(() => {
          inflight.delete(key)
          // 用 setTimeout 避免同步递归导致调用栈过深，特别是在大量小文件快速完成时
          setTimeout(() => pump(), 0)
        })
    }
  } finally {
    pumping = false
    if (pumpQueued) {
      pumpQueued = false
      // 延迟一小会儿再触发，避免同步递归导致调用栈过深
      setTimeout(() => pump(), 0)
    } else if (!dispatched) {
      maybeStopKeepAlive()
    }
  }
  if (dispatched) startKeepAlive()
  // 随路心跳（节流 30s）：下载越活跃，租约越安全
  if (claimState) maybeHeartbeat().catch(() => {})
}

// 派发前闸口：读取最新任务状态（暂停/取消对未开始的集立即生效）
async function gateCheck(taskId, trackId) {
  const task = await getTask(taskId)
  if (!task) return 'abort'
  if (task.status === 'cancelled') return 'abort'
  if (task.status !== 'running' || task.paused) {
    await updateTrack(taskId, trackId, { status: 'pending', error: '' })
    return 'abort'
  }
  return 'go'
}

const trackDoneResolvers = new Map()   // downloadId -> resolve
const pendingTerminals = new Map()     // downloadId -> {time}，LRU 淘汰，避免无界增长

function awaitTrackDone(downloadId) {
  return new Promise(res => {
    trackDoneResolvers.set(downloadId, res)
    if (pendingTerminals.has(downloadId)) {
      pendingTerminals.delete(downloadId)
      res()
    }
  })
}

function resolveTerminal(downloadId) {
  const r = trackDoneResolvers.get(downloadId)
  if (r) {
    trackDoneResolvers.delete(downloadId)
    r()
  } else {
    // 终端事件先于 resolver 到达，缓存起来，设置上限 LRU 淘汰最旧
    if (pendingTerminals.size >= 1000) {
      // 删除最旧的 100 条
      const keys = Array.from(pendingTerminals.keys()).slice(0, 100)
      for (const k of keys) pendingTerminals.delete(k)
    }
    pendingTerminals.set(downloadId, Date.now())
  }
}

async function runTrack(taskId, trackId) {
  const { serverUrl, token } = await cfg()
  let downloadId = null
  let lastErr = null
  const settings = await getSettings()
  const maxRetry = Math.max(1, Math.min(10, settings.maxRetry || MAX_RETRY))
  const delayMs = Math.max(0, Math.min(120000, settings.downloadDelayMs || 0))
  if (delayMs > 0) await sleep(delayMs)

  for (let attempt = 1; attempt <= maxRetry; attempt++) {
    if (await gateCheck(taskId, trackId) !== 'go') return
    try {
      const task = await getTask(taskId)
      if (!task) return
      const tr = task.tracks.find(t => t.track_id === trackId)
      if (!tr) return
      if (tr.status === 'done' || tr.status === 'downloading') return

      // 防重复落盘守卫：同专辑+同集+同格式已在其他任务中处于非失败状态（pending/resolving/downloading/done）
      // 之前仅检查 done，导致同一专辑被推送两次时产生 (1) 副本
      const idx = await getIndex()
      const otherIds = idx.filter(i => i.task_id !== taskId && i.album_id === task.album_id).map(i => i.task_id)
      if (otherIds.length) {
        const otherTasks = await getTasksBatch(otherIds)
        const dup = otherTasks.find(o =>
          o.tracks.some(t => {
            if (String(t.track_id) !== String(trackId)) return false
            if (t.status === 'error') return false // 失败的不算，可重试
            const fmtA = (t.fmt || o.fmt || 'mp3')
            const fmtB = (tr.fmt || task.fmt || 'mp3')
            return fmtA === fmtB
          })
        )
        if (dup) {
          console.log('[plugin] 跳过重复集（其他任务已存在）', task.album_id, trackId, 'existing in', dup.task_id)
          await updateTrack(taskId, trackId, { status: 'done', error: '' })
          return
        }
      }

      // 锁内二次校验：防止竞态下同一 track 被并发派发两次
      let shouldSkip = false
      await withQueue(async () => {
        const freshTask = await getTask(taskId)
        if (!freshTask) { shouldSkip = true; return }
        const freshTr = freshTask.tracks.find(t => t.track_id === trackId)
        if (!freshTr) { shouldSkip = true; return }
        if (freshTr.status === 'done' || freshTr.status === 'downloading') {
          shouldSkip = true
        }
      })
      if (shouldSkip) return

      const url = await resolveForTrack(task, tr, { serverUrl, token })
      if (!url) throw new Error('解析结果为空')
      const fname = buildFilename(task, tr, settings.downloadPrefix)
      console.log('[plugin] start download', trackId, fname)
      downloadId = await chrome.downloads.download({
        url, filename: fname, conflictAction: 'uniquify', saveAs: false,
      })
      console.log('[plugin] download created id=', downloadId, 'track=', trackId)
      await updateTrack(taskId, trackId, { status: 'downloading', downloadId, error: '' })
      break
    } catch (e) {
      if (e instanceof ClaimLostError) {
        // 服务端判定 claim 失效（cookie/冷却接口被拒）：停止整个任务，不再逐集重试
        console.warn('[plugin] claim 失效，停止任务', taskId, e.message)
        await handleClaimLost(e.message)
        return
      }
      lastErr = e
      console.warn(`[plugin] 第${attempt}次解析/创建下载失败`, trackId, e && e.message)
      if (attempt < maxRetry) { await sleep(attempt * 1500); continue }
      await updateTrack(taskId, trackId, { status: 'error', error: (lastErr && lastErr.message) || String(lastErr) })
      return
    }
  }
  if (downloadId == null) return
  const ok = await Promise.race([
    awaitTrackDone(downloadId).then(() => true),
    sleep(TRACK_TIMEOUT).then(() => false),
  ])
  if (!ok) {
    console.warn('[plugin] 下载超时', downloadId, trackId)
    // 超时后清理 resolver，防止泄漏
    trackDoneResolvers.delete(downloadId)
    pendingTerminals.delete(downloadId)
    await updateTrack(taskId, trackId, { status: 'error', error: '下载超时（30分钟未完成）' })
    try { await chrome.downloads.cancel(downloadId) } catch (e) {}
  } else {
    // 正常完成，resolver 已在 resolveTerminal 中删除，此处防御性清理
    trackDoneResolvers.delete(downloadId)
  }
}

async function resolveForTrack(task, tr, ctx) {
  if (task.source === 'official') {
    const sign = await getSign()
    const accounts = await getXmCookie(task)   // 携带 task_id/claim_id；校验失败抛 ClaimLostError
    if (!accounts || !accounts.length) throw new Error('无可用官方账号（请先在网页「账号」页登录喜马拉雅账号）')
    return await resolveOfficialRotation(tr.track_id, task.quality, sign, accounts, task, ctx)
  }
  const resolver = globalThis.RESOLVERS[task.source]
  if (!resolver) throw new Error(`未实现解析器: ${task.source}（本地下载要求 extension/sources/ 下有同名脚本注册该音源。若后端新增了脚本接口，需在 sources/ 放对应 JS 脚本、并在 sources.config.js 的 PLUGIN_SOURCES 登记；否则请改用网页端「服务器端下载」）`)
  const res = await resolver(tr, { serverUrl: ctx.serverUrl, token: ctx.token, quality: task.quality, fmt: task.fmt, albumId: task.album_id })
  return typeof res === 'string' ? res : (res.url || '')
}

// ── 监听 chrome.downloads 状态变化 ──
function handleDownloadChanged(delta) {
  if (!delta || !delta.id) return
  const id = delta.id
  const terminal = (delta.state && (delta.state.current === 'complete' || delta.state.current === 'interrupted'))
    || (delta.error && delta.error.current)
  if (terminal) {
    console.log('[plugin] download terminal', id, delta.state && delta.state.current, delta.error && delta.error.current)
    resolveTerminal(id)
  }
  if (!delta.state && !delta.error && !delta.filename) return
  withQueue(async () => {
    const idx = await getIndex()
    let found = false
    for (const item of idx) {
      const task = await getTask(item.task_id)
      if (!task) continue
      const tr = task.tracks.find(t => t.downloadId === id)
      if (!tr) continue
      found = true
      const wasCancelledByUser = task.status === 'cancelled' || (tr.status === 'error' && tr.error === '已取消')
      const patch = {}
      if (delta.state && delta.state.current) {
        if (delta.state.current === 'complete') { patch.status = 'done'; patch.error = '' }
        else if (delta.state.current === 'interrupted' && !wasCancelledByUser) {
          patch.status = 'error'; patch.error = (delta.error && delta.error.current) || '下载中断'
        }
      }
      if (delta.error && delta.error.current && !wasCancelledByUser && delta.error.current !== 'USER_CANCELED') {
        patch.status = 'error'; patch.error = delta.error.current
      }
      if (delta.filename && delta.filename.current) patch.filename = delta.filename.current
      const hadPatch = Object.keys(patch).length > 0
      Object.assign(tr, patch)
      const finished = finalizeTask(task)

      // 清理 inflight，防止 SW 重启后残留
      const key = task.task_id + '/' + tr.track_id
      if (patch.status === 'done' || patch.status === 'error') {
        inflight.delete(key)
      }

      if (hadPatch || finished) {
        await setTask(task)
        upsertIndexEntry(idx, task)
        await setIndex(idx)
        if (delta.state && delta.state.current === 'complete') {
          try { chrome.downloads.erase({ id }).catch(() => {}) } catch (e) {}
        }
        if (finished) notifyTaskDone({ ...task })
      }
      break
    }
    if (!found) {
      // 未匹配到插件任务，可能是用户手动下载，忽略
    }
  }).then(() => maybeStopKeepAlive()).catch(e => console.error('[plugin] onChanged error', e))
}

// ── 下载控制：暂停 / 继续 / 取消 / 重试（全部真正生效）──
async function pauseTask(taskId) {
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    task.paused = true
    const idx = await getIndex()
    await setTask(task)
    upsertIndexEntry(idx, task)
    await setIndex(idx)
  })
  const task = await getTask(taskId)
  if (!task) return
  for (const tr of task.tracks) {
    if (tr.status === 'downloading' && tr.downloadId != null) {
      try { await chrome.downloads.pause(tr.downloadId) } catch (e) {}
    }
  }
}

async function resumeTask(taskId) {
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (task && !task.frozen) {
      task.paused = false
      // 注意：pending 不置 running —— 下载由 claim 驱动（tickClaim）
      const idx = await getIndex()
      await setTask(task)
      upsertIndexEntry(idx, task)
      await setIndex(idx)
    }
  })
  const task = await getTask(taskId)
  if (task) {
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) {
        try { await chrome.downloads.resume(tr.downloadId) } catch (e) {}
      }
    }
  }
  if (task && task.status === 'pending') { claimPending = true; await tickClaim() }
  await pump()
}

async function cancelTask(taskId) {
  // 先通知服务端（新流程）：
  // - 本设备 claim 中的任务 → 带 claim_id 取消并释放下载槽；
  // - 尚未 claim 的 pending 任务 → 不带 claim_id 取消（否则之后会被 tickClaim 再次 claim 下载）；
  // - 其他设备 claim 中的任务（frozen 展示）→ 不动服务端（本地仅取消展示）
  if (claimState && claimState.taskId === taskId) {
    try {
      await serverApi(`/api/extension/tasks/${encodeURIComponent(taskId)}/cancel`,
        { method: 'POST', body: { claim_id: claimState.claimId } })
    } catch (e) { console.warn('[plugin] 服务端取消失败', e) }
    await clearClaimState()
  } else {
    const t0 = await getTask(taskId)
    if (t0 && t0.status === 'pending') {
      try {
        await serverApi(`/api/extension/tasks/${encodeURIComponent(taskId)}/cancel`,
          { method: 'POST', body: {} })
      } catch (e) { /* 服务器不可达时仅本地取消 */ }
    }
  }
  const ids = []
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) ids.push(tr.downloadId)
      if (tr.status === 'pending' || tr.status === 'downloading' || tr.status === 'resolving') {
        tr.status = 'error'; tr.error = '已取消'
        inflight.delete(task.task_id + '/' + tr.track_id)
      }
    }
    task.status = 'cancelled'
    task.paused = false
    task.claim_id = null
    recomputeProgress(task)
    const idx = await getIndex()
    await setTask(task)
    upsertIndexEntry(idx, task)
    await setIndex(idx)
  })
  for (const id of ids) { try { await chrome.downloads.cancel(id) } catch (e) {} }
  maybeStopKeepAlive()
}

async function retryFailed(taskId) {
  const task = await getTask(taskId)
  if (!task || task.status === 'cancelled') return
  if (task.frozen) throw new Error('此任务正由其他设备下载，请在该设备上重试')
  let has = false
  for (const tr of task.tracks) {
    if (tr.status === 'error') { tr.status = 'pending'; tr.error = ''; tr.downloadId = null; has = true }
  }
  if (!has) return
  await withQueue(async () => {
    const idx = await getIndex()
    await setTask(task)
    upsertIndexEntry(idx, task)
    await setIndex(idx)
  })
  // 本设备 claim 仍有效：直接续传失败集
  if (task.status === 'running' && claimState && claimState.taskId === taskId) {
    await pump()
    return
  }
  // done（有失败集）或 pending：需要（重新）claim 下载槽
  if (claimState) {
    // 当前另一本书在下载：排队，本本完成后自动接力
    claimQueue.push(taskId)
    return
  }
  const r = await claimTask(taskId)
  if (r.busy) throw new Error('当前卡密已有下载任务进行中，请等待完成后再开始下一本')
  if (r.error) throw new Error(r.error)
  await pump()
}

async function retryAllFailed() {
  const idx = await getIndex()
  const candidates = []
  for (const item of idx) {
    if (item.status === 'cancelled' || item.frozen) continue
    const task = await getTask(item.task_id)
    if (!task) continue
    if (task.tracks.some(tr => tr.status === 'error')) candidates.push(item.task_id)
  }
  if (!candidates.length) return
  // 全局下载槽：同一时间只能重试一本；第一本立即（或排队）处理，
  // 其余加入 claimQueue，由 tickClaim 在完成后自动接力
  await retryFailed(candidates[0])
  for (let i = 1; i < candidates.length; i++) claimQueue.push(candidates[i])
}

async function pauseAll() {
  const idx = await getIndex()
  const runningIds = idx.filter(i => i.status === 'running' && !i.paused).map(i => i.task_id)
  for (const id of runningIds) await pauseTask(id)
}

async function startTask(taskId) {
  const task = await getTask(taskId)
  if (!task) return { ok: false, error: '任务不存在' }
  if (task.status === 'cancelled') return { ok: false, error: '任务已取消' }
  if (task.status === 'done') {
    // 已完成但有失败集 → 走重试（重新 claim）
    if (task.tracks.some(t => t.status === 'error')) return await retryFailed(taskId)
    return { ok: false, error: '任务已完成' }
  }
  if (task.frozen) return { ok: false, error: '此任务正由其他设备下载' }
  if (task.status === 'running' && claimState && claimState.taskId === taskId) {
    // 本设备 claim 中但被暂停 → 恢复
    await withQueue(async () => {
      task.paused = false
      const idx = await getIndex()
      await setTask(task)
      upsertIndexEntry(idx, task)
      await setIndex(idx)
    })
    await pump()
    return { ok: true }
  }
  // pending（或本地 running 但 claim 已失效）→ (重新) claim 下载槽
  if (claimState && claimState.taskId !== taskId) {
    claimQueue.push(taskId)
    return { ok: true, queued: true, message: '当前卡密已有下载任务进行中，此本将在其完成后自动开始' }
  }
  const r = await claimTask(taskId)
  if (r.ok) { await pump(); return { ok: true } }
  if (r.busy) return { ok: false, error: '当前卡密已有下载任务进行中，请等待完成后再开始下一本' }
  return { ok: false, error: r.error }
}

async function resumeAll() {
  // 0.7.0：pending 任务不再直接置 running（下载由 claim 驱动），
  // 只做两件事：恢复本设备 claim 任务/其他 running 任务的暂停；并请求 claim 下一本。
  await withQueue(async () => {
    const idx = await getIndex()
    let changed = false
    for (const item of idx) {
      if (item.status === 'done' || item.status === 'cancelled') continue
      if (item.status === 'running' && item.paused) {
        const task = await getTask(item.task_id)
        if (task && !task.frozen) {
          task.paused = false
          await setTask(task)
          upsertIndexEntry(idx, task)
          changed = true
        }
      }
    }
    if (changed) await setIndex(idx)
  })
  const idx = await getIndex()
  const runningIds = idx.filter(i => i.status === 'running' && !i.frozen).map(i => i.task_id)
  const tasks = await getTasksBatch(runningIds)
  for (const task of tasks) {
    if (task.status !== 'running') continue
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) {
        try { await chrome.downloads.resume(tr.downloadId) } catch (e) {}
      }
    }
  }
  // 有 pending 未开始 → 请求 claim（全局下载槽串行接力）
  if (idx.some(i => i.status === 'pending')) claimPending = true
  await tickClaim()
  await pump()
}

async function clearTasks(statuses) {
  return withQueue(async () => {
    const idx = await getIndex()
    const cleared = idx.filter(item => statuses.includes(item.status)).length
    const remaining = idx.filter(item => !statuses.includes(item.status))
    const removedIds = idx.filter(item => statuses.includes(item.status)).map(item => item.task_id)
    for (const id of removedIds) {
      await delTask(id)
      // 清理 inflight 中属于被删除任务的条目
      for (const k of Array.from(inflight)) {
        if (k.startsWith(id + '/')) inflight.delete(k)
      }
    }
    await setIndex(remaining)
    return { ok: true, cleared }
  })
}

// ── keepAlive：下载进行中保持 service worker 存活 ──
function startKeepAlive() {
  if (keepAliveActive) return
  keepAliveActive = true
  chrome.alarms.create('keepAlive', { periodInMinutes: 1 })
}
function stopKeepAlive() {
  if (!keepAliveActive) return
  keepAliveActive = false
  chrome.alarms.clear('keepAlive')
}
async function maybeStopKeepAlive() {
  if (inflight.size > 0) return
  if (claimState) return   // claim 进行中：保持 SW 存活（心跳/租约依赖它）
  const idx = await getIndex()
  const anyActive = idx.some(item => item.status === 'running' && !item.paused && !item.frozen)
  if (!anyActive) stopKeepAlive()
}

// ── 静默下载：关闭浏览器自带下载 UI ──
async function applySilentMode() {
  try {
    const s = await getSettings()
    const enabled = !s.silentDownload
    if (chrome.downloads.setUiOptions) {
      await chrome.downloads.setUiOptions({ enabled })
      console.log('[plugin] 下载 UI 已', enabled ? '恢复' : '隐藏（静默下载中）')
    } else if (chrome.downloads.setShelfEnabled) {
      chrome.downloads.setShelfEnabled(enabled)
    }
  } catch (e) { console.error('[plugin] setUiOptions 失败（检查 manifest 是否有 downloads.ui 权限）:', e) }
}

// ── 工具函数 ──
function buildFilename(task, tr, prefix) {
  // 文件名安全 + 长度限制，避免 OS 路径过长
  const sanitize = (s) => (s || '').replace(/[\\/:*?"<>|]/g, '_').trim()
  const maxLen = 80 // 单段最大长度
  const trunc = (s, len) => {
    s = sanitize(s)
    if (s.length <= len) return s
    return s.slice(0, len - 3) + '...'
  }

  const album = trunc(task.album_title || ('album_' + task.album_id), maxLen) || 'album'
  const ep = tr.episode_num != null ? String(tr.episode_num).padStart(4, '0') : (tr.track_id || '')
  const title = trunc(tr.title || '', maxLen) || tr.track_id || 'track'
  const ext = (tr.fmt || task.fmt || 'mp3').replace(/[^a-z0-9]/gi, '').slice(0, 6) || 'mp3'
  const base = `${album}/${ep} ${title}.${ext}`
  if (!prefix) return base
  const p = trunc(prefix, 40).replace(/\/$/, '')
  return `${p}/${base}`
}

// ── 官方 xm-sign：通过 Offscreen Document 调用 du_web_sdk ──
async function ensureOffscreen() {
  try {
    if (typeof chrome.offscreen.hasDocument === 'function') {
      const has = await chrome.offscreen.hasDocument()
      if (has) {
        offscreenReady = true
        return
      } else {
        // 文档不存在，重置标志以便重建
        offscreenReady = false
      }
    }
  } catch (_) {}
  if (offscreenReady) return
  try {
    const reason = (chrome.offscreen.Reason && chrome.offscreen.Reason.DOM_SCRAPING) || 'DOM_SCRAPING'
    await chrome.offscreen.createDocument({
      url: 'offscreen.html',
      reasons: [reason],
      justification: '加载 du_web_sdk 计算 xm-sign',
    })
    offscreenReady = true
  } catch (e) {
    const m = (e && e.message) || String(e)
    if (/already|single|only/i.test(m)) { offscreenReady = true; return }
    console.error('[plugin] create offscreen failed', e)
    throw new Error('无法创建离屏文档（xm-sign 计算环境）：' + m)
  }
}

async function getSign() {
  await ensureOffscreen()
  for (let i = 0; i < 4; i++) {
    try {
      const sign = await chrome.runtime.sendMessage({ target: 'offscreen', type: 'sign' })
      if (sign && sign.error) throw new Error(sign.error)
      if (!sign || typeof sign !== 'string') throw new Error('xm-sign 为空')
      return sign
    } catch (e) {
      const msg = (e && e.message) || String(e)
      if (/Receiving end does not exist|could not establish|message channel|The receiver/.test(msg)) {
        if (i < 3) {
          offscreenReady = false
          await sleep(400 * (i + 1))
          await ensureOffscreen().catch(() => {})
          continue
        }
      }
      throw e
    }
  }
}

// ── 官方账号 cookie：从后端下发（仅当前登录卡密下的可用账号列表）──
// 0.7.0：必须携带当前任务的 task_id + claim_id，服务端 5 项校验（本地槽持有/task 匹配/
// claim 匹配/running/source=official）。缓存按 claim 维度（key=task_id:claim_id）5 分钟，
// 避免"上一个 claim 的缓存"被用于新任务导致校验失败或串号。
let xmCookieCache = null   // {key, ts, accounts}
function xmCookieKey(task) {
  return ((task && task.task_id) || '') + ':' + ((task && task.claim_id) || '')
}
async function getXmCookie(task) {
  const now = Date.now()
  const key = xmCookieKey(task)
  if (xmCookieCache && xmCookieCache.key === key && now - xmCookieCache.ts < 5 * 60 * 1000) {
    return xmCookieCache.accounts
  }
  if (!task || !task.task_id) {
    xmCookieCache = { key, ts: now, accounts: [] }
    return []
  }
  let res
  try {
    res = await serverApi('/api/extension/xm-cookie', {
      params: { task_id: task.task_id, claim_id: task.claim_id || '' },
    })
  } catch (e) {
    console.warn('[plugin] getXmCookie 网络失败', e)
    xmCookieCache = { key, ts: now, accounts: [] }
    return []
  }
  if (res.status === 401) {
    xmCookieCache = { key, ts: now, accounts: [] }   // handleUnauthorized 已在 serverApi 内处理
    return []
  }
  if (!res.ok) {
    // 400/403/409：服务端 claim 校验失败（租约失效/被其他设备持有/任务非官方源）
    throw new ClaimLostError((res.data && (res.data.error || res.data.detail))
      || `官方 cookie 被服务器拒绝 (${res.status})`)
  }
  const accounts = res.data && Array.isArray(res.data.accounts) ? res.data.accounts : []
  xmCookieCache = { key, ts: now, accounts }
  return accounts
}

function isRateLimited(msg) {
  if (!msg) return false
  return ['网络繁忙', '明天再试', '访问过于频繁', '请求过于频繁'].some(kw => msg.includes(kw))
}

// 冷却接口同样带 claim 凭证校验：防止同卡另一插件在持有者下载期间把所有账号打 24h 冷却（软 DDoS）
async function markAccountCooldown(accountId, task) {
  if (!accountId || !task || !task.task_id) return false
  let res
  try {
    res = await serverApi('/api/extension/xm-cookie/cooldown', {
      method: 'POST',
      body: { account_id: accountId, task_id: task.task_id, claim_id: task.claim_id || '' },
    })
  } catch (e) { console.warn('[plugin] markAccountCooldown failed', e); return false }
  if (res.status === 401) return false
  if (!res.ok) {
    if (res.status === 403 || res.status === 409 || res.status === 400) {
      throw new ClaimLostError((res.data && res.data.error) || '冷却请求被服务器拒绝')
    }
    console.warn('[plugin] 冷却被拒绝', res.status)
    return false
  }
  xmCookieCache = null   // 冷却后立即失效本地缓存，下次 getXmCookie 重新拉取可用账号
  return true
}

let lastInstalledCookie = null
async function installXmCookies(cookieStr) {
  if (!cookieStr || cookieStr === lastInstalledCookie) return
  const expiry = Math.floor(Date.now() / 1000) + 365 * 24 * 3600
  const pairs = cookieStr.split(';').map(s => s.trim()).filter(Boolean)
  let ok = 0
  for (const p of pairs) {
    const idx = p.indexOf('=')
    if (idx < 0) continue
    const name = p.slice(0, idx).trim()
    const value = p.slice(idx + 1).trim()
    if (!name) continue
    try {
      await chrome.cookies.set({
        url: 'https://ximalaya.com/',
        name, value,
        domain: 'ximalaya.com',
        path: '/',
        secure: true,
        httpOnly: false,
        sameSite: 'no_restriction',
        expirationDate: expiry,
      })
      ok++
    } catch (e) {
      console.warn('[plugin] 注入 cookie 失败（可能 httpOnly）:', name, e.message)
    }
  }
  if (!ok) throw new Error('无法注入喜马拉雅登录态（请确认插件已授权 cookies 权限，并在 chrome://extensions 刷新后重试）')
  lastInstalledCookie = cookieStr
}

async function resolveOfficialRotation(trackId, quality, sign, accounts, task, ctx) {
  // 用 cookie 锁串行化官方解析，防止并发下载时 cookie 串号
  // 每个 track 的 cookie 安装 + fetch 原子化，避免 A 安装了账号1，B 紧接着安装账号2，A 的请求却用了账号2
  return withCookieLock(async () => {
    const usable = accounts.slice()
    const errors = []
    while (usable.length) {
      const acc = usable.shift()
      try {
        await installXmCookies(acc.cookie)
      } catch (e) {
        const msg = (e && e.message) ? e.message : String(e)
        console.warn('[plugin] 官方账号 cookie 注入失败，切换下一个:', acc.id, msg)
        errors.push(`账号${acc.id}: cookie注入失败 ${msg}`)
        continue
      }
      try {
        return await globalThis.RESOLVERS.official(trackId, quality, sign)
      } catch (e) {
        if (e instanceof ClaimLostError) throw e   // claim 失效：不再轮询账号，直接向上停止整个任务
        const msg = (e && e.message) ? e.message : String(e)
        errors.push(`账号${acc.id}: ${msg}`)
        if (isRateLimited(msg)) {
          console.warn('[plugin] 官方账号限流，切换下一个:', acc.id, msg)
          await markAccountCooldown(acc.id, task)   // 可能抛 ClaimLostError → 向上停止整个任务
        }
      }
    }
    throw new Error(errors.length ? errors.join(' | ') : '所有官方账号均不可用')
  })
}

// ── 完成通知 ──
function notifyTaskDone(task) {
  if (typeof chrome.notifications === 'undefined') return
  const done = (task.progress && task.progress.done) || 0
  const failed = (task.progress && task.progress.failed) || 0
  const title = (task.album_title || ('专辑 ' + task.album_id)) + ' · 下载结束'
  const msg = `${done} 集完成` + (failed ? `，${failed} 集失败` : '')
  try {
    chrome.notifications.create('xm_done_' + task.task_id, {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icon128.png'),
      title,
      message: msg,
      priority: 2,
    }).catch(() => {})
  } catch (e) {}
}

// ── 连接状态 ──
async function pingServer() {
  const { serverUrl } = await cfg()
  const headers = await authHeaders()
  if (!serverUrl || !headers) return { ok: false, msg: '未登录' }
  try {
    const r = await fetch(`${serverUrl}/api/extension/tasks`, { headers })
    if (r.status === 401) {
      const reason = (await r.json().catch(() => null))?.detail
      await handleUnauthorized(reason)
      return { ok: false, msg: reason || '登录已失效' }
    }
    const data = await r.json()
    return { ok: !!data.success, msg: data.success ? '已连接' : (data.error || '服务器拒绝') }
  } catch (e) {
    return { ok: false, msg: '连接失败：' + e.message }
  }
}
