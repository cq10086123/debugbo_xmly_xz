<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useToast } from '../utils/toast'
import { bizApi, getSavedCardCode, setSavedCardCode } from '../utils/request'

const auth = useAuthStore()
const router = useRouter()
const toast = useToast()

const code = ref('')
const captchaId = ref('')
const captchaSvg = ref('')
const captchaInput = ref('')
const captchaInputEl = ref(null)   // 模板 ref，输完 4 位后把光标定位到这里
const loading = ref(false)

// 验证码最大长度（与服务端生成规则保持一致：4 位字母数字）
const CAPTCHA_MAX = 4

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

onMounted(async () => {
  // 1) 优先用上次成功登录缓存的卡密（被踢/重启浏览器后自动回填）
  const saved = getSavedCardCode()
  if (saved) code.value = saved
  // 2) 拉验证码
  await loadCaptcha()
  // 3) 有缓存的卡密时把光标直接定位到验证码框，少一次 Tab
  if (code.value) {
    await nextTick()
    captchaInputEl.value?.focus()
  }
})

async function submit() {
  // 防止双触发：loading 中 / 验证码已用过的 submit 直接丢弃
  // 注意：卡密/验证码 input 上的 @keyup.enter 已去掉，只留 form 的 @submit.prevent 一个触发点
  // （form 内 input 按回车浏览器会同步触发 form submit，回车仍可登录，但不会双触发）
  if (loading.value) return
  if (!captchaId.value) { toast.error('验证码已失效，请点击刷新'); return }
  const c = code.value.trim()
  if (!c) { toast.error('请输入卡密'); return }
  if (!captchaInput.value.trim()) { toast.error('请输入验证码'); return }
  loading.value = true
  try {
    const data = await auth.login(c, captchaId.value, captchaInput.value.trim())
    if (data.success) {
      // 成功后立即清掉本地的 captchaId——即使有意外重入，第二次 submit 也会因 captchaId 为空直接拦下
      captchaId.value = ''
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
    } else if (status === 404 || status === 403 || status === 401) {
      // 卡密不存在/被禁用/已过期：清掉缓存的卡密避免下次又自动填一个废号
      // 注意：被踢/网络抖动的 401 走的是拦截器那条路径（请求头带 token），不会到这儿；
      // 到这儿的 401/403/404 都是 /auth/login 直接拒绝——意味着这张卡废了
      setSavedCardCode('')
      code.value = ''
      toast.error(d?.detail || d?.error || '卡密无效')
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
        />

        <label class="lbl" style="margin-top:14px">验证码</label>
        <div class="captcha-row">
          <input
            ref="captchaInputEl"
            v-model="captchaInput"
            placeholder="请输入右侧字符"
            autocomplete="off"
            spellcheck="false"
            :maxlength="CAPTCHA_MAX"
          />
          <img
            v-if="captchaSvg"
            :src="captchaSvg"
            class="captcha-img"
            title="点击刷新验证码"
            @click="loadCaptcha"
          />
        </div>
        <p v-if="code" class="muted tip">已自动填入上次使用的卡密，输入验证码即可登录</p>

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
.tip { font-size: 12px; margin: 8px 0 0; }
</style>
