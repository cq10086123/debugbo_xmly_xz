<script setup>
import { ref, onUnmounted } from 'vue'
import { bizApi } from '../utils/request'
import { useToast } from '../utils/toast'

const toast = useToast()

const accounts = ref([])
const loading = ref(false)

// 扫码登录状态
const showQr = ref(false)
const qrImg = ref('')
const qrId = ref('')
const scanning = ref(false)
const scanMsg = ref('')
let pollTimer = null

async function loadAccounts() {
  loading.value = true
  try {
    const r = await bizApi.get('/accounts')
    if (r.data.success) accounts.value = r.data.accounts || []
  } catch (e) {
    toast.error('加载账号失败：' + (e.response?.data?.error || e.message))
  } finally {
    loading.value = false
  }
}

function _normalizeImg(img) {
  if (!img) return ''
  return img.startsWith('data:') ? img : 'data:image/png;base64,' + img
}

function maskText(val) {
  const s = String(val || '')
  if (!s) return '-'
  if (s.length <= 2) return '***'
  return s.slice(0, 1) + '***' + s.slice(-1)
}

async function openQr() {
  stopPoll()
  try {
    const r = await bizApi.get('/accounts/qrcode')
    if (!r.data.success) {
      toast.error('获取二维码失败：' + (r.data.error || '未知错误'))
      return
    }
    qrId.value = r.data.qr_id
    qrImg.value = _normalizeImg(r.data.img)
    showQr.value = true
    scanMsg.value = '请使用喜马拉雅 App 扫码登录'
    scanning.value = true
    startPoll()
  } catch (e) {
    toast.error('获取二维码失败：' + (e.response?.data?.error || e.message))
  }
}

function startPoll() {
  stopPoll()
  let elapsed = 0
  pollTimer = setInterval(async () => {
    try {
      const r = await bizApi.get('/accounts/status/poll', { params: { qr_id: qrId.value } })
      const d = r.data
      if (d.success) {
        scanMsg.value = `登录成功：${maskText(d.nickname || d.mobile || d.uid)}`
        scanning.value = false
        stopPoll()
        setTimeout(() => { showQr.value = false }, 1200)
        toast.success('账号已添加：' + maskText(d.nickname || d.mobile || d.uid))
        loadAccounts()
      } else if (d.status === 'scanned') {
        scanMsg.value = '已扫码，请在手机上确认登录…'
      } else {
        scanMsg.value = '等待扫码…'
      }
    } catch (e) {
      scanMsg.value = '轮询异常，请重试'
      scanning.value = false
      stopPoll()
    }
    elapsed += 2
    if (elapsed >= 180) {
      scanMsg.value = '二维码已过期，请重新获取'
      scanning.value = false
      stopPoll()
    }
  }, 2000)
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function closeQr() {
  stopPoll()
  showQr.value = false
  scanning.value = false
}

async function verifyAccount(id) {
  try {
    const r = await bizApi.post(`/accounts/${id}/verify`)
    const d = r.data
    if (d.is_valid) toast.success(`账号有效${d.is_vip ? '（VIP）' : ''}：${maskText(d.nickname || id)}`)
    else toast.error('Cookie 已失效，请删除后重新扫码登录')
    loadAccounts()
  } catch (e) {
    toast.error('校验失败：' + (e.response?.data?.error || e.message))
  }
}

async function removeAccount(id) {
  if (!confirm('确定删除该账号？')) return
  try {
    const r = await bizApi.delete(`/accounts/${id}`)
    if (r.data.success) {
      toast.success('账号已删除')
      loadAccounts()
    } else {
      toast.error(r.data.message || '删除失败')
    }
  } catch (e) {
    toast.error('删除失败：' + (e.response?.data?.error || e.message))
  }
}

loadAccounts()
onUnmounted(stopPoll)
</script>

<template>
  <div>
    <div class="card-panel pad">
      <div class="hd">
        <div>
          <h3 style="margin:0 0 4px">👤 我的喜马拉雅账号</h3>
          <p class="muted" style="margin:0">
            本账号仅属于当前卡密，官方引擎下载需先登录至少一个账号；
            <b>卡密过期后登录数据将自动销毁</b>。
          </p>
        </div>
        <button @click="openQr" :disabled="scanning">＋ 扫码登录添加</button>
      </div>

      <div v-if="loading" class="empty-state">加载中…</div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>昵称</th><th>UID</th><th>手机</th><th>VIP</th><th>添加时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in accounts" :key="a.id">
            <td>{{ maskText(a.nickname) }}</td>
            <td class="muted">{{ maskText(a.uid) }}</td>
            <td class="muted">{{ maskText(a.mobile) }}</td>
            <td>
              <span v-if="a.is_vip" class="badge vip">VIP</span>
              <span v-else class="badge">普通</span>
              <span v-if="a.cooling" class="badge cooling">🕒 冷却中</span>
            </td>
            <td class="muted">{{ a.added_at || '-' }}</td>
            <td class="ops">
              <button class="mini" @click="verifyAccount(a.id)">校验</button>
              <button class="mini danger" @click="removeAccount(a.id)">删除</button>
            </td>
          </tr>
          <tr v-if="!accounts.length"><td colspan="6" class="empty-state">暂无账号，请扫码登录添加后才能使用官方下载</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 扫码弹窗 -->
    <div v-if="showQr" class="modal-mask" @click.self="closeQr">
      <div class="modal">
        <h3 style="margin:0 0 10px">扫描二维码登录</h3>
        <div class="qr-wrap">
          <img v-if="qrImg" :src="qrImg" alt="qr" class="qr" />
        </div>
        <p class="muted center" :class="{ ok: !scanning }">{{ scanMsg }}</p>
        <button class="ghost full" @click="closeQr">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 20px; }
.hd { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 14px; }
.tbl th, .tbl td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--text-dim); font-weight: 600; font-size: 13px; }
.ops { display: flex; gap: 8px; }
.mini { padding: 4px 10px; font-size: 12px; }
.mini.danger { background: rgba(255,90,90,.16); color: #ff7a7a; border: 1px solid rgba(255,90,90,.3); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: var(--bg-soft); color: var(--text-dim); }
.badge.vip { background: linear-gradient(135deg, #ffcf6b, #ff9d3c); color: #2a1c00; font-weight: 700; }
.badge.cooling { background: rgba(255,154,60,.18); color: #ff9d3c; border: 1px solid rgba(255,154,60,.4); font-weight: 600; }

.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 24px; width: 320px; max-width: 92vw; text-align: center; }
.qr-wrap { display: flex; justify-content: center; margin: 10px 0 14px; }
.qr { width: 220px; height: 220px; background: #fff; border-radius: 10px; object-fit: contain; }
.center { text-align: center; }
.ok { color: #57e3a0; }
.full { width: 100%; margin-top: 6px; }
</style>
