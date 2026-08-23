// popup.js — 插件弹窗：卡密登录、任务列表、实时字节进度、任务控制
const $ = (id) => document.getElementById(id)

// config.js 内置服务器地址：配置后登录页隐藏地址输入框，只需填卡密
const BUILTIN_SERVER = (typeof PLUGIN_CONFIG !== 'undefined' && PLUGIN_CONFIG.serverUrl
  ? String(PLUGIN_CONFIG.serverUrl) : '').trim().replace(/\/+$/, '')

function setMsg(text, cls) {
  const el = $('msg')
  el.textContent = text || ''
  el.className = 'status ' + (cls || '')
}
function getCfg() { return new Promise(r => chrome.storage.local.get(['serverUrl', 'token', 'card', 'deviceId'], r)) }
function setCfg(obj) { return new Promise(r => chrome.storage.local.set(obj, r)) }

// ── 设备 ID（设备绑定用）：首次生成后持久化，退出登录不清除 ──
function genUuid() {
  if (crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const b = new Uint8Array(16)
  crypto.getRandomValues(b)
  b[6] = (b[6] & 0x0f) | 0x40
  b[8] = (b[8] & 0x3f) | 0x80
  const hex = Array.from(b, x => x.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`
}
async function ensureDeviceId() {
  const { deviceId } = await getCfg()
  if (deviceId) return deviceId
  const id = genUuid()
  await setCfg({ deviceId: id })
  return id
}

let curCaptchaId = ''
async function loadCaptcha(serverUrl) {
  if (!serverUrl) return
  const img = $('captchaImg')
  try {
    const r = await fetch(`${serverUrl}/api/auth/captcha`, { cache: 'no-store' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const j = await r.json()
    curCaptchaId = j.captchaId || ''
    if (img && j.svg) { img.src = j.svg; img.title = '点击刷新验证码' }
  } catch (e) {
    // 显示占位提示，让用户知道是服务器地址/后端问题，而不是 UI 缺图
    curCaptchaId = ''
    if (img) {
      img.src = ''
      img.title = `验证码加载失败：${e.message}，点此重试`
    }
  }
}
function refreshCaptcha() {
  const su = BUILTIN_SERVER || ($('server').value.trim().replace(/\/+$/, ''))
  if (su) loadCaptcha(su)
}
function escapeHtml(s) { return (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) }
function fmtBytes(n) {
  if (!n || n <= 0) return '0 MB'
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

// 统一的后台通信层：15s 超时 + 捕获异常 + service worker 唤醒竞态导致的 undefined 返回 → 自动重试一次
async function swCall(type, body) {
  let lastErr = null
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await Promise.race([
        chrome.runtime.sendMessage({ target: 'sw', type, ...(body || {}) }),
        new Promise((_, rej) => setTimeout(() => rej(new Error('后台响应超时')), 15000)),
      ])
      if (r === undefined) throw new Error('no-response')
      if (r && r.ok === false && r.error) throw new Error(r.error)
      return r
    } catch (e) {
      lastErr = e
      await new Promise(res => setTimeout(res, 400))
    }
  }
  throw new Error('插件后台无响应（' + ((lastErr && lastErr.message) || '未知') + '），请在 chrome://extensions 刷新本插件后重试')
}

let refreshTimer = null
let connTimer = null
const expandedTasks = new Set()   // 已展开明细的任务 id（1.5s 自动刷新重建 DOM 时保持展开状态）

async function showMain() {
  const { token, card, auth_error } = await getCfg()
  if (token) {
    $('loginBox').style.display = 'none'
    $('mainBox').style.display = 'block'
    $('who').innerHTML = `已登录：<b>${escapeHtml((card && card.code) || '')}</b>`
    await swCall('pullTasks').catch(() => {})
    await loadSettings()
    await renderQueue()
    renderAnnouncement()
    startAutoRefresh()
    startConn()
  } else {
    $('loginBox').style.display = 'block'
    $('mainBox').style.display = 'none'
    // 后台检测到登录失效（被顶下线/过期/风控）时留下原因，弹窗打开即展示并清除
    if (auth_error) {
      setMsg(auth_error, 'err')
      setCfg({ auth_error: null })
    }
    if (BUILTIN_SERVER) {
      // config.js 内置了服务器地址 → 隐藏输入框，保持登录页简洁
      $('server').style.display = 'none'
      $('serverLabel').style.display = 'none'
      loadCaptcha(BUILTIN_SERVER)
    } else {
      $('server').style.display = ''
      $('serverLabel').style.display = ''
      ;(async () => {
        const saved = ((await getCfg()).serverUrl || '').trim()
        if (saved) {
          $('server').value = saved
          loadCaptcha(saved)
        }
      })()
      $('server').onblur = () => {
        const v = $('server').value.trim().replace(/\/+$/, '')
        if (v) {
          setCfg({ serverUrl: v })
          loadCaptcha(v)
        }
      }
    }
    $('captchaImg').onclick = refreshCaptcha
    stopAutoRefresh()
    stopConn()
  }
}

$('loginBtn').onclick = async () => {
  const serverUrl = BUILTIN_SERVER || $('server').value.trim().replace(/\/+$/, '')
  const code = $('code').value.trim()
  if (!serverUrl || !code) { setMsg(BUILTIN_SERVER ? '请填写卡密' : '请填写服务器地址和卡密', 'err'); return }
  setMsg('登录中…')
  const deviceId = await ensureDeviceId()
  try {
    const r = await fetch(`${serverUrl}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code,
        captchaId: curCaptchaId,
        captcha: $('captcha').value.trim(),
        deviceId,          // 设备绑定：本插件的设备 ID
        client: 'extension',
      }),
    })
    const data = await r.json()
    if (!data.success) {
      setMsg(data.error || '登录失败', 'err')
      refreshCaptcha()  // 验证码一次性失效，登录失败后刷新
      return
    }
    await setCfg({ serverUrl, token: data.token, card: data.card })
    setMsg('登录成功', 'ok')
    showMain()
  } catch (e) { setMsg('登录失败：' + e.message, 'err') }
}

$('logoutBtn').onclick = async () => {
  setMsg('正在退出…')
  const { serverUrl, token } = await getCfg()
  if (token) {
    try { await fetch(`${serverUrl}/api/auth/logout`, { method: 'POST', headers: { Authorization: 'Bearer ' + token } }) } catch (e) {}
  }
  await setCfg({ token: null, card: null })
  setMsg('已退出登录', 'ok')
  showMain()
}

// ── 公告：读取后台缓存的最新公告，未读则展示，关闭即标记已读（存 chrome.storage.local）──
function getAnnRead() { return new Promise(r => chrome.storage.local.get(['xm_announcement_read'], o => r(o.xm_announcement_read || []))) }

async function renderAnnouncement() {
  let ann = null
  try { const r = await swCall('pullAnnouncement'); ann = (r && r.announcement) || null } catch (e) {}
  if (!ann) {
    // 后台拉取失败/未登录 → 回退用缓存，避免漏展示
    const o = await new Promise(r => chrome.storage.local.get(['xm_announcement'], r))
    ann = o.xm_announcement || null
  }
  const read = await getAnnRead()
  const box = $('announcementBox')
  if (ann && !read.includes(ann.id)) {
    $('annTitle').textContent = ann.title || ''
    $('annContent').textContent = ann.content || ''
    box.style.display = 'block'
  } else {
    box.style.display = 'none'
  }
}

async function dismissAnnouncement() {
  const o = await new Promise(r => chrome.storage.local.get(['xm_announcement', 'xm_announcement_read'], r))
  const ann = o.xm_announcement
  const read = o.xm_announcement_read || []
  if (ann && !read.includes(ann.id)) {
    read.push(ann.id)
    await new Promise(r => chrome.storage.local.set({ xm_announcement_read: read.slice(-100) }, r))
  }
  $('announcementBox').style.display = 'none'
}

// ── 设置 ──
async function loadSettings() {
  let r
  try { r = await swCall('getSettings') } catch (e) { return }
  const s = (r && r.settings) || {}
  $('downloadPrefix').value = s.downloadPrefix || '有声下载'
  $('concurrency').value = s.concurrency || 1
  $('downloadDelay').value = s.downloadDelayMs ?? 500
  $('maxRetry').value = s.maxRetry || 3
  $('autoDownload').checked = !!s.autoDownload
  $('silentDownload').checked = s.silentDownload !== false
}

$('saveSettingsBtn').onclick = async () => {
  const prefix = $('downloadPrefix').value.trim()
  const concurrency = Math.max(1, Math.min(8, parseInt($('concurrency').value, 10) || 1))
  const dlRaw = parseInt($('downloadDelay').value, 10)
  const downloadDelayMs = Math.max(0, Math.min(120000, Number.isNaN(dlRaw) ? 500 : dlRaw))
  const maxRetry = Math.max(1, Math.min(10, parseInt($('maxRetry').value, 10) || 3))
  const autoDownload = $('autoDownload').checked
  const silentDownload = $('silentDownload').checked
  if (!prefix) { setMsg('下载路径前缀不能为空', 'err'); return }
  setMsg('保存中…')
  try {
    await swCall('setSettings', { settings: { downloadPrefix: prefix, concurrency, downloadDelayMs, maxRetry, autoDownload, silentDownload } })
  } catch (e) { setMsg('保存失败：' + e.message, 'err'); return }
  setMsg('✓ 设置已保存', 'ok')
}

// ── 顶部操作按钮 ──
$('refreshBtn').onclick = async () => {
  setMsg('正在拉取…')
  try {
    await swCall('pullTasks')
    await renderQueue()
    setMsg('拉取完成（已开启自动下载的新任务会自动开始）', 'ok')
  } catch (e) { setMsg('拉取失败：' + e.message, 'err') }
}

$('dlBtn').onclick = async () => {
  setMsg('已通知后台开始下载（走你的 IP）…')
  try {
    await swCall('downloadAll')
    await renderQueue()
  } catch (e) { setMsg('启动失败：' + e.message, 'err') }
}

$('pauseAllBtn').onclick = async () => {
  setMsg('正在全部暂停…')
  try {
    await swCall('pauseAll')
    await renderQueue()
    setMsg('已全部暂停', 'ok')
  } catch (e) { setMsg('操作失败：' + e.message, 'err') }
}

$('resumeAllBtn').onclick = async () => {
  setMsg('正在全部继续…')
  try {
    await swCall('resumeAll')
    await renderQueue()
    setMsg('已全部继续', 'ok')
  } catch (e) { setMsg('操作失败：' + e.message, 'err') }
}

$('clearBtn').onclick = async () => {
  setMsg('正在清空…')
  let r
  try { r = await swCall('clearDone') } catch (e) { setMsg('清空失败：' + e.message, 'err'); return }
  await renderQueue()
  const cleared = (r && r.cleared) || 0
  setMsg(cleared ? `已清空 ${cleared} 个已完成任务` : '没有已完成任务可清空', 'ok')
}

$('clearEndedBtn').onclick = async () => {
  setMsg('正在清空…')
  let r
  try { r = await swCall('clearEnded') } catch (e) { setMsg('清空失败：' + e.message, 'err'); return }
  await renderQueue()
  const cleared = (r && r.cleared) || 0
  setMsg(cleared ? `已清空 ${cleared} 个已结束任务（完成+取消）` : '没有已结束任务可清空', 'ok')
}

$('retryAllBtn').onclick = async () => {
  setMsg('正在重试全部失败项…')
  try {
    await swCall('retryAllFailed')
    await renderQueue()
    setMsg('已发起重试', 'ok')
  } catch (e) { setMsg('操作失败：' + e.message, 'err') }
}

// 任务操作（事件委托）
$('tasks').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]')
  if (!btn) return
  const taskId = btn.getAttribute('data-task-id')
  const action = btn.getAttribute('data-action')
  // 展开/收起明细：纯本地 UI 状态，不经过后台
  if (action === 'toggle') {
    if (expandedTasks.has(taskId)) expandedTasks.delete(taskId)
    else expandedTasks.add(taskId)
    await renderQueue()
    return
  }
  btn.disabled = true
  try {
    if (action === 'pause') await swCall('pauseTask', { taskId })
    else if (action === 'resume') await swCall('resumeTask', { taskId })
    else if (action === 'cancel') await swCall('cancelTask', { taskId })
    else if (action === 'retry') await swCall('retryFailed', { taskId })
    else if (action === 'start') await swCall('startTask', { taskId })
    await renderQueue()
  } catch (err) { setMsg('操作失败：' + err.message, 'err') }
})

// ── 渲染任务队列（含 Chrome 下载管理器的实时字节进度）──
async function renderQueue() {
  let r
  try { r = await swCall('getQueue') } catch (e) { setMsg(e.message, 'err'); return }
  const queue = r.queue || []

  // 实时字节进度（popup 打开期间直查 Chrome 下载管理器，无需后台持久化）
  let dlMap = {}
  try {
    const items = await chrome.downloads.search({ state: 'in_progress' })
    for (const it of items) dlMap[it.id] = it
  } catch (e) {}

  const box = $('tasks')
  box.innerHTML = ''
  if (!queue.length) {
    $('summary').textContent = ''
    box.innerHTML = '<div class="muted" style="margin-top:8px">暂无任务。前往网页端批量下载选择「本地下载」即可推送到这里。</div>'
    return
  }

  // 汇总行
  let sumTotal = 0, sumDone = 0, sumFailed = 0, sumActive = 0
  for (const t of queue) {
    const tracks = t.tracks || []
    sumTotal += tracks.length
    sumDone += tracks.filter(tr => tr.status === 'done').length
    sumFailed += tracks.filter(tr => tr.status === 'error').length
    sumActive += tracks.filter(tr => tr.status === 'downloading' || tr.status === 'resolving').length
  }
  $('summary').textContent = `任务 ${queue.length} 个 · 共 ${sumTotal} 集 · 完成 ${sumDone} · 失败 ${sumFailed} · 进行中 ${sumActive}`

  // 当前活跃书（与后台调度器同规则：第一本有进行中集的运行中任务；否则第一本有待下载集的运行中任务）
  // 多本书排队下载，非活跃书的 running 任务显示「排队中」
  let activeBookId = null
  for (const t of queue) {
    if (t.status !== 'running' || t.paused) continue
    if ((t.tracks || []).some(tr => tr.status === 'downloading' || tr.status === 'resolving')) { activeBookId = t.task_id; break }
  }
  if (!activeBookId) {
    for (const t of queue) {
      if (t.status !== 'running' || t.paused) continue
      if ((t.tracks || []).some(tr => tr.status === 'pending')) { activeBookId = t.task_id; break }
    }
  }

  const sorted = [...queue].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0))
  for (const t of sorted) {
    const tracks = t.tracks || []
    const div = document.createElement('div')
    div.className = 'task'
    const done = tracks.filter(tr => tr.status === 'done').length
    const failed = tracks.filter(tr => tr.status === 'error').length
    const total = tracks.length
    const downloading = tracks.filter(tr => tr.status === 'downloading').length
    const resolving = tracks.filter(tr => tr.status === 'resolving').length
    const pending = tracks.filter(tr => tr.status === 'pending').length

    // 进度条：完成数 + 下载中集的字节比例（平滑推进，不再 0% → 跳变）
    let frac = 0
    for (const tr of tracks) {
      if (tr.status !== 'downloading') continue
      const it = dlMap[tr.downloadId]
      if (it && it.totalBytes > 0) frac += Math.min(1, it.bytesReceived / it.totalBytes)
    }
    const pct = total ? Math.min(100, Math.round((done + failed + frac) / total * 100)) : 0

    const statusText = t.status === 'done' ? '已完成'
      : t.status === 'cancelled' ? '已取消'
      : t.paused ? '已暂停'
      : t.status === 'running' ? (t.task_id === activeBookId ? '下载中' : '排队中')
      : '待处理'

    // 操作按钮
    let actions = ''
    if (t.status === 'pending') actions += `<button class="mini" data-action="start" data-task-id="${t.task_id}">▶ 开始下载</button>`
    if (t.status === 'running' && !t.paused) actions += `<button class="mini" data-action="pause" data-task-id="${t.task_id}">⏸ 暂停</button>`
    if (t.paused) actions += `<button class="mini" data-action="resume" data-task-id="${t.task_id}">▶ 继续</button>`
    if (t.status !== 'done' && t.status !== 'cancelled') actions += `<button class="mini danger" data-action="cancel" data-task-id="${t.task_id}">✕ 取消</button>`
    if (failed > 0 && t.status !== 'cancelled') actions += `<button class="mini" data-action="retry" data-task-id="${t.task_id}">↻ 重试失败(${failed})</button>`

    // 单集明细：默认折叠（上千集不撑爆弹窗），点「展开明细」查看；
    // 展开状态存在 expandedTasks，1.5s 自动刷新重建 DOM 不会丢。
    // 展开时最多渲染 300 条，进行中/失败优先，避免千集全量渲染卡顿。
    const isOpen = expandedTasks.has(t.task_id)
    actions += `<button class="mini" data-action="toggle" data-task-id="${t.task_id}">${isOpen ? '▾ 收起明细' : '▸ 展开明细'}</button>`
    let tracksHtml = ''
    if (isOpen && tracks.length) {
      const MAX_ROWS = 300
      const act = tracks.filter(tr => tr.status === 'downloading' || tr.status === 'resolving')
      const err = tracks.filter(tr => tr.status === 'error')
      const rest = tracks.filter(tr => tr.status !== 'downloading' && tr.status !== 'resolving' && tr.status !== 'error')
      const show = [...act, ...err, ...rest].slice(0, MAX_ROWS)
      if (tracks.length > MAX_ROWS) {
        tracksHtml = `<div class="muted" style="margin-top:6px">共 ${tracks.length} 集，仅显示前 ${MAX_ROWS} 条（进行中/失败优先）</div>`
      }
      tracksHtml += '<div class="tracks">' + show.map(tr => trackRow(tr, dlMap)).join('') + '</div>'
    }

    const resolvingText = resolving ? ` · ${resolving} 解析中` : ''
    div.innerHTML = `
      <div class="task-head"><b>${escapeHtml(t.album_title || ('专辑 ' + t.album_id))}</b><span class="muted"> · ${escapeHtml(t.source)} · ${statusText}</span></div>
      <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
      <div class="muted">${done}/${total} 完成 · ${failed} 失败 · ${downloading} 下载中${resolvingText} · ${pending} 待处理 · ${pct}%</div>
      <div class="actions">${actions}</div>
      ${tracksHtml}
    `
    box.appendChild(div)
  }
}

function trackRow(tr, dlMap) {
  const rawStatus = tr.status || 'pending'
  const dotCls = rawStatus === 'resolving' ? 'downloading' : rawStatus
  const err = tr.error ? `<span class="err" title="${escapeHtml(tr.error)}">⚠ ${escapeHtml(tr.error.slice(0, 80))}</span>` : ''
  let extra = ''
  if (rawStatus === 'resolving') {
    extra = '<span class="bytes">解析中…</span>'
  } else if (rawStatus === 'downloading') {
    const it = dlMap && dlMap[tr.downloadId]
    if (it) {
      const totalTxt = it.totalBytes > 0 ? fmtBytes(it.totalBytes) : '?'
      extra = `<span class="bytes">${it.paused ? '⏸ ' : ''}${fmtBytes(it.bytesReceived)}/${totalTxt}</span>`
    }
  }
  return `<div class="tr"><span class="dot ${dotCls}"></span><span class="tt">${escapeHtml(String(tr.episode_num || tr.track_id || ''))}. ${escapeHtml(tr.title || '')}</span>${extra}${err}</div>`
}

$('annAck').onclick = dismissAnnouncement
$('annClose').onclick = dismissAnnouncement

// ── 连接状态 ──
async function startConn() {
  stopConn()
  const tick = async () => {
    try {
      const r = await swCall('ping')
      const el = $('conn')
      if (r.ok) { el.textContent = '● 已连接'; el.className = 'conn ok' }
      else { el.textContent = '● ' + (r.msg || '异常'); el.className = 'conn err' }
    } catch (e) {
      const el = $('conn'); el.textContent = '● 未连接'; el.className = 'conn err'
    }
  }
  await tick()
  connTimer = setInterval(tick, 10000)
}
function stopConn() { if (connTimer) { clearInterval(connTimer); connTimer = null } }

function startAutoRefresh() {
  stopAutoRefresh()
  refreshTimer = setInterval(renderQueue, 1500)
}
function stopAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
}

// popup 关闭时停止轮询
window.addEventListener('beforeunload', () => { stopAutoRefresh(); stopConn() })

showMain()
