<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useToast } from '../utils/toast'
import { bizApi } from '../utils/request'

const auth = useAuthStore()
const router = useRouter()
const toast = useToast()

const code = ref('')
const captchaId = ref('')
const captchaSvg = ref('')
const captchaInput = ref('')
const loading = ref(false)

async function loadCaptcha() {
  try {
    const { data } = await bizApi.get('/auth/captcha')
    captchaId.value = data.captchaId
    captchaSvg.value = data.svg
    captchaInput.value = ''
  } catch (e) {
    // 验证码拉取失败不阻塞页面，提交时后端会拦截
  }
}

onMounted(loadCaptcha)

async function submit() {
  const c = code.value.trim()
  if (!c) { toast.error('请输入卡密'); return }
  if (!captchaInput.value.trim()) { toast.error('请输入验证码'); return }
  loading.value = true
  try {
    const data = await auth.login(c, captchaId.value, captchaInput.value.trim())
    if (data.success) {
      toast.success('登录成功')
      router.replace('/guide')
    } else {
      toast.error(data.detail || data.error || '登录失败')
      loadCaptcha()
    }
  } catch (e) {
    const d = e.response?.data
    const status = e.response?.status
    if (status === 423) {
      toast.error(d?.detail || '尝试次数过多，请稍后再试')
    } else {
      toast.error(d?.detail || d?.error || '网络错误')
    }
    loadCaptcha()
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="center-screen">
    <div class="login card-panel">
      <div class="hd">
        <div class="emoji">🎧</div>
        <h1>有声书下载器</h1>
        <p class="muted">输入卡密登录后开始下载</p>
      </div>
      <form @submit.prevent="submit">
        <label class="lbl">卡密</label>
        <input
          v-model="code"
          placeholder="例如 XM-XXXX-XXXX-XXXX"
          autocomplete="off"
          spellcheck="false"
          @keyup.enter="submit"
        />

        <label class="lbl" style="margin-top:14px">验证码</label>
        <div class="captcha-row">
          <input
            v-model="captchaInput"
            placeholder="请输入右侧字符"
            autocomplete="off"
            spellcheck="false"
            maxlength="4"
            @keyup.enter="submit"
          />
          <img
            v-if="captchaSvg"
            :src="captchaSvg"
            class="captcha-img"
            title="点击刷新验证码"
            @click="loadCaptcha"
          />
        </div>

        <button type="submit" :disabled="loading" style="margin-top:16px;width:100%">
          {{ loading ? '登录中…' : '登 录' }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login { width: 380px; max-width: 92vw; padding: 34px 30px; }
.hd { text-align: center; margin-bottom: 24px; }
.emoji { font-size: 42px; }
.hd h1 { margin: 8px 0 4px; font-size: 22px; }
.lbl { display: block; font-size: 13px; color: var(--text-dim); margin-bottom: 8px; }
form input { width: 100%; letter-spacing: 1px; }
.captcha-row { display: flex; gap: 10px; align-items: center; }
.captcha-row input { flex: 1; letter-spacing: 2px; }
.captcha-img { width: 120px; height: 44px; border-radius: 6px; cursor: pointer; border: 1px solid var(--border); }
</style>
