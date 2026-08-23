<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useToast } from '../../utils/toast'
import { getAdminPath, probeAdminPath } from '../../utils/request'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const toast = useToast()

const username = ref('')
const password = ref('')
const loading = ref(false)
// 地址段有效性：true 有效 / false 无效（显示提示、禁用表单）/ null 检测中或后端不可达（不阻断）
const pathValid = ref(null)

onMounted(async () => {
  const seg = route.params.adminPath
  if (!seg) { pathValid.value = true; return }
  const ok = await probeAdminPath(seg)
  pathValid.value = ok !== false
})

async function submit() {
  if (pathValid.value === false) { toast.error('后台地址无效，请先修正 URL'); return }
  if (!username.value || !password.value) { toast.error('请输入账号和密码'); return }
  loading.value = true
  try {
    const data = await auth.adminLogin(username.value, password.value)
    if (data.success) {
      toast.success('管理员登录成功')
      const p = route.params.adminPath || getAdminPath()
      router.replace(`/${p}/cards`)
    } else {
      toast.error(data.detail || '登录失败')
    }
  } catch (e) {
    const d = e.response?.data
    toast.error(d?.detail || '网络错误')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="center-screen">
    <div class="login card-panel">
      <div class="hd">
        <div class="emoji">⚙️</div>
        <h1>管理后台</h1>
        <p class="muted">管理员账号登录</p>
      </div>
      <div v-if="pathValid === false" class="invalid-box">
        <p><b>⚠️ 后台地址无效</b></p>
        <p>
          <code>/#/{{ route.params.adminPath }}</code> 不是本服务器的管理后台地址。
          后台地址段是部署时自定义的（并非默认的 admin），请核对 URL。
        </p>
        <p class="muted">
          忘记地址？查看后端启动日志中的「管理后台 API 挂载于 /api/xxx」，
          或查服务器 app.db 的 api_config 表 admin_path 项。
        </p>
      </div>
      <form v-else @submit.prevent="submit">
        <label class="lbl">账号</label>
        <input v-model="username" placeholder="admin" autocomplete="off" />
        <label class="lbl" style="margin-top:14px">密码</label>
        <input v-model="password" type="password" placeholder="••••••" @keyup.enter="submit" />
        <button type="submit" :disabled="loading" style="margin-top:18px;width:100%">
          {{ loading ? '登录中…' : '登 录' }}
        </button>
      </form>
      <div class="foot muted"><router-link to="/login">← 返回卡密登录</router-link></div>
    </div>
  </div>
</template>

<style scoped>
.login { width: 360px; max-width: 92vw; padding: 32px 28px; }
.hd { text-align: center; margin-bottom: 22px; }
.emoji { font-size: 38px; }
.hd h1 { margin: 8px 0 4px; font-size: 20px; }
.lbl { display: block; font-size: 13px; color: var(--text-dim); margin-bottom: 8px; }
form input { width: 100%; }
.foot { text-align: center; margin-top: 18px; font-size: 13px; }
.invalid-box { font-size: 13px; line-height: 1.7; }
.invalid-box code { background: var(--bg-soft, #f3f4f6); padding: 1px 6px; border-radius: 4px; }
.invalid-box p { margin: 0 0 10px; }
</style>
