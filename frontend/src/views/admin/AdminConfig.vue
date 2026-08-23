<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { adminApi, getAdminPath } from '../../utils/request'
import { useToast } from '../../utils/toast'
import { useAuthStore } from '../../stores/auth'

const toast = useToast()
const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const saving = ref(false)

// 配置项定义（展示名 + key + 类型）
// 注：第三方接口密钥已迁移至「接口管理」中各接口自身的配置，此处仅保留全局任务策略。
const fields = [
  { key: 'auto_retry_enabled', label: '失败自动重试', type: 'bool' },
  { key: 'auto_retry_interval_minutes', label: '重试间隔(分钟)', type: 'number' },
  { key: 'auto_retry_max_rounds', label: '最大重试轮数', type: 'number' },
  { key: 'admin_path', label: '后台管理地址', type: 'text' },
  { key: 'admin_lan_only', label: '局域网访问限制', type: 'bool' },
]

const cfg = reactive({})

async function load() {
  loading.value = true
  try {
    const r = await adminApi.get('/config')
    if (r.data.success) Object.assign(cfg, r.data.config || {})
  } catch (e) {}
  loading.value = false
}

async function save() {
  saving.value = true
  try {
    const toSave = {}
    for (const f of fields) toSave[f.key] = cfg[f.key]
    const r = await adminApi.put('/config', { config: toSave })
    if (r.data.success) {
      Object.assign(cfg, r.data.config || {})
      const newPath = r.data.config?.admin_path
      if (newPath && newPath !== getAdminPath()) {
        toast.success('配置已保存。后台地址改为「' + newPath + '」，重启服务后生效，新入口：/#/' + newPath)
      } else {
        toast.success('配置已保存并立即生效')
      }
    } else toast.error(r.data.error || '保存失败')
  } catch (e) { toast.error(e.response?.data?.detail || '保存失败') }
  finally { saving.value = false }
}

function modelValue(f) {
  if (f.type === 'bool') return cfg[f.key] === '1' || cfg[f.key] === true || cfg[f.key] === 'true'
  return cfg[f.key]
}

// ── 管理员账号安全 ──
const cred = reactive({ current_password: '', new_username: '', new_password: '', confirm: '' })
const credSaving = ref(false)

async function saveCred() {
  if (!cred.current_password) return toast.error('请输入当前密码')
  if (!cred.new_password) return toast.error('请输入新密码')
  if (cred.new_password.length < 8) return toast.error('新密码至少 8 位')
  if (cred.new_password !== cred.confirm) return toast.error('两次输入的新密码不一致')
  credSaving.value = true
  try {
    const r = await adminApi.post('/account', {
      current_password: cred.current_password,
      new_username: cred.new_username.trim() || null,
      new_password: cred.new_password,
    })
    if (r.data.success) {
      toast.success('管理员账号已更新，所有会话已失效，请用新凭据重新登录')
      auth.adminLogout()
      router.push(`/${getAdminPath()}/login`)
    } else toast.error(r.data.error || '修改失败')
  } catch (e) { toast.error(e.response?.data?.detail || '修改失败') }
  finally { credSaving.value = false }
}

onMounted(load)
</script>

<template>
  <div class="card-panel pad">
    <h3 style="margin:0 0 6px">🔌 接口与密钥配置</h3>
    <p class="muted" style="margin:0 0 16px">修改后立即生效，无需重启。敏感密钥请妥善保管。</p>

    <div v-if="loading" class="empty-state">加载中…</div>
    <div v-else class="form">
      <div v-for="f in fields" :key="f.key" class="row">
        <label>{{ f.label }}</label>
        <input v-if="f.type==='text'" v-model="cfg[f.key]" :placeholder="f.key" />
        <input v-else-if="f.type==='number'" v-model="cfg[f.key]" type="number" style="max-width:160px" />
        <label v-else-if="f.type==='bool'" class="switch">
          <input type="checkbox" :checked="modelValue(f)" @change="cfg[f.key] = $event.target.checked ? '1' : '0'" />
          <span>{{ modelValue(f) ? '开启' : '关闭' }}</span>
        </label>
      </div>
      <p class="muted hint">
        「后台管理地址」即管理后台的 URL 段（当前为 <b>{{ cfg.admin_path || 'admin' }}</b>，入口 /#/{{ cfg.admin_path || 'admin' }}）。
        小写字母开头，仅含小写字母/数字/-/_，修改后<b>需重启服务</b>才生效。
        管理后台 API 与文档页受「局域网访问限制」开关控制：开启时仅允许局域网 IP 访问，关闭后公网也可访问（请确保已修改强密码，并尽量走 HTTPS）。
      </p>
      <button class="success" :disabled="saving" @click="save">{{ saving ? '保存中…' : '💾 保存配置' }}</button>
    </div>
  </div>

  <div class="card-panel pad" style="margin-top:16px">
    <h3 style="margin:0 0 6px">🔑 管理员账号安全</h3>
    <p class="muted" style="margin:0 0 16px">
      修改登录后台的用户名 / 密码。初始账号为 admin / admin123，<b>强烈建议立即修改</b>。
      修改成功后所有登录会话失效，需用新凭据重新登录。
    </p>
    <div class="form">
      <div class="row">
        <label>当前密码</label>
        <input v-model="cred.current_password" type="password" autocomplete="current-password" placeholder="验证身份用" />
      </div>
      <div class="row">
        <label>新用户名</label>
        <input v-model="cred.new_username" type="text" placeholder="留空 = 保持不变（2~32 位字母/数字/_/-）" />
      </div>
      <div class="row">
        <label>新密码</label>
        <input v-model="cred.new_password" type="password" autocomplete="new-password" placeholder="至少 8 位" />
      </div>
      <div class="row">
        <label>确认新密码</label>
        <input v-model="cred.confirm" type="password" autocomplete="new-password" placeholder="再输入一次" />
      </div>
      <button class="success" :disabled="credSaving" @click="saveCred">{{ credSaving ? '提交中…' : '🔑 修改账号凭据' }}</button>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 20px; }
.form { display: flex; flex-direction: column; gap: 14px; max-width: 620px; }
.row { display: flex; align-items: center; gap: 14px; }
.row label:first-child { width: 130px; color: var(--text-dim); font-size: 13px; flex-shrink: 0; }
.row input[type=text], .row input[type=number] { flex: 1; }
.switch { display: flex; align-items: center; gap: 8px; }
.switch input { width: auto; }
.hint { font-size: 12px; line-height: 1.7; margin: 0; }
</style>
