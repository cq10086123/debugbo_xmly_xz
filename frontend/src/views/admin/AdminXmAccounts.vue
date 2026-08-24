<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { adminApi } from '../../utils/request'
import { useToast } from '../../utils/toast'

const toast = useToast()
const backendAccounts = ref([])
const loading = ref(false)
const verifyingAll = ref(false)
const verifyingId = ref(null)

// 后端扫码登录二维码弹窗
const qrModal = reactive({ show: false, img: '', qrId: '', checking: false })
let pollTimer = null

// 注入到卡密表单
const injectForm = reactive({ cardIdent: '', injectAll: true, selected: [], result: '' })

function _normalizeImg(img) {
  if (!img) return ''
  return img.startsWith('data:') ? img : 'data:image/png;base64,' + img
}

async function loadBackendAccounts() {
  loading.value = true
  try {
    const r = await adminApi.get('/xm-login/accounts')
    if (r.data.success) backendAccounts.value = r.data.accounts
  } catch (e) {}
  loading.value = false
}

async function openQr() {
  try {
    const r = await adminApi.post('/xm-login/qr')
    if (r.data.success) {
      qrModal.img = _normalizeImg(r.data.img)
      qrModal.qrId = r.data.qr_id
      qrModal.show = true
      qrModal.checking = false
      startPoll()
    } else toast.error(r.data.error || '生成二维码失败')
  } catch (e) { toast.error(e.response?.data?.error || '生成二维码失败') }
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    try {
      const r = await adminApi.get('/xm-login/qr/check', { params: { qr_id: qrModal.qrId } })
      const d = r.data
      if (d.success) {
        stopPoll()
        qrModal.show = false
        toast.success(`已登录后端账号 ${d.nickname || d.uid}`)
        loadBackendAccounts()
      } else if (d.status === 'scanned') {
        qrModal.checking = true
      }
    } catch (e) { stopPoll() }
  }, 2000)
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function closeQr() { qrModal.show = false; stopPoll() }

async function delBackend(id) {
  if (!confirm('确认从供体池删除该后端账号？')) return
  try {
    const r = await adminApi.delete(`/xm-login/accounts/${id}`)
    if (r.data.success) { toast.success('已删除'); loadBackendAccounts() }
    else toast.error(r.data.error || '删除失败')
  } catch (e) { toast.error('删除失败') }
}

async function inject() {
  const ident = injectForm.cardIdent.trim()
  if (!ident) { toast.error('请输入卡密 ID 或卡密号'); return }
  injectForm.result = ''
  try {
    const payload = {}
    if (!injectForm.injectAll) {
      if (!injectForm.selected.length) { toast.error('请至少选择一个供体账号'); return }
      payload.backend_ids = [...injectForm.selected]
    }
    const r = await adminApi.post(`/cards/${encodeURIComponent(ident)}/inject-cookie`, payload)
    if (r.data.success) {
      injectForm.result = JSON.stringify(r.data.result, null, 2)
      toast.success('注入完成')
    } else toast.error(r.data.error || '注入失败')
  } catch (e) { toast.error(e.response?.data?.detail || '注入失败') }
}

async function revoke() {
  const ident = injectForm.cardIdent.trim()
  if (!ident) { toast.error('请输入卡密 ID 或卡密号'); return }
  if (!confirm('确认撤销该卡密下所有后端注入的账号？')) return
  try {
    const r = await adminApi.post(`/cards/${encodeURIComponent(ident)}/revoke-cookie`)
    if (r.data.success) {
      toast.success(`已撤销 ${r.data.revoked} 个注入账号`)
      injectForm.result = JSON.stringify(r.data, null, 2)
    } else toast.error(r.data.error || '撤销失败')
  } catch (e) { toast.error(e.response?.data?.detail || '撤销失败') }
}

async function verifyOne(id) {
  verifyingId.value = id
  try {
    const r = await adminApi.post(`/xm-login/accounts/${id}/verify`)
    if (r.data.success) {
      if (r.data.is_valid) {
        toast.success(`${r.data.nickname || r.data.id} Cookie 有效 (VIP: ${r.data.is_vip ? '是' : '否'})`)
      } else {
        toast.error(`${r.data.id} ${r.data.error || 'Cookie 已失效'}`)
      }
      loadBackendAccounts()
    } else toast.error(r.data.error || '验证失败')
  } catch (e) { toast.error('验证失败') }
  verifyingId.value = null
}

async function verifyAll() {
  if (verifyingAll.value) return
  verifyingAll.value = true
  try {
    const r = await adminApi.post('/xm-login/accounts/verify-all')
    if (r.data.success) {
      toast.success(`验证完成: ${r.data.valid} 有效 / ${r.data.invalid} 失效 (共 ${r.data.total})`)
      loadBackendAccounts()
    } else toast.error('验证失败')
  } catch (e) { toast.error('验证失败') }
  verifyingAll.value = false
}

function statusLabel(acc) {
  if (acc.is_valid === null || acc.is_valid === undefined) return '未验证'
  return acc.is_valid ? '有效' : '失效'
}

function statusClass(acc) {
  if (acc.is_valid === null || acc.is_valid === undefined) return 'pending'
  return acc.is_valid ? 'active' : 'disabled'
}

onMounted(() => { loadBackendAccounts() })
onUnmounted(() => { stopPoll() })
</script>

<template>
  <div>
    <!-- 后端供体账号池 -->
    <div class="card-panel pad">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap">
        <h3 style="margin:0">🍪 后端喜马拉雅供体账号池</h3>
        <button class="success" @click="openQr">+ 扫码登录后端账号</button>
        <button class="ghost" :disabled="verifyingAll || !backendAccounts.length" @click="verifyAll">
          {{ verifyingAll ? '验证中…' : '🔍 批量验证全部' }}
        </button>
      </div>
      <p class="muted" style="margin:0 0 14px;font-size:13px">
        在后台扫码登录的喜马拉雅账号暂存于此，仅作「供体」被复制进卡密，绝不直接用于下载。
        供体 cookie 更新时会自动级联刷新已注入的副本。
      </p>
      <div v-if="loading" class="empty-state">加载中…</div>
      <table v-else-if="backendAccounts.length" class="tbl">
        <thead>
          <tr><th>ID</th><th>昵称</th><th>UID</th><th>VIP</th><th>手机号</th><th>状态</th><th>最后验证</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in backendAccounts" :key="b.id">
            <td class="mono">{{ b.id }}</td>
            <td>{{ b.nickname || '—' }}</td>
            <td class="mono">{{ b.uid || '—' }}</td>
            <td><span class="tag" :class="b.is_vip ? 'used' : 'disabled'">{{ b.is_vip ? 'VIP' : '普通' }}</span></td>
            <td class="mono">{{ b.mobile || '—' }}</td>
            <td><span class="tag" :class="statusClass(b)">{{ statusLabel(b) }}</span></td>
            <td class="muted" style="font-size:12px">{{ b.last_verified_at || '—' }}</td>
            <td class="ops">
              <a class="verify" @click="verifyOne(b.id)" :class="{ 'verifying': verifyingId === b.id }">
                {{ verifyingId === b.id ? '验证中' : '验证' }}
              </a>
              <a class="del" @click="delBackend(b.id)">删除</a>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">供体池为空，请先扫码登录后端账号</div>
    </div>

    <!-- 注入到卡密 -->
    <div class="card-panel pad">
      <h3 style="margin:0 0 14px">🔑 将供体账号注入卡密</h3>
      <div class="gen-row">
        <label>卡密 ID 或卡密号
          <input v-model="injectForm.cardIdent" placeholder="如 12 或 XM-XXXX-XXXX" style="width:220px" />
        </label>
        <label class="bind-item">
          <input type="checkbox" v-model="injectForm.injectAll" />
          <span>注入全部供体账号</span>
        </label>
        <button class="success" @click="inject">注入</button>
        <button class="ghost" @click="revoke">撤销该卡注入</button>
      </div>
      <div v-if="!injectForm.injectAll" class="bind-row">
        <span class="muted">选择要注入的供体：</span>
        <label v-for="b in backendAccounts" :key="b.id" class="bind-item">
          <input type="checkbox" :value="b.id" v-model="injectForm.selected" />
          <span>{{ b.nickname || b.uid }} <span class="muted mono" style="font-size:12px">({{ b.id }})</span></span>
        </label>
        <span v-if="!backendAccounts.length" class="muted">供体池为空</span>
      </div>
      <pre v-if="injectForm.result" class="result">{{ injectForm.result }}</pre>
    </div>

    <!-- 扫码二维码弹窗 -->
    <div v-if="qrModal.show" class="modal-mask" @click.self="closeQr">
      <div class="modal card-panel" style="text-align:center">
        <h3 style="margin:0 0 6px">扫码登录后端喜马拉雅账号</h3>
        <p class="muted" style="margin:0 0 12px;font-size:13px">
          用手机喜马拉雅扫码；登录成功后该账号进入供体池，可复制给任意卡密。
        </p>
        <div class="qr-wrap">
          <img v-if="qrModal.img" :src="qrModal.img" alt="qr" class="qr" />
        </div>
        <div class="muted" style="font-size:13px">
          {{ qrModal.checking ? '已扫码，请在手机上确认…' : '等待扫码…' }}
        </div>
        <div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">
          <button class="ghost" @click="closeQr">关闭</button>
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
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--text-dim); font-weight: 600; }
.mono { font-family: monospace; letter-spacing: .5px; }
.ops { display: flex; gap: 10px; flex-wrap: wrap; }
.ops a { cursor: pointer; }
.ops .verify { color: var(--accent, #3b82f6); }
.ops .verify.verifying { color: var(--text-dim); pointer-events: none; }
.ops .del { color: var(--danger); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 500; }
.tag.active { background: #10b981; color: #fff; }
.tag.pending { background: #f59e0b; color: #fff; }
.tag.disabled { background: #ef4444; color: #fff; }
.tag.used { background: #8b5cf6; color: #fff; }
.result { text-align: left; background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 12px; margin-top: 14px; overflow: auto; max-height: 280px; }
.qr-wrap { display: flex; justify-content: center; margin: 10px 0 14px; }
.qr { width: 220px; height: 220px; background: #fff; border-radius: 10px; object-fit: contain; }
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { width: 360px; max-width: 92vw; padding: 22px; }
button:disabled { opacity: .5; cursor: not-allowed; }
</style>
