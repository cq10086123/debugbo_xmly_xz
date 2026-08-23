<script setup>
import { ref, reactive, onMounted } from 'vue'
import { adminApi } from '../../utils/request'
import { useToast } from '../../utils/toast'

const toast = useToast()
const cards = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const statusFilter = ref('')
const keyword = ref('')
const loading = ref(false)

// 可绑定的接口清单（从接口管理拉取）
const interfaces = ref([])

const gen = reactive({ count: 1, expiry_type: 'fixed', expires_at: '', valid_days: 30, note: '', interface_names: [], inject_xm_cookie: false, download_mode: 'both', max_devices: 1, generating: false })
const genResult = ref('')

// 绑定编辑弹窗
const bindEditor = reactive({ show: false, card: null, selected: [], download_mode: 'both', max_devices: 1, saving: false })

// 网络绑定管理弹窗
const devMgr = reactive({ show: false, card: null, loading: false, data: null, busy: false })

async function loadInterfaces() {
  try {
    const r = await adminApi.get('/interfaces')
    if (r.data.success) interfaces.value = r.data.interfaces
  } catch (e) {}
}

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize }
    if (statusFilter.value) params.status = statusFilter.value
    if (keyword.value) params.keyword = keyword.value
    const r = await adminApi.get('/cards', { params })
    if (r.data.success) {
      cards.value = r.data.cards
      total.value = r.data.total
    }
  } catch (e) {}
  loading.value = false
}

async function generate() {
  if (gen.expiry_type === 'fixed' && !gen.expires_at) { toast.error('请选择到期时间'); return }
  const md = Math.floor(Number(gen.max_devices))
  if (!(md >= 1 && md <= 10)) { toast.error('网络数需在 1~10 之间'); return }
  gen.generating = true
  genResult.value = ''
  try {
    const payload = {
      count: Number(gen.count) || 1,
      expiry_type: gen.expiry_type,
      note: gen.note || null,
    }
    if (gen.expiry_type === 'fixed') payload.expires_at = new Date(gen.expires_at).toISOString()
    else payload.valid_days = Number(gen.valid_days) || 1
    if (gen.interface_names.length) payload.interface_names = [...gen.interface_names]
    if (gen.inject_xm_cookie) payload.inject_xm_cookie = true
    payload.download_mode = gen.download_mode
    payload.max_devices = md
    const r = await adminApi.post('/cards/generate', payload)
    if (r.data.success) {
      genResult.value = r.data.codes.join('\n')
      toast.success(`已生成 ${r.data.count} 张卡密`)
      load()
    } else toast.error(r.data.error || '生成失败')
  } catch (e) { toast.error(e.response?.data?.detail || '生成失败') }
  finally { gen.generating = false }
}

async function patchCard(id, body, okMsg) {
  try {
    const r = await adminApi.patch(`/cards/${id}`, body)
    if (r.data.success) { toast.success(okMsg); load() }
    else toast.error(r.data.error || '操作失败')
  } catch (e) { toast.error(e.response?.data?.detail || '操作失败') }
}

function toggleDisable(c) {
  patchCard(c.id ?? c.code, { status: c.status === 'disabled' ? 'enable' : 'disable' },
    c.status === 'disabled' ? '已启用' : '已禁用')
}

async function renewCard(c) {
  const input = prompt(`为卡密 ${c.code} 续期（输入顺延天数）：`, '30')
  if (input === null) return
  const days = Number(input)
  if (!Number.isInteger(days) || days < 1) { toast.error('请输入正整数天数'); return }
  try {
    const r = await adminApi.post(`/cards/${c.id}/renew`, { days })
    if (r.data.success) {
      toast.success(r.data.message || '续期成功')
      load()
    } else toast.error(r.data.error || '续期失败')
  } catch (e) { toast.error(e.response?.data?.detail || '续期失败') }
}

function openBindEditor(c) {
  bindEditor.card = c
  bindEditor.selected = [...(c.bound_interfaces || [])]
  bindEditor.download_mode = c.download_mode || 'both'
  bindEditor.max_devices = c.max_devices || 1
  bindEditor.show = true
}

async function saveBinding() {
  const md = Math.floor(Number(bindEditor.max_devices))
  if (!(md >= 1 && md <= 10)) { toast.error('网络数需在 1~10 之间'); return }
  bindEditor.saving = true
  try {
    const body = { interface_names: [...bindEditor.selected] }
    body.download_mode = bindEditor.download_mode
    body.max_devices = md
    const r = await adminApi.patch(`/cards/${bindEditor.card.id}`, body)
    if (r.data.success) {
      toast.success(bindEditor.selected.length ? '绑定已更新' : '已取消接口限制')
      bindEditor.show = false
      load()
    } else toast.error(r.data.error || '保存失败')
  } catch (e) { toast.error(e.response?.data?.detail || '保存失败') }
  finally { bindEditor.saving = false }
}

// ── 网络绑定管理 ──
async function openDeviceMgr(c) {
  devMgr.card = c
  devMgr.show = true
  await loadDevices()
}

async function loadDevices() {
  if (!devMgr.card) return
  devMgr.loading = true
  try {
    const r = await adminApi.get(`/cards/${devMgr.card.id}/devices`)
    if (r.data.success) devMgr.data = r.data
  } catch (e) { toast.error(e.response?.data?.detail || '绑定信息加载失败') }
  devMgr.loading = false
}

async function unbindDevice(netKey) {
  const label = netKey.includes('/') || netKey === 'lan' ? `网络 ${netKey}` : `旧版设备 …${netKey.slice(-8)}`
  if (!confirm(`确认解绑${label}？其下会话将立即下线，需重新登录。`)) return
  devMgr.busy = true
  try {
    const r = await adminApi.post(`/cards/${devMgr.card.id}/devices/unbind`, { device_id: netKey })
    if (r.data.success) { toast.success('已解绑并下线'); loadDevices() }
    else toast.error(r.data.error || '解绑失败')
  } catch (e) { toast.error(e.response?.data?.detail || '解绑失败') }
  finally { devMgr.busy = false }
}

async function unbindAllDevices() {
  if (!confirm('确认解绑全部网络并踢下线？用户重新登录即可自动重新绑定。')) return
  devMgr.busy = true
  try {
    const r = await adminApi.post(`/cards/${devMgr.card.id}/devices/unbind-all`)
    if (r.data.success) { toast.success(r.data.message || '已解绑全部网络'); loadDevices() }
    else toast.error(r.data.error || '操作失败')
  } catch (e) { toast.error(e.response?.data?.detail || '操作失败') }
  finally { devMgr.busy = false }
}

async function kickSessions() {
  if (!confirm('确认踢下线全部会话？网络绑定保留，用户在原网络重新登录即可恢复。')) return
  devMgr.busy = true
  try {
    const r = await adminApi.post(`/cards/${devMgr.card.id}/kick`)
    if (r.data.success) { toast.success(r.data.message || '已踢下线'); loadDevices() }
    else toast.error(r.data.error || '操作失败')
  } catch (e) { toast.error(e.response?.data?.detail || '操作失败') }
  finally { devMgr.busy = false }
}

async function delCard(id) {
  if (!confirm('确认删除该卡密？其任务与记录将一并删除（磁盘文件保留）。')) return
  try {
    const r = await adminApi.delete(`/cards/${id}`)
    if (r.data.success) { toast.success('已删除'); load() }
    else toast.error(r.data.error || '删除失败')
  } catch (e) { toast.error('删除失败') }
}

function fmtDate(s) {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

function fmtBound(c) {
  if (!c.bound_interfaces || !c.bound_interfaces.length) return '全部接口'
  return c.bound_interfaces
    .map((n) => (interfaces.value.find((i) => i.name === n)?.display_name) || n)
    .join('、')
}

function fmtMode(c) {
  const m = c.download_mode || 'both'
  if (m === 'server') return '仅服务器'
  if (m === 'local') return '仅本地'
  return '都允许'
}

function copyCodes() {
  if (genResult.value) {
    navigator.clipboard?.writeText(genResult.value)
    toast.success('已复制卡密')
  }
}

onMounted(() => { load(); loadInterfaces() })
</script>

<template>
  <div>
    <div class="card-panel pad">
      <h3 style="margin:0 0 14px">🔑 生成卡密</h3>
      <div class="gen-row">
        <label>数量 <input v-model.number="gen.count" type="number" min="1" max="500" style="width:80px" /></label>
        <label>类型
          <select v-model="gen.expiry_type">
            <option value="fixed">固定到期</option>
            <option value="days">激活后 N 天</option>
          </select>
        </label>
        <label v-if="gen.expiry_type==='fixed'">到期时间
          <input v-model="gen.expires_at" type="datetime-local" />
        </label>
        <label v-else>有效天数 <input v-model.number="gen.valid_days" type="number" min="1" style="width:80px" /></label>
        <label>备注 <input v-model="gen.note" placeholder="可选" style="width:140px" /></label>
        <label>下载模式
          <select v-model="gen.download_mode">
            <option value="both">都允许</option>
            <option value="server">仅服务器下载</option>
            <option value="local">仅本地下载</option>
          </select>
        </label>
        <label>网络数
          <input v-model.number="gen.max_devices" type="number" min="1" max="10" step="1" style="width:80px" title="允许同时绑定的网络（出口IP）数，1~10；同一网络下不限设备" />
        </label>
        <button class="success" :disabled="gen.generating" @click="generate">{{ gen.generating ? '生成中…' : '生成' }}</button>
      </div>
      <div class="bind-row">
        <span class="muted">绑定接口（不勾选 = 不限制，可使用全部接口）：</span>
        <label v-for="it in interfaces" :key="it.name" class="bind-item">
          <input type="checkbox" :value="it.name" v-model="gen.interface_names" />
          <span>{{ it.display_name }}</span>
        </label>
        <label class="bind-item" style="margin-left:auto">
          <input type="checkbox" v-model="gen.inject_xm_cookie" />
          <span>注入后端 Cookie（免前端扫码即可下官方音频）</span>
        </label>
      </div>
      <div v-if="genResult" class="gen-result">
        <div class="gr-head">
          <span class="muted">已生成卡密（每行一个）：</span>
          <a @click="copyCodes">复制</a>
        </div>
        <textarea readonly :value="genResult" rows="4"></textarea>
      </div>
    </div>

    <div class="card-panel pad">
      <div class="filter-row">
        <input v-model="keyword" placeholder="搜索卡密号" style="flex:1" @keyup.enter="page=1;load" />
        <select v-model="statusFilter" @change="page=1;load()">
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="used">used</option>
          <option value="disabled">disabled</option>
          <option value="expired">expired</option>
        </select>
        <button class="ghost" @click="page=1;load">查询</button>
      </div>

      <div v-if="loading" class="empty-state">加载中…</div>
      <table v-else class="tbl">
        <thead>
          <tr><th>卡密</th><th>状态</th><th>类型</th><th>到期/剩余</th><th>绑定接口</th><th>下载模式</th><th>网络数</th><th>备注</th><th>最近登录</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in cards" :key="c.code">
            <td class="mono">{{ c.code }}</td>
            <td><span class="tag" :class="c.status">{{ c.status }}</span></td>
            <td class="muted">{{ c.expiry_type === 'fixed' ? '固定' : '天数' }}</td>
            <td class="muted">
              <span v-if="c.expiry_type==='fixed'">{{ fmtDate(c.expires_at) }}</span>
              <span v-else>{{ c.remaining_text || '未激活' }}</span>
            </td>
            <td class="muted">{{ fmtBound(c) }}</td>
            <td class="muted">{{ fmtMode(c) }}</td>
            <td class="muted">{{ c.max_devices || 1 }} 个</td>
            <td class="muted">{{ c.note || '—' }}</td>
            <td class="muted">{{ fmtDate(c.last_login_at) }}</td>
            <td class="ops">
              <a @click="renewCard(c)">续期</a>
              <a @click="openBindEditor(c)">绑定</a>
              <a @click="openDeviceMgr(c)">网络</a>
              <a @click="toggleDisable(c)">{{ c.status==='disabled' ? '启用' : '禁用' }}</a>
              <a class="del" @click="delCard(c.id)">删除</a>
            </td>
          </tr>
          <tr v-if="!cards.length"><td colspan="10" class="empty-state">暂无卡密</td></tr>
        </tbody>
      </table>
      <div class="pager muted" v-if="total > pageSize">
        共 {{ total }} 张 · 第 {{ page }} 页
        <button class="ghost" :disabled="page<=1" @click="page--;load()">上一页</button>
        <button class="ghost" :disabled="page*pageSize>=total" @click="page++;load()">下一页</button>
      </div>
    </div>

    <!-- 绑定编辑弹窗 -->
    <div v-if="bindEditor.show" class="modal-mask" @click.self="bindEditor.show=false">
      <div class="modal card-panel">
        <h3 style="margin:0 0 6px">🧩 修改接口绑定</h3>
        <p class="muted" style="margin:0 0 14px;font-size:13px">
          卡密 <span class="mono">{{ bindEditor.card?.code }}</span><br/>
          勾选后该卡密<b>只能</b>使用勾选的接口；全部不勾选 = 不限制。
        </p>
        <label v-for="it in interfaces" :key="it.name" class="bind-item block">
          <input type="checkbox" :value="it.name" v-model="bindEditor.selected" />
          <span>{{ it.display_name }} <span class="muted mono" style="font-size:12px">({{ it.name }})</span></span>
        </label>
        <label class="bind-item block">
          下载模式
          <select v-model="bindEditor.download_mode">
            <option value="both">都允许</option>
            <option value="server">仅服务器下载</option>
            <option value="local">仅本地下载</option>
          </select>
        </label>
        <label class="bind-item block">
          允许绑定的网络数（1~10，同一网络不限设备）
          <input v-model.number="bindEditor.max_devices" type="number" min="1" max="10" step="1" style="width:80px" />
        </label>
        <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end">
          <button class="ghost" @click="bindEditor.show=false">取消</button>
          <button class="success" :disabled="bindEditor.saving" @click="saveBinding">{{ bindEditor.saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- 网络绑定管理弹窗 -->
    <div v-if="devMgr.show" class="modal-mask" @click.self="devMgr.show=false">
      <div class="modal card-panel" style="width:560px">
        <h3 style="margin:0 0 6px">🌐 网络绑定</h3>
        <p class="muted" style="margin:0 0 14px;font-size:13px">
          卡密 <span class="mono">{{ devMgr.card?.code }}</span> ·
          允许网络数 {{ devMgr.data?.max_devices ?? '—' }} 个 ·
          在线会话 {{ devMgr.data?.active_sessions ?? 0 }} 个 ·
          网络绑定：<b>{{ devMgr.data?.binding_enabled ? '已开启（同IP不限设备，换公网IP才顶号）' : '未开启（系统配置中可开启）' }}</b>
        </p>

        <div v-if="devMgr.loading" class="empty-state">加载中…</div>
        <template v-else>
          <table v-if="devMgr.data?.bindings?.length" class="tbl">
            <thead>
              <tr><th>网络</th><th>绑定时间</th><th>最后活跃</th><th>最近 IP</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="d in devMgr.data.bindings" :key="d.device_id">
                <td class="mono" :title="d.device_id">{{ d.label || d.device_id }}</td>
                <td class="muted">{{ fmtDate(d.bound_at) }}</td>
                <td class="muted">{{ fmtDate(d.last_active_at) }}</td>
                <td class="muted mono">{{ d.last_ip || '—' }}</td>
                <td><a class="del" :disabled="devMgr.busy" @click="unbindDevice(d.device_id)">解绑</a></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="muted" style="font-size:12px;padding:4px 0">（未绑定网络）</div>
        </template>

        <div style="display:flex;gap:10px;margin-top:14px;justify-content:flex-end">
          <button class="ghost" :disabled="devMgr.busy" @click="kickSessions">踢下线全部会话</button>
          <button class="ghost" :disabled="devMgr.busy" @click="unbindAllDevices">解绑全部网络</button>
          <button class="success" @click="devMgr.show=false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 18px 20px; margin-bottom: 16px; }
.gen-row { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; }
.gen-row label { font-size: 13px; color: var(--text-dim); display: flex; align-items: center; gap: 6px; }
.bind-row { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin-top: 12px; font-size: 13px; }
.bind-item { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.bind-item.block { display: flex; padding: 6px 0; }
.gen-result { margin-top: 14px; }
.gr-head { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 6px; }
.gr-head a { cursor: pointer; }
textarea { width: 100%; background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-family: monospace; resize: vertical; }
.filter-row { display: flex; gap: 10px; margin-bottom: 14px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--text-dim); font-weight: 600; }
.mono { font-family: monospace; letter-spacing: .5px; }
.ops { display: flex; gap: 10px; flex-wrap: wrap; }
.ops a { cursor: pointer; }
.ops .del { color: var(--danger); }
.pager { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
.pager button { padding: 5px 10px; font-size: 12px; }
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { width: 420px; max-width: 92vw; padding: 22px; }
</style>
