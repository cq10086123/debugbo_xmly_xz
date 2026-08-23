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
// ⚠️ 存储迁移标记不再用内存变量（SW 重启会丢失导致重复迁移），
//    改为 chrome.storage.local 持久化 key = xm_storage_migrated（见 migrateStorage）

// ── 队列读写互斥 ──
// ⚠️ 死锁红线：withQueue 的回调里绝对不允许再调用 withQueue（自己等自己，永久卡死）。
//    锁内需要"检查任务是否完成/重算进度"时，只能用下面的纯函数 finalizeTask / recomputeProgress。
let queueLock = Promise.resolve()
function withQueue(fn) {
  const p = queueLock.then(fn, fn)
  queueLock = p.catch(() => {})
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

// 在锁内原子更新任务 + 索引（保证一致性）
async function setTaskAndUpdateIndex(task) {
  if (!task || !task.task_id) throw new Error('setTaskAndUpdateIndex: task_id is required')
  await setTask(task)
  // 同步更新索引中的状态字段
  const idx = await getIndex()
  const itemIdx = idx.findIndex(item => item.task_id === task.task_id)
  if (itemIdx >= 0) {
    idx[itemIdx] = {
      ...idx[itemIdx],
      status: task.status,
      paused: !!task.paused,
      album_id: task.album_id,
      album_title: task.album_title,
      source: task.source,
    }
    await setIndex(idx)
  }
}

// 向后兼容：从旧存储迁移到分片存储
// ⚠️ 修复（2026-08-19）：旧实现有三个致命缺陷——
//   1) storageMigrated 是内存变量，SW 重启后置 false → 重复迁移
//   2) 迁移后未删除 xm_taskQueue → 旧数据永久残留，每次 SW 重启复活
//   3) setIndex(idx) 整体替换索引 → 当前分片任务（如「剑来」）被丢弃丢失
// 新实现：
//   - 用 chrome.storage 持久化标记 xm_storage_migrated 替代内存变量
//   - 迁移完成后立即删除 xm_taskQueue（根因）
//   - 只把"分片仍存在"的任务补回索引（已清空的完美世界不会复活，被覆盖的剑来能找回）
//   - 额外自愈：扫描所有 xm_task:* 分片，把索引中缺失的任务补回（恢复被覆盖丢失的）
async function migrateStorage() {
  const migrated = await new Promise(r => chrome.storage.local.get(['xm_storage_migrated'], o => r(o.xm_storage_migrated)))
  if (migrated) return
  try {
    const all = await new Promise(r => chrome.storage.local.get(null, r))
    const idx = await getIndex()
    const have = new Set(idx.map(i => i.task_id))
    let changed = false

    // ① 自愈：扫描所有分片任务，把索引中缺失的补回（修复 SW 重启导致索引被覆盖丢失的任务）
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

    // ② 旧格式 xm_taskQueue：仅当对应分片仍存在时才补回（用户已清空的不会复活）
    const old = all[STORAGE_QUEUE]
    if (old && old.length) {
      for (const t of old) {
        if (!t.task_id || have.has(t.task_id)) continue
        if (!all[taskKey(t.task_id)]) continue  // 分片不存在 = 用户已删除，不恢复
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
    // ③ 删除旧格式数据（根因：残留导致每次 SW 重启重复迁移旧记录）
    if (old && old.length) await new Promise(r => chrome.storage.local.remove([STORAGE_QUEUE], r))
    console.log('[plugin] 存储迁移/自愈完成')
  } catch (e) {
    console.error('[plugin] 存储迁移失败', e)
  }
  // ④ 持久化标记，替代内存变量 storageMigrated（SW 重启后不再重复迁移）
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
  // 分片模式下不再使用全量 setQueue，但为了兼容旧调用，转换为分片写入
  await migrateStorage()
  if (!q || !q.length) {
    // 清空所有
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
    await setTaskAndUpdateIndex(updated)
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
    await setTaskAndUpdateIndex(task)
  })
  if (finished) notifyTaskDone(finished)
  maybeStopKeepAlive()
}

// ── 事件注册 ──
chrome.alarms.onAlarm.addListener(handleAlarm)
chrome.runtime.onMessage.addListener(handleMessage)
chrome.downloads.onChanged.addListener(handleDownloadChanged)
// 注意：不要在 onSuspend 里恢复下载 UI —— SW 每次挂起都会恢复，
// 会造成"静默开关开着但气泡照弹"的竞态窗口。UI 隐藏状态由 Chrome 按扩展维度保持，
// 用户在设置里关掉「静默下载」或卸载插件时自然会恢复。

// 启动：静默下载 UI → 拉取新任务 → 校正历史下载状态（SW 被杀重启场景）→ 自动批准待处理 → 派发续传
applySilentMode()
pollAnnouncement().catch(() => {})   // 公告拉取独立于下载主流程，失败不影响任务
pollBackend()
  .then(() => reconcileDownloads())
  .then(() => autoApproveIfEnabled())
  .then(() => pump())
  .catch(e => console.error('[plugin] 启动流程 error', e))
chrome.alarms.create('poll', { periodInMinutes: 1 })

function handleAlarm(alarm) {
  if (alarm.name === 'poll') {
    pollAnnouncement().catch(() => {})
    pollBackend()
      .then(() => reconcileDownloads())
      .then(() => autoApproveIfEnabled())
      .then(() => pump())
      .catch(e => console.error('[plugin] poll error', e))
  }
  // keepAlive alarm 收到即保持 service worker 存活
}

// 开启「自动下载」时，把所有卡在 pending 的任务批准为 running（含重启前遗留的）
async function autoApproveIfEnabled() {
  const s = await getSettings()
  if (!s.autoDownload) return
  await withQueue(async () => {
    const idx = await getIndex()
    const taskIds = idx.filter(item => item.status === 'pending').map(item => item.task_id)
    if (!taskIds.length) return
    for (const taskId of taskIds) {
      const task = await getTask(taskId)
      if (task && task.status === 'pending') {
        task.status = 'running'
        await setTaskAndUpdateIndex(task)
      }
    }
  })
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
// 拉取最新启用公告并缓存到 storage（xm_announcement），popup 读取后与本地已读 id 对比决定是否弹出。
// 已读状态存于 xm_announcement_read（id 数组），后端不跟踪已读。
async function pollAnnouncement() {
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) return null
  try {
    const r = await fetch(`${serverUrl}/api/announcements/current`, { headers: { Authorization: 'Bearer ' + token } })
    const data = await r.json()
    const ann = (data && data.success && data.announcement) ? data.announcement : null
    await new Promise(res => chrome.storage.local.set({ xm_announcement: ann }, res))
    return ann
  } catch (e) {
    return null  // 拉取失败静默忽略，不影响下载主流程
  }
}

// ── 后端轮询 ──
async function pollBackend() {
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) return
  let newIds = []
  try {
    const r = await fetch(`${serverUrl}/api/extension/tasks`, { headers: { Authorization: 'Bearer ' + token } })
    const data = await r.json()
    if (!data.success) { console.warn('[plugin] poll backend failed', data); return }
    const tasks = data.tasks || []
    if (!tasks.length) return
    // 先本地落库，再 ack 后端（顺序反了会丢任务）
    await withQueue(async () => {
      const idx = await getIndex()
      let changed = false
      for (const t of tasks) {
        if (idx.some(existing => existing.task_id === t.task_id)) continue
        const tracks = (t.tracks || []).map(tr => ({
          ...tr, status: 'pending', error: '', downloadId: null,
        }))
        const newTask = {
          ...t, tracks, status: 'pending', paused: false,
          progress: { total: tracks.length, done: 0, failed: 0 },
          errorMsg: '', createdAt: Date.now(),
        }
        await setTask(newTask)
        idx.push({
          task_id: t.task_id,
          album_id: t.album_id,
          album_title: t.album_title,
          source: t.source,
          status: 'pending',
          paused: false,
          createdAt: Date.now(),
        })
        newIds.push(t.task_id)
        changed = true
      }
      if (changed) await setIndex(idx)
    })
    // ack（已落库，丢 ack 也只是重复拉取，不会丢任务）
    for (const t of tasks) {
      try {
        await fetch(`${serverUrl}/api/extension/tasks/${t.task_id}/ack`, {
          method: 'POST', headers: { Authorization: 'Bearer ' + token },
        })
      } catch (e) { console.error('[plugin] ack failed', t.task_id, e) }
    }
    console.log('[plugin] 已拉取新任务', newIds.length, '个')
  } catch (e) { console.error('[plugin] poll error', e); return }

  // 设置开启了「自动下载」→ 新任务直接批准并派发
  if (newIds.length) {
    const s = await getSettings()
    if (s.autoDownload) {
      await withQueue(async () => {
        const idx = await getIndex()
        for (const item of idx) {
          if (newIds.includes(item.task_id) && item.status === 'pending') {
            item.status = 'running'
            const task = await getTask(item.task_id)
            if (task) {
              task.status = 'running'
              await setTaskAndUpdateIndex(task)
            }
          }
        }
        await setIndex(idx)
      })
      pump()
    }
  }
}

// ── 启动续传：校正 SW 被杀前的下载状态 ──
// 卡在 resolving/downloading 且下载记录已丢失的 → 重置为 pending 交由 pump 重派；
// 已完成 / 已失败的按 chrome.downloads 真实状态校正（SW 死亡期间完成的也能对账）。
async function reconcileDownloads() {
  const finishedList = []
  await withQueue(async () => {
    const idx = await getIndex()
    let changed = false
    for (const item of idx) {
      if (item.status === 'done' || item.status === 'cancelled') continue
      const task = await getTask(item.task_id)
      if (!task) continue
      let taskChanged = false
      for (const tr of task.tracks) {
        if (tr.downloadId == null) {
          if (tr.status === 'downloading' || tr.status === 'resolving') { tr.status = 'pending'; tr.error = ''; taskChanged = true }
          continue
        }
        try {
          const items = await chrome.downloads.search({ id: tr.downloadId })
          const it = items && items[0]
          if (!it || it.state === 'interrupted') {
            // ⚠️ 只重置「传输中/解析中」的集：done 集的下载记录是被我们自己 erase 的（正常状态），
            // 若把 done 重置回 pending，下一轮 alarm 的 pump 会把已完成集重新下载出 "(1)" 副本
            if (tr.status === 'downloading' || tr.status === 'resolving') {
              tr.status = 'pending'; tr.downloadId = null; tr.error = ''; taskChanged = true
            }
          } else if (it.state === 'complete') {
            if (tr.status !== 'done') { tr.status = 'done'; tr.error = ''; taskChanged = true }
          } else if (it.state === 'in_progress') {
            if (tr.status !== 'downloading') { tr.status = 'downloading'; taskChanged = true }
          }
        } catch (e) { /* 忽略单条查询错误 */ }
      }
      if (finalizeTask(task)) { finishedList.push({ ...task }); taskChanged = true }
      if (taskChanged) {
        await setTaskAndUpdateIndex(task)
        changed = true
      }
    }
    if (changed) { console.log('[plugin] reconcileDownloads corrected state') }
  })
  for (const t of finishedList) notifyTaskDone(t)
}

// 手动下载：把 pending 任务标记为 running（已批准），再派发
async function approveAndProcess() {
  await withQueue(async () => {
    const idx = await getIndex()
    let changed = false
    for (const item of idx) {
      if (item.status === 'pending' || item.status === 'running') {
        if (item.status !== 'running') {
          item.status = 'running'
          const task = await getTask(item.task_id)
          if (task) {
            task.status = 'running'
            task.paused = false
            await setTaskAndUpdateIndex(task)
          }
          changed = true
        }
      }
    }
    if (changed) await setIndex(idx)
  })
  await pump()
}

// ── 下载调度器（pump 式：每次读取最新队列状态，暂停/取消立即生效）──
let activeCount = 0                 // 进行中的下载协程数
const inflight = new Set()          // `${taskId}/${trackId}`，防重复派发
let pumping = false
let pumpQueued = false

// ── 书籍级串行选取 ──
// 规则：同一时刻只允许一本「活跃书」在下载。服务器一次推多本时，
// 其余书保持 running 但处于「排队中」，等当前书全部结束才轮到下一本。
// 活跃书 = 第一本有进行中集（resolving/downloading/inflight）的 running 任务；
// 若没有活跃书，则取第一本还有待下载集的 running 任务。
// ⚠️ 此函数在 pump 的 while 循环中调用，不在 withQueue 锁内。它只读取 storage，不写入。
async function pickBookDispatch() {
  const idx = await getIndex()
  const runningIds = idx.filter(i => i.status === 'running').map(i => i.task_id)
  if (!runningIds.length) return null
  
  // 获取所有 running 任务的完整数据
  const tasks = await getTasksBatch(runningIds)
  
  let active = null
  for (const t of tasks) {
    if (t.paused) continue
    const busy = t.tracks.some(tr =>
      tr.status === 'resolving' || tr.status === 'downloading' ||
      (tr.status === 'pending' && inflight.has(t.task_id + '/' + tr.track_id))
    )
    if (busy) { active = t; break }
  }
  if (!active) {
    for (const t of tasks) {
      if (t.paused) continue
      if (t.tracks.some(tr => tr.status === 'pending')) { active = t; break }
    }
  }
  if (!active) return null
  for (const tr of active.tracks) {
    if (tr.status === 'pending' && !inflight.has(active.task_id + '/' + tr.track_id)) {
      return { taskId: active.task_id, trackId: tr.track_id }
    }
  }
  return null  // 活跃书没有可派发的集（都在传输中）→ 等它结束再派下一本
}

async function pump() {
  if (pumping) { pumpQueued = true; return }
  pumping = true
  let dispatched = false
  try {
    const settings = await getSettings()
    const limit = Math.max(1, Math.min(8, settings.concurrency || 1))
    while (activeCount < limit) {
      const pick = await pickBookDispatch()
      if (!pick) break
      const key = pick.taskId + '/' + pick.trackId
      inflight.add(key)
      activeCount++
      dispatched = true
      // 先落盘为 resolving，杜绝 SW 重启/并发导致的重复派发
      await updateTrack(pick.taskId, pick.trackId, { status: 'resolving', error: '' })
      runTrack(pick.taskId, pick.trackId)
        .catch(e => console.error('[plugin] runTrack error', e))
        .finally(() => {
          inflight.delete(key)
          activeCount = Math.max(0, activeCount - 1)
          pump()
        })
    }
  } finally {
    pumping = false
    if (pumpQueued) { pumpQueued = false; pump() }
    else if (!dispatched) maybeStopKeepAlive()
  }
  if (dispatched) startKeepAlive()
}

// 派发前闸口：读取最新任务状态（暂停/取消对未开始的集立即生效）
// ⚠️ 此函数在 runTrack 中调用，不在 withQueue 锁内。它读取任务状态，必要时调用 updateTrack（会获取锁）
async function gateCheck(taskId, trackId) {
  const task = await getTask(taskId)
  if (!task) return 'abort'
  if (task.status === 'cancelled') return 'abort'   // cancelTask 已把该集标记为「已取消」
  if (task.status !== 'running' || task.paused) {
    // 未开始即被暂停 → 回滚为待处理，等用户继续
    await updateTrack(taskId, trackId, { status: 'pending', error: '' })
    return 'abort'
  }
  return 'go'
}

const trackDoneResolvers = new Map()   // downloadId -> resolve
const pendingTerminals = new Map()     // downloadId -> true（终端事件先于 resolver 注册到达时缓存）
function awaitTrackDone(downloadId) {
  return new Promise(res => {
    trackDoneResolvers.set(downloadId, res)
    if (pendingTerminals.has(downloadId)) { pendingTerminals.delete(downloadId); res() }
  })
}
function resolveTerminal(downloadId) {
  const r = trackDoneResolvers.get(downloadId)
  if (r) { trackDoneResolvers.delete(downloadId); r() }
  else {
    // 非插件下载（用户手动下载等）的终端事件会缓存到 pendingTerminals，设上限防无界增长
    if (pendingTerminals.size > 1000) pendingTerminals.clear()
    pendingTerminals.set(downloadId, true)
  }
}

async function runTrack(taskId, trackId) {
  const { serverUrl, token } = await cfg()
  let downloadId = null
  let lastErr = null
  const settings = await getSettings()
  const maxRetry = Math.max(1, Math.min(10, settings.maxRetry || MAX_RETRY))
  const delayMs = Math.max(0, Math.min(120000, settings.downloadDelayMs || 0))
  // 每集间隔延迟（防音源风控）；延迟期间暂停/取消由循环内的 gateCheck 兜住
  if (delayMs > 0) await sleep(delayMs)
  // 阶段一：解析直链 + 创建下载（失败可重试）
  for (let attempt = 1; attempt <= maxRetry; attempt++) {
    if (await gateCheck(taskId, trackId) !== 'go') return
    try {
      const task = await getTask(taskId)
      if (!task) return
      const tr = task.tracks.find(t => t.track_id === trackId)
      if (!tr) return
      // 防重复落盘守卫①：本集已完成 / 正在传输 → 不再重复下载
      if (tr.status === 'done' || tr.status === 'downloading') return
      // 防重复落盘守卫②：同专辑+同集+同格式已在其他任务下完（如同一专辑被推送了两次）
      // → 直接标 done 跳过，不再产生 "(1)" 副本文件
      // ⚠️ 此检查在锁外，但只读取状态不写入。即使竞态导致漏检，后续 updateTrack 的锁内检查会兜底
      const idx = await getIndex()
      const otherIds = idx.filter(i => i.task_id !== taskId && i.album_id === task.album_id).map(i => i.task_id)
      const otherTasks = await getTasksBatch(otherIds)
      const dup = otherTasks.find(o =>
        o.tracks.some(t => t.track_id === trackId && t.status === 'done' &&
          (t.fmt || o.fmt || 'mp3') === (tr.fmt || task.fmt || 'mp3')))
      if (dup) {
        console.log('[plugin] 跳过重复集（其他任务已下载）', task.album_id, trackId)
        await updateTrack(taskId, trackId, { status: 'done', error: '' })
        return
      }
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
      lastErr = e
      console.warn(`[plugin] 第${attempt}次解析/创建下载失败`, trackId, e && e.message)
      if (attempt < maxRetry) { await sleep(attempt * 1500); continue }
      await updateTrack(taskId, trackId, { status: 'error', error: (lastErr && lastErr.message) || String(lastErr) })
      return
    }
  }
  if (downloadId == null) return
  // 阶段二：等待终端态（完成/失败由 onChanged 处理；不重试避免重复文件）
  const ok = await Promise.race([
    awaitTrackDone(downloadId).then(() => true),
    sleep(TRACK_TIMEOUT).then(() => false),
  ])
  if (!ok) {
    console.warn('[plugin] 下载超时', downloadId, trackId)
    await updateTrack(taskId, trackId, { status: 'error', error: '下载超时（30分钟未完成）' })
    try { await chrome.downloads.cancel(downloadId) } catch (e) {}
  }
}

async function resolveForTrack(task, tr, ctx) {
  if (task.source === 'official') {
    const sign = await getSign()
    const accounts = await getXmCookie()
    if (!accounts || !accounts.length) throw new Error('无可用官方账号（请先在网页「账号」页登录喜马拉雅账号）')
    return await resolveOfficialRotation(tr.track_id, task.quality, sign, accounts, ctx)
  }
  const resolver = globalThis.RESOLVERS[task.source]
  if (!resolver) throw new Error(`未实现解析器: ${task.source}（本地下载要求 extension/sources/ 下有同名脚本注册该音源。若后端新增了脚本接口，需在 sources/ 放对应 JS 脚本、并在 sources.config.js 的 PLUGIN_SOURCES 登记；否则请改用网页端「服务器端下载」）`)
  const res = await resolver(tr, { serverUrl: ctx.serverUrl, token: ctx.token, quality: task.quality, fmt: task.fmt, albumId: task.album_id })
  return typeof res === 'string' ? res : (res.url || '')
}

// ── 监听 chrome.downloads 状态变化 ──
// 顺序关键：先 resolveTerminal 唤醒等待协程（绝不被队列锁阻塞），再异步落盘状态。
function handleDownloadChanged(delta) {
  if (!delta || !delta.id) return
  const id = delta.id
  const terminal = (delta.state && (delta.state.current === 'complete' || delta.state.current === 'interrupted'))
    || (delta.error && delta.error.current)
  if (terminal) {
    console.log('[plugin] download terminal', id, delta.state && delta.state.current, delta.error && delta.error.current)
    resolveTerminal(id)
    // 注意：erase 不在这里做 —— 必须等下方 withQueue 把「done」写进存储之后再抹记录，
    // 否则 SW 在 erase→写存储 的间隙被杀时，重启对账找不到记录会把已完成集重置重下。
    // 且 erase 只应作用于插件自己的下载（匹配到队列条目才抹），不能抹用户手动下载的记录。
  }
  if (!delta.state && !delta.error && !delta.filename) return
  withQueue(async () => {
    const idx = await getIndex()
    for (const item of idx) {
      const task = await getTask(item.task_id)
      if (!task) continue
      const tr = task.tracks.find(t => t.downloadId === id)
      if (!tr) continue
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
      // 仅当确有状态变更（patch 非空或任务已终态）时才写回
      if (hadPatch || finished) {
        await setTaskAndUpdateIndex(task)
        // 状态已安全落盘 → 此时抹掉 Chrome 下载记录才不会再触发重启重下；磁盘文件不受影响
        if (delta.state && delta.state.current === 'complete') {
          try { chrome.downloads.erase({ id }).catch(() => {}) } catch (e) {}
        }
        if (finished) notifyTaskDone({ ...task })
      }
      break
    }
  }).then(() => maybeStopKeepAlive()).catch(e => console.error('[plugin] onChanged error', e))
}

// ── 下载控制：暂停 / 继续 / 取消 / 重试（全部真正生效）──
async function pauseTask(taskId) {
  await updateTask(taskId, { paused: true })
  const task = await getTask(taskId)
  if (!task) return
  // 传输中的也真正暂停（Chrome 层面断流，不只是停止派发新集）
  for (const tr of task.tracks) {
    if (tr.status === 'downloading' && tr.downloadId != null) {
      try { await chrome.downloads.pause(tr.downloadId) } catch (e) {}
    }
  }
}

async function resumeTask(taskId) {
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (task) {
      task.paused = false
      if (task.status === 'pending') task.status = 'running'
      await setTaskAndUpdateIndex(task)
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
  await pump()
}

async function cancelTask(taskId) {
  const ids = []
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) ids.push(tr.downloadId)
      if (tr.status === 'pending' || tr.status === 'downloading' || tr.status === 'resolving') {
        tr.status = 'error'; tr.error = '已取消'
      }
    }
    task.status = 'cancelled'
    task.paused = false
    recomputeProgress(task)
    await setTaskAndUpdateIndex(task)
  })
  // 先落库「已取消」再 cancel，onChanged 回来时会看到取消标记、不覆盖语义
  for (const id of ids) { try { await chrome.downloads.cancel(id) } catch (e) {} }
  maybeStopKeepAlive()
}

async function retryFailed(taskId) {
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (!task) return
    if (task.status === 'cancelled') return   // 已取消的任务不重试（其 error 都是「已取消」）
    let has = false
    for (const tr of task.tracks) {
      if (tr.status === 'error') { tr.status = 'pending'; tr.error = ''; tr.downloadId = null; has = true }
    }
    // 只要有失败集被重置为 pending，就把任务改回 running（含 done 任务）。
    // 之前 done 不改回 running，是「点重试没反应」的根因：调度器只派发 running 任务。
    if (has) { task.status = 'running'; task.paused = false }
    recomputeProgress(task)
    await setTaskAndUpdateIndex(task)
  })
  await pump()
}

async function retryAllFailed() {
  await withQueue(async () => {
    const idx = await getIndex()
    for (const item of idx) {
      if (item.status === 'cancelled') continue   // 已取消的任务不重试，避免复活取消的书
      const task = await getTask(item.task_id)
      if (!task) continue
      let has = false
      for (const tr of task.tracks) {
        if (tr.status === 'error') { tr.status = 'pending'; tr.error = ''; tr.downloadId = null; has = true }
      }
      // 同 retryFailed：done 任务也要改回 running 才能被调度器派发
      if (has) { task.status = 'running'; task.paused = false }
      recomputeProgress(task)
      if (has) {
        await setTaskAndUpdateIndex(task)
      }
    }
  })
  await pump()
}

async function pauseAll() {
  const idx = await getIndex()
  const runningIds = idx.filter(i => i.status === 'running' && !i.paused).map(i => i.task_id)
  for (const id of runningIds) await pauseTask(id)
}

// 启动单个待处理任务（批准 → running → 派发）
async function startTask(taskId) {
  await withQueue(async () => {
    const task = await getTask(taskId)
    if (task && (task.status === 'pending' || task.status === 'running')) {
      task.status = 'running'; task.paused = false
      await setTaskAndUpdateIndex(task)
    }
  })
  await pump()
}

// 全部继续 = 恢复已暂停 + 批准所有待处理任务（不再让任务卡在「待处理」）
async function resumeAll() {
  await withQueue(async () => {
    const idx = await getIndex()
    let changed = false
    for (const item of idx) {
      if (item.status === 'done' || item.status === 'cancelled') continue
      if (item.status === 'pending' || item.paused) {
        item.status = 'running'
        item.paused = false
        const task = await getTask(item.task_id)
        if (task) {
          task.status = 'running'
          task.paused = false
          await setTaskAndUpdateIndex(task)
        }
        changed = true
      }
    }
    if (changed) await setIndex(idx)
  })
  // 恢复所有传输中被暂停的下载
  const idx = await getIndex()
  const runningIds = idx.filter(i => i.status === 'running').map(i => i.task_id)
  const tasks = await getTasksBatch(runningIds)
  for (const task of tasks) {
    if (task.status !== 'running') continue
    for (const tr of task.tracks) {
      if (tr.status === 'downloading' && tr.downloadId != null) {
        try { await chrome.downloads.resume(tr.downloadId) } catch (e) {}
      }
    }
  }
  await pump()
}

async function clearTasks(statuses) {
  return withQueue(async () => {
    const idx = await getIndex()
    const cleared = idx.filter(item => statuses.includes(item.status)).length
    const remaining = idx.filter(item => !statuses.includes(item.status))
    const removedIds = idx.filter(item => statuses.includes(item.status)).map(item => item.task_id)
    // 删除分片
    for (const id of removedIds) await delTask(id)
    await setIndex(remaining)
    return { ok: true, cleared }
  })
}

// ── keepAlive：下载进行中保持 service worker 存活 ──
function startKeepAlive() {
  if (keepAliveActive) return
  keepAliveActive = true
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 })  // Chrome 最小允许 0.5 分钟(30s)，更小的会被静默钳制
}
function stopKeepAlive() {
  if (!keepAliveActive) return
  keepAliveActive = false
  chrome.alarms.clear('keepAlive')
}
async function maybeStopKeepAlive() {
  // 先检查内存中的 activeCount，避免不必要的存储读取
  if (activeCount > 0) return
  const idx = await getIndex()
  const anyActive = idx.some(item => item.status === 'running' && !item.paused)
  if (!anyActive) stopKeepAlive()
}

// ── 静默下载：关闭浏览器自带下载 UI（下载完成弹窗/下载气泡）──
// 注意：setUiOptions 除了 downloads 权限外还需要 manifest 里的 downloads.ui 权限，
// 否则调用直接抛错（不弹任何提示，纯静默失败）。
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
  const album = (task.album_title || ('album_' + task.album_id)).replace(/[\\/:*?"<>|]/g, '_')
  const ep = tr.episode_num != null ? String(tr.episode_num).padStart(4, '0') : (tr.track_id || '')
  const title = (tr.title || '').replace(/[\\/:*?"<>|]/g, '_')
  const base = `${album}/${ep} ${title}.${tr.fmt || task.fmt || 'mp3'}`
  if (!prefix) return base
  const p = prefix.replace(/[\\/:*?"<>|]/g, '_').replace(/\/$/, '')
  return `${p}/${base}`
}

// ── 官方 xm-sign：通过 Offscreen Document 调用 du_web_sdk ──
async function ensureOffscreen() {
  // 用 hasDocument() 取真实状态作为真相来源：文档在就跳过；被关闭/崩溃后能重建。
  // （不能用 offscreenReady 静态标志当真相——否则文档关闭后 ensureOffscreen 永远 return，
  //   官方源 sendMessage 到不存在的文档 → "Receiving end does not exist" → 永久失败）
  try {
    if (typeof chrome.offscreen.hasDocument === 'function' && await chrome.offscreen.hasDocument()) {
      offscreenReady = true
      return
    }
  } catch (_) { /* 旧版本 Chrome 无此 API，走下方 offscreenReady 兜底 */ }
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
  // offscreen 文档刚创建时脚本可能尚未注册监听 → 重试规避 "Receiving end does not exist"
  for (let i = 0; i < 3; i++) {
    try {
      const sign = await chrome.runtime.sendMessage({ target: 'offscreen', type: 'sign' })
      if (sign && sign.error) throw new Error(sign.error)
      if (!sign || typeof sign !== 'string') throw new Error('xm-sign 为空')
      return sign
    } catch (e) {
      const msg = (e && e.message) || String(e)
      if (/Receiving end does not exist|could not establish|message channel|The receiver/.test(msg) && i < 2) {
        await sleep(300); continue
      }
      throw e
    }
  }
}

// ── 官方账号 cookie：从后端下发（仅当前登录卡密下的可用账号列表），缓存 5 分钟 ──
let xmCookieCache = null
let xmCookieTs = 0
async function getXmCookie() {
  const now = Date.now()
  if (xmCookieCache !== null && now - xmCookieTs < 5 * 60 * 1000) return xmCookieCache
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) { xmCookieCache = []; xmCookieTs = now; return [] }
  try {
    const r = await fetch(`${serverUrl}/api/extension/xm-cookie`, { headers: { Authorization: 'Bearer ' + token } })
    const data = await r.json()
    xmCookieCache = Array.isArray(data.accounts) ? data.accounts : []
  } catch (e) {
    console.warn('[plugin] getXmCookie failed', e)
    xmCookieCache = []
  }
  xmCookieTs = now
  return xmCookieCache
}

// 限流关键词（与服务器 api/download.py _is_rate_limited 保持一致）
function isRateLimited(msg) {
  if (!msg) return false
  return ['网络繁忙', '明天再试', '访问过于频繁', '请求过于频繁'].some(kw => msg.includes(kw))
}

// 插件检测到某账号被限流时，通知后端标记该账号冷却（仅当前卡密）
async function markAccountCooldown(accountId, ctx) {
  if (!ctx || !ctx.serverUrl || !ctx.token || !accountId) return
  try {
    await fetch(`${ctx.serverUrl}/api/extension/xm-cookie/cooldown`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + ctx.token },
      body: JSON.stringify({ account_id: accountId }),
    })
  } catch (e) { console.warn('[plugin] markAccountCooldown failed', e) }
}

// MV3 关键坑：fetch 禁止手动设置 Cookie 头（forbidden header），浏览器会静默丢弃。
// 所以必须把官方账号的登录态用 chrome.cookies.set 注入浏览器 cookie 商店，
// fetch 访问 mobile.ximalaya.com 时才会自动带上，否则永远 ret=2002 未登录。
let lastInstalledCookie = null
async function installXmCookies(cookieStr) {
  if (!cookieStr || cookieStr === lastInstalledCookie) return   // 同字符串去重，避免千集重复写盘
  const expiry = Math.floor(Date.now() / 1000) + 365 * 24 * 3600
  const pairs = cookieStr.split(';').map(s => s.trim()).filter(Boolean)
  let ok = 0
  for (const p of pairs) {
    const idx = p.indexOf('=')
    if (idx < 0) continue
    const name = p.slice(0, idx).trim()
    const value = p.slice(idx + 1).trim()
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

// 官方源多账号轮询：遍历 accounts（VIP 优先），某账号命中限流则标记冷却并换下一个。
async function resolveOfficialRotation(trackId, quality, sign, accounts, ctx) {
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
      const msg = (e && e.message) ? e.message : String(e)
      errors.push(`账号${acc.id}: ${msg}`)
      if (isRateLimited(msg)) {
        console.warn('[plugin] 官方账号限流，切换下一个:', acc.id, msg)
        await markAccountCooldown(acc.id, ctx)
      }
      // 非限流错误也继续轮动（如该账号 cookie 失效/临时失败，换下一个可用账号），
      // 但把每个账号的原始报错收集起来，最后汇总抛出，用户能看到真实失败原因
    }
  }
  throw new Error(errors.length ? errors.join(' | ') : '所有官方账号均不可用')
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
  const { serverUrl, token } = await cfg()
  if (!serverUrl || !token) return { ok: false, msg: '未登录' }
  try {
    const r = await fetch(`${serverUrl}/api/extension/tasks`, { headers: { Authorization: 'Bearer ' + token } })
    const data = await r.json()
    return { ok: !!data.success, msg: data.success ? '已连接' : (data.error || '服务器拒绝') }
  } catch (e) {
    return { ok: false, msg: '连接失败：' + e.message }
  }
}
