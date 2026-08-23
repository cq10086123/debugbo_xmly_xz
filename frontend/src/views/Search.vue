<script setup>
import { ref, reactive, watch, onMounted, computed } from 'vue'
import { bizApi, getBizToken } from '../utils/request'
import { useToast } from '../utils/toast'
import { useAuthStore } from '../stores/auth'

const toast = useToast()
const auth = useAuthStore()

// 接口列表从 /api/interfaces 动态拉取
const interfaces = ref([])
const loadingInterfaces = ref(false)
const notLoggedIn = ref(false) // 未登录卡密：不显示接口，仅提示
const tab = ref('') // 当前选中的接口 name
const currentInterface = computed(() => interfaces.value.find(it => it.name === tab.value) || null)
const isOfficial = computed(() => currentInterface.value?.type === 'official')

async function loadInterfaces() {
  loadingInterfaces.value = true
  try {
    const r = await bizApi.get('/interfaces')
    if (r.data.success) {
      interfaces.value = (r.data.interfaces || []).filter(it => it.enabled)
      // 默认选中第一个；若当前选中的不在列表中，也重置为第一个
      if (!tab.value || !interfaces.value.find(it => it.name === tab.value)) {
        tab.value = interfaces.value[0]?.name || ''
      }
      // 未登录卡密时不显示接口（官方接口需卡密授权）
      notLoggedIn.value = !getBizToken()
    }
  } catch (e) {
    toast.error('加载接口列表失败：' + (e.response?.data?.error || e.message))
  } finally {
    loadingInterfaces.value = false
  }
}
onMounted(loadInterfaces)

// 官方下载需当前卡密已登录账号
const hasAccount = ref(false)
async function checkAccount() {
  if (!isOfficial.value) { hasAccount.value = false; return }
  try {
    const r = await bizApi.get('/accounts')
    hasAccount.value = !!(r.data.success && (r.data.accounts || []).length)
  } catch { hasAccount.value = false }
}
watch(tab, checkAccount, { immediate: true })

// 搜索结果
const keyword = ref('')
const searching = ref(false)
const results = ref([])

// 选中专辑后的章节列表
const album = reactive({
  id: '',
  title: '',
  tracks: [],
  total: 0,
  loading: false,
})

// 批量下载参数
const batch = reactive({
  mode: 'server', // server=离线下载到服务器 | local=本地下载到浏览器插件
  start: 1,
  end: '',
  fmt: 'mp3',
  quality: 0,
  concurrency: 1,
  submitting: false,
})

// 卡密下载模式权限：server/local/both（缺省 both）。据此禁用不被允许的下载方式 radio。
const cardMode = computed(() => auth.card?.download_mode || 'both')
// 当卡密被限定为单一模式时，自动把默认下载方式切到该模式（避免默认 server 被 local-only 卡误拦）
watch(cardMode, (m) => {
  if (m === 'server') batch.mode = 'server'
  else if (m === 'local') batch.mode = 'local'
}, { immediate: true })

function resetAlbum() {
  album.id = ''
  album.title = ''
  album.tracks = []
  album.total = 0
}
watch(tab, resetAlbum)

async function doSearch() {
  const kw = keyword.value.trim()
  if (!kw) { toast.error('请输入搜索关键词'); return }
  if (!currentInterface.value) { toast.error('没有可用的下载接口'); return }
  searching.value = true
  results.value = []
  try {
    let data
    if (isOfficial.value) {
      const r = await bizApi.get('/search', { params: { keyword: kw, page: 1 } })
      data = r.data
    } else {
      const r = await bizApi.get(`/intf/${currentInterface.value.name}/search`, { params: { keyword: kw } })
      data = r.data
    }
    if (data.success) results.value = data.results || []
    if (!results.value.length) toast.info('未找到相关结果')
  } catch (e) {
    toast.error('搜索失败：' + (e.response?.data?.error || e.message))
  } finally {
    searching.value = false
  }
}

// 请求序号：防止快速连点两张专辑时慢响应后写入，出现「标题 B 章节 A」错配
let albumReqSeq = 0
function selectAlbum(item) {
  album.id = item.albumId || item.bookId || item.id
  album.title = item.title
  fetchAlbumList()
}

async function fetchAlbumList() {
  if (!album.id || !currentInterface.value) return
  const seq = ++albumReqSeq
  album.loading = true
  album.tracks = []
  try {
    let data
    const id = isOfficial.value ? Number(album.id) : String(album.id)
    if (isOfficial.value) {
      const r = await bizApi.post('/download/album-list', { album_id: id })
      data = r.data
    } else {
      const r = await bizApi.post(`/intf/${currentInterface.value.name}/album-list`, { book_id: id })
      data = r.data
    }
    if (data.success) {
      if (seq !== albumReqSeq) return  // 已有更新的请求发出，丢弃本次慢响应
      album.tracks = data.tracks || []
      album.total = data.track_total || album.tracks.length
    } else {
      toast.error(data.error || '获取章节失败')
    }
  } catch (e) {
    toast.error('获取章节失败：' + (e.response?.data?.error || e.message))
  } finally {
    album.loading = false
  }
}

async function startBatch() {
  if (!album.id) { toast.error('请先选择专辑'); return }
  if (isOfficial.value && !hasAccount.value) {
    toast.error('请先在「我的账号」页扫码登录喜马拉雅账号后再使用官方下载')
    return
  }
  batch.submitting = true
  try {
    const id = isOfficial.value ? Number(album.id) : String(album.id)
    const start = Number(batch.start) || 1
    const end = batch.end ? Number(batch.end) : Infinity

    // 模式一：本地下载（浏览器插件）—— 仅推送章节元数据，不占服务器空间、走用户 IP
    if (batch.mode === 'local') {
      // 官方接口返回的 track 没有 index/order/episode_num，按排序后的数组下标 +1 作为集号（与后端 download_by_chapter 一致）
      const tracks = album.tracks
        .map((t, idx) => ({ ...t, _ep: t.index ?? t.order ?? t.episode_num ?? (idx + 1) }))
        .filter(t => t._ep >= start && t._ep <= end)
        .map(t => ({
          track_id: String(t.trackId ?? t.id ?? ''),
          episode_num: t._ep,
          title: t.title || '',
          fmt: batch.fmt,
        }))
        .filter(t => t.track_id)
      if (!tracks.length) { toast.error('当前范围没有可下载章节'); return }
      const r = await bizApi.post('/extension/task', {
        source: isOfficial.value ? 'official' : (currentInterface.value?.name || ''),
        album_id: String(id),
        album_title: album.title,
        quality: batch.quality,
        fmt: batch.fmt,
        tracks,
      })
      if (r.data.success) {
        toast.success(`✅ 已推送到本地浏览器插件（${r.data.count} 集），请打开插件下载（走你的 IP，不占服务器空间）`)
      } else {
        toast.error(r.data.error || '推送失败')
      }
      return
    }

    // 模式二：离线下载到服务器（原有逻辑）
    let url, payload
    if (isOfficial.value) {
      url = '/download/batch'
      payload = {
        album_id: id,
        quality: batch.quality,
        start_episode: start,
        end_episode: batch.end ? Number(batch.end) : null,
        fmt: batch.fmt,
      }
    } else {
      url = `/intf/${currentInterface.value.name}/batch`
      payload = {
        book_id: id,
        start_episode: start,
        end_episode: batch.end ? Number(batch.end) : null,
        fmt: batch.fmt,
        concurrency: batch.concurrency,
      }
    }
    const r = await bizApi.post(url, payload)
    if (r.data.success) {
      toast.success(`已创建任务 ${r.data.task_id}，前往任务页查看进度`)
    } else {
      toast.error(r.data.error || '创建任务失败')
    }
  } catch (e) {
    toast.error('创建失败：' + (e.response?.data?.error || e.message))
  } finally {
    batch.submitting = false
  }
}
</script>

<template>
  <div>
    <div class="card-panel pad">
      <div v-if="loadingInterfaces" class="empty-state">加载接口列表…</div>
      <div v-else-if="notLoggedIn" class="empty-state login-hint">
        🔒 请先<router-link to="/login">登录卡密</router-link>后使用下载功能。
      </div>
      <div v-else-if="!interfaces.length" class="empty-state">
        暂无可用接口，请前往后台「🧩 接口管理」添加。
      </div>

      <template v-else>
        <div class="tabs">
          <button
            v-for="it in interfaces"
            :key="it.name"
            class="tab"
            :class="{ on: tab === it.name }"
            @click="tab = it.name"
          >
            {{ it.display_name || it.name }}
            <span v-if="it.type === 'official'" class="tag">VIP</span>
          </button>
        </div>

        <div v-if="isOfficial && !hasAccount" class="acct-hint">
          ⚠️ 官方下载需先登录账号：请前往
          <router-link to="/account">「我的账号」</router-link>
          扫码登录你的喜马拉雅账号后再使用。
        </div>

        <div class="search-row">
          <input v-model="keyword" placeholder="输入书名 / 主播 / 关键词" @keyup.enter="doSearch" style="flex:1" />
          <button :disabled="searching" @click="doSearch">{{ searching ? '搜索中…' : '搜索' }}</button>
        </div>
      </template>

      <div v-if="results.length" class="results">
        <div v-for="item in results" :key="item.albumId||item.bookId||item.id" class="result" @click="selectAlbum(item)">
          <div class="cover">
            <img v-if="item.cover" :src="item.cover" alt="" loading="lazy"
                 @error="(e)=>{e.target.style.display='none'}" />
            <span v-else class="ph">♪</span>
          </div>
          <div class="meta">
            <div class="title">{{ item.title }}</div>
            <div class="sub muted">
              <span v-if="item.author">🎤 {{ item.author }}</span>
              <span v-if="item.type">· {{ item.type }}</span>
              <span v-if="item.trackCount || item.total">· {{ item.trackCount || item.total }} 集</span>
            </div>
            <div class="id muted">ID: {{ item.albumId || item.bookId || item.id }}</div>
          </div>
        </div>
      </div>
      <div v-else-if="!searching && interfaces.length" class="empty-state">搜索后在此显示结果</div>
    </div>

    <div class="card-panel pad" v-if="album.id">
      <div class="album-hd">
        <h3>📚 {{ album.title }}</h3>
        <span class="muted">共 {{ album.total || album.tracks.length }} 集</span>
      </div>

      <div v-if="album.loading" class="empty-state">加载章节中…</div>
      <div v-else-if="album.tracks.length" class="tracks">
        <div v-for="t in album.tracks" :key="t.index||t.trackId||t.id" class="track">
          <span class="idx">{{ t.index || t.order || (t.episode_num) || '·' }}</span>
          <span class="tt">{{ t.title }}</span>
          <span class="dur muted" v-if="t.duration">{{ t.duration }}</span>
        </div>
      </div>

      <div class="batch-box">
        <div class="mode-row">
          <span class="mode-label">下载方式</span>
          <label class="mode-opt">
            <input type="radio" value="server" v-model="batch.mode" :disabled="cardMode === 'local'" />
            <span>💾 离线下载（容易触发限流）</span>
            <span v-if="cardMode === 'local'" class="muted" style="font-size:12px">（本卡未授权）</span>
          </label>
          <label class="mode-opt">
            <input type="radio" value="local" v-model="batch.mode" :disabled="cardMode === 'server'" />
            <span>🌐 本地下载（浏览器插件 · 不易触发限流）</span>
            <span v-if="cardMode === 'server'" class="muted" style="font-size:12px">（本卡未授权）</span>
          </label>
        </div>
        <div v-if="batch.mode==='local'" class="mode-tip">
          ℹ️ 选择后任务会推送到你的浏览器插件，由插件在你本机解析音频并下载，节省服务器空间、使用你的公网 IP。
          请确保已在浏览器安装并登录本插件。<b>下载并发数由插件设置控制</b>（插件弹窗 → 设置 → 同时下载数量），此处无需选择。
          相同专辑+相同集范围重复推送会自动去重，不会重复下载。
        </div>
        <div class="row">
          <label>起始集 <input v-model="batch.start" type="number" min="1" style="width:90px" /></label>
          <label>结束集 <input v-model="batch.end" type="number" min="1" style="width:90px" placeholder="留空=全部" /></label>
          <label>格式
            <select v-model="batch.fmt">
              <option value="mp3">mp3</option>
              <option value="m4a">m4a</option>
            </select>
          </label>
          <label v-if="isOfficial">音质
            <select v-model.number="batch.quality">
              <option :value="0">标准</option>
              <option :value="1">高</option>
              <option :value="2">超高</option>
            </select>
          </label>
          <label v-else-if="batch.mode==='server'">并发
            <select v-model.number="batch.concurrency">
              <option :value="1">1</option>
              <option :value="2">2</option>
              <option :value="3">3</option>
            </select>
          </label>
        </div>
        <button class="success" :disabled="batch.submitting || (isOfficial && !hasAccount)" @click="startBatch">
          {{ batch.submitting ? '提交中…' : (isOfficial && !hasAccount ? '请先登录账号' : '🚀 开始批量下载') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 20px; margin-bottom: 18px; }
.tabs { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.acct-hint { background: rgba(255,170,60,.12); border: 1px solid rgba(255,170,60,.35); color: #ffc266; padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 14px; }
.acct-hint a { color: #ffd98a; text-decoration: underline; }
.login-hint { font-size: 14px; }
.login-hint a { color: var(--primary); text-decoration: underline; }
.tab { background: transparent; color: var(--text-dim); border: 1px solid var(--border); box-shadow: none; display: inline-flex; align-items: center; gap: 6px; }
.tab.on { background: linear-gradient(135deg, var(--primary), var(--primary-2)); color: #fff; border-color: transparent; }
.tab .tag { font-size: 10px; background: rgba(255,255,255,.18); padding: 1px 5px; border-radius: 4px; }
.search-row { display: flex; gap: 10px; }
.results { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.result {
  display: flex; gap: 12px; padding: 10px; border: 1px solid var(--border);
  border-radius: 10px; cursor: pointer; transition: all .15s ease; background: var(--bg-soft);
}
.result:hover { border-color: var(--primary); transform: translateY(-2px); }
.cover { width: 56px; height: 56px; flex-shrink: 0; border-radius: 8px; overflow: hidden; background: var(--panel-2); display:flex; align-items:center; justify-content:center; }
.cover img { width: 100%; height: 100%; object-fit: cover; }
.ph { font-size: 22px; color: var(--text-dim); }
.meta { min-width: 0; }
.title { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sub { font-size: 12px; margin-top: 3px; display: flex; gap: 6px; flex-wrap: wrap; }
.id { font-size: 11px; margin-top: 3px; }
.album-hd { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; }
.album-hd h3 { margin: 0; }
.tracks { max-height: 320px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; margin-bottom: 16px; }
.track { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 8px; }
.track:hover { background: var(--bg-soft); }
.idx { width: 34px; color: var(--text-dim); font-variant-numeric: tabular-nums; flex-shrink: 0; }
.tt { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dur { font-size: 12px; }
.batch-box { border-top: 1px solid var(--border); padding-top: 16px; }
.mode-row { display: flex; gap: 18px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
.mode-label { font-size: 13px; color: var(--text-dim); }
.mode-opt { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
.mode-opt input { accent-color: var(--primary); }
.mode-tip { font-size: 12px; line-height: 1.6; color: var(--text-dim); background: var(--bg-soft); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin-bottom: 14px; }
.row { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
.row label { font-size: 13px; color: var(--text-dim); display: flex; align-items: center; gap: 6px; }
</style>
