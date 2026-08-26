<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { bizApi, getBizToken } from '../utils/request'
import { useToast } from '../utils/toast'

const toast = useToast()
const files = ref([])       // 顶层散文件
const albums = ref([])      // 专辑分组
const loading = ref(true)
const cleaning = ref(false)
const albumExpanded = ref({}) // 专辑名 -> 是否展开，默认折叠
const quarkAllowed = ref(false)
const quarkMounted = ref(false)
const quarkMountError = ref('')
const syncJobs = ref({})    // album -> {job_id, status, done, total, error}
let pollTimer = null

function isExpanded(name) { return !!albumExpanded.value[name] }
function toggleAlbum(name) { albumExpanded.value[name] = !isExpanded(name) }
function expandAll() { albums.value.forEach((al) => { albumExpanded.value[al.name] = true }) }
function collapseAll() { albums.value.forEach((al) => { albumExpanded.value[al.name] = false }) }

async function load() {
  loading.value = true
  try {
    const r = await bizApi.get('/files')
    if (r.data.success) {
      files.value = r.data.files || []
      albums.value = r.data.albums || []
    }
  } catch (e) {}
  loading.value = false
}

function fmtSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i]
}

function downloadZip(albumName) {
  const token = getBizToken()
  if (!token) { toast.error('未登录或登录已失效'); return }
  // 原生下载：直接让浏览器请求带 token 的 URL 并流式写入磁盘，
  // 不经过 fetch/blob，避免大文件占满内存或被提前 revoke 导致下载中断。
  // 原生导航无法带自定义请求头 → 设备 ID 同样走查询参数（服务端 get_current_card_download 支持）。
  const url = `/api/files/album-zip?album=${encodeURIComponent(albumName)}&token=${encodeURIComponent(token)}`
  const a = document.createElement('a')
  a.href = url
  a.download = `${albumName}.zip`
  document.body.appendChild(a)
  a.click()
  a.remove()
  toast.success('已开始下载 ZIP，请按 Ctrl+J（或 ⌘+J）查看浏览器下载管理器，大文件需等待片刻')
}

function downloadFile(path, name) {
  const token = getBizToken()
  if (!token) { toast.error('未登录或登录已失效'); return }
  const url = `/api/files/file/${encodeURIComponent(path)}?token=${encodeURIComponent(token)}`
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  toast.success('已开始下载，请按 Ctrl+J（或 ⌘+J）查看')
}

async function delFile(path) {
  if (!confirm('确认删除该文件？')) return
  try {
    const r = await bizApi.delete(`/files/${encodeURIComponent(path)}`)
    if (r.data.success) { toast.success('已删除'); load() }
    else toast.error(r.data.error || '删除失败')
  } catch (e) { toast.error('删除失败') }
}

async function cleanup() {
  cleaning.value = true
  try {
    const r = await bizApi.post('/files/cleanup-duplicates')
    if (r.data.success) {
      toast.success(`清理重复文件 ${r.data.deleted_count} 个`)
      load()
    }
  } catch (e) { toast.error('清理失败') }
  finally { cleaning.value = false }
}

async function loadQuark() {
  try {
    const r = await bizApi.get('/quark/status')
    if (r.data.success) {
      quarkAllowed.value = !!r.data.allowed
      quarkMounted.value = !!r.data.mounted
      quarkMountError.value = r.data.mount_error || ''
      const running = r.data.running
      if (running && running.album) {
        syncJobs.value = { ...syncJobs.value, [running.album]: running }
        if (running.status === 'running') startPoll()
      }
    }
  } catch (e) {}
}

async function syncAlbum(albumName) {
  if (!quarkAllowed.value) return
  if (!quarkMounted.value) {
    toast.error(quarkMountError.value || '夸克挂载目录不可用')
    return
  }
  try {
    const r = await bizApi.post('/quark/sync', { album: albumName })
    if (r.data.success) {
      syncJobs.value = { ...syncJobs.value, [albumName]: r.data }
      toast.success('已开始同步到夸克，本地文件会保留')
      startPoll()
    } else {
      toast.error(r.data.error || r.data.detail || '同步失败')
    }
  } catch (e) {
    toast.error(e.response?.data?.detail || '同步失败')
  }
}

function syncLabel(albumName) {
  const j = syncJobs.value[albumName]
  if (!j) return '同步到夸克'
  if (j.status === 'running') {
    const t = j.total || 0
    const d = j.done || 0
    return t ? `同步中 ${d}/${t}` : '同步中…'
  }
  if (j.status === 'done') return '已同步'
  if (j.status === 'failed') return '重试同步'
  return '同步到夸克'
}

function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    const running = Object.values(syncJobs.value).filter((j) => j && j.status === 'running' && j.job_id)
    if (!running.length) {
      clearInterval(pollTimer)
      pollTimer = null
      return
    }
    for (const j of running) {
      try {
        const r = await bizApi.get(`/quark/jobs/${j.job_id}`)
        if (r.data.success) {
          const album = r.data.album || j.album
          syncJobs.value = { ...syncJobs.value, [album]: r.data }
          if (r.data.status === 'done') toast.success(`《${album}》已同步到夸克`)
          if (r.data.status === 'failed') toast.error(r.data.error || '同步失败')
        }
      } catch (e) {
        const code = e.response && e.response.status
        if (code === 404) {
          const album = j.album
          if (album) {
            syncJobs.value = { ...syncJobs.value, [album]: { ...j, status: 'failed', error: '任务已丢失（可能服务重启）' } }
          }
          toast.error('同步任务已丢失，请重试')
        }
      }
    }
  }, 1200)
}

onMounted(() => {
  load()
  loadQuark()
})
onUnmounted(() => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
})
</script>

<template>
  <div>
    <div class="card-panel pad head">
      <div>
        <h3 style="margin:0 0 4px">📁 文件管理</h3>
        <p class="muted" style="margin:0">仅显示当前卡密下载目录下的文件。</p>
      </div>
      <div class="head-ops">
        <button class="ghost" @click="expandAll">展开全部</button>
        <button class="ghost" @click="collapseAll">折叠全部</button>
        <button class="ghost" :disabled="cleaning" @click="cleanup">{{ cleaning ? '清理中…' : '🧹 去重清理' }}</button>
      </div>
    </div>

    <div v-if="loading" class="empty-state">加载中…</div>

    <div v-else>
      <div v-for="al in albums" :key="al.name" class="card-panel pad album">
        <div class="album-hd">
          <div class="album-title" @click="toggleAlbum(al.name)">
            <span class="toggle-icon">{{ isExpanded(al.name) ? '▼' : '▶' }}</span>
            <div>
              <div class="name">📚 {{ al.name }}</div>
              <div class="muted" style="font-size:12px">{{ al.count }} 集 · {{ fmtSize(al.total_size) }}</div>
            </div>
          </div>
          <div class="album-ops">
            <button
              v-if="quarkAllowed"
              class="ghost"
              :disabled="syncJobs[al.name]?.status === 'running' || !quarkMounted"
              :title="quarkMounted ? '' : (quarkMountError || '夸克挂载不可用')"
              @click.stop="syncAlbum(al.name)"
            >☁ {{ syncLabel(al.name) }}</button>
            <button class="success" @click.stop="downloadZip(al.name)">⬇ 下载 ZIP</button>
          </div>
        </div>
        <div v-if="isExpanded(al.name)" class="file-grid">
          <div v-for="f in al.files" :key="f.path" class="file">
            <span class="fname" :title="f.name">{{ f.name }}</span>
            <span class="fsize muted">{{ fmtSize(f.size) }}</span>
            <span class="fops">
              <a @click="downloadFile(f.path, f.name)">下载</a>
              <a class="del" @click="delFile(f.path)">删除</a>
            </span>
          </div>
        </div>
      </div>

      <div v-if="files.length" class="card-panel pad">
        <div class="name" style="margin-bottom:10px">散文件</div>
        <div class="file-grid">
          <div v-for="f in files" :key="f.path" class="file">
            <span class="fname" :title="f.name">{{ f.name }}</span>
            <span class="fsize muted">{{ fmtSize(f.size) }}</span>
            <span class="fops">
              <a @click="downloadFile(f.path, f.name)">下载</a>
              <a class="del" @click="delFile(f.path)">删除</a>
            </span>
          </div>
        </div>
      </div>

      <div v-if="!albums.length && !files.length" class="empty-state">还没有下载任何文件，去搜索页开始吧。</div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 18px 20px; }
.head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.album { margin-bottom: 16px; }
.album-hd { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 12px; }
.album-ops { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.name { font-weight: 700; }
.file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
.file { display: flex; align-items: center; gap: 8px; padding: 7px 10px; background: var(--bg-soft); border: 1px solid var(--border); border-radius: 8px; }
.fname { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fsize { font-size: 11px; }
.fops { display: flex; gap: 8px; font-size: 12px; }
.fops a { cursor: pointer; }
.fops .del { color: var(--danger); }
.head-ops { display: flex; gap: 8px; align-items: center; }
.album-title { display: flex; align-items: center; gap: 10px; cursor: pointer; flex: 1; min-width: 0; }
.album-title:hover .toggle-icon { opacity: 0.7; }
.toggle-icon { font-size: 12px; color: var(--muted); user-select: none; width: 14px; text-align: center; transition: opacity .15s; }
</style>
