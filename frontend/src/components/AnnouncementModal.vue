<script setup>
// 公告弹窗：全局挂载于 App.vue。
// 仅业务端（卡密登录态）拉取 /api/announcements/current；管理后台/登录页不弹。
// 已读状态存 localStorage（announcement_read_ids），同一条公告读过后不再弹。
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { bizApi } from '../utils/request'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const auth = useAuthStore()

const READ_KEY = 'announcement_read_ids'

const visible = ref(false)
const ann = ref(null)
let fetching = false // 防并发重复请求（登录时 token 与路由 watcher 可能同时触发）

function getReadIds() {
  try {
    const v = JSON.parse(localStorage.getItem(READ_KEY) || '[]')
    return Array.isArray(v) ? v : []
  } catch (e) { return [] }
}

function markRead(id) {
  const ids = getReadIds()
  if (!ids.includes(id)) ids.push(id)
  // 只保留最近 100 条，避免无限增长
  localStorage.setItem(READ_KEY, JSON.stringify(ids.slice(-100)))
}

// 是否处于业务端可弹窗的上下文：已登录 + 非管理后台/登录页
const canShow = computed(() => {
  if (!auth.isLoggedIn) return false
  const isAdmin = route.matched.some((r) => r.meta.admin || r.meta.adminLogin)
  if (isAdmin) return false
  return route.path !== '/login'
})

async function check() {
  if (!canShow.value || fetching || visible.value) return
  fetching = true
  try {
    const r = await bizApi.get('/announcements/current')
    const a = r.data && r.data.announcement
    // 仅当存在启用公告且本地未读时才弹；已读/无公告均不打扰
    if (a && !getReadIds().includes(a.id)) {
      ann.value = a
      visible.value = true
    }
  } catch (e) { /* 拉取失败静默忽略，不影响业务 */ }
  finally { fetching = false }
}

function close() {
  if (ann.value) markRead(ann.value.id)
  visible.value = false
}

// 触发点①：登录态变化 / 路由切换（含业务页之间跳转，canShow 恒 true 故须另 watch fullPath）
watch([() => auth.bizToken, () => route.fullPath], () => { check() })
onMounted(check)

// 触发点②：定时轮询（每 60s），停留单页不动也能收到新公告；组件卸载时清理
const POLL_MS = 60 * 1000
const pollTimer = setInterval(check, POLL_MS)
onUnmounted(() => clearInterval(pollTimer))
</script>

<template>
  <div v-if="visible && ann" class="ann-mask" @click.self="close">
    <div class="ann-modal card-panel">
      <div class="ann-head">
        <span class="ann-badge">📢 公告</span>
        <button class="ann-close" @click="close" title="关闭">✕</button>
      </div>
      <h3 class="ann-title">{{ ann.title }}</h3>
      <div class="ann-content">{{ ann.content }}</div>
      <div class="ann-foot">
        <span class="muted" v-if="ann.created_at">{{ new Date(ann.created_at).toLocaleString('zh-CN', { hour12: false }) }}</span>
        <button class="success" @click="close">知道了</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ann-mask {
  position: fixed; inset: 0; background: rgba(0, 0, 0, .55);
  display: flex; align-items: center; justify-content: center; z-index: 200;
}
.ann-modal { width: 460px; max-width: 92vw; max-height: 80vh; overflow-y: auto; padding: 20px 22px; }
.ann-head { display: flex; align-items: center; justify-content: space-between; }
.ann-badge {
  font-size: 12px; font-weight: 700; color: var(--primary);
  background: rgba(91, 140, 255, .14); border: 1px solid rgba(91, 140, 255, .4);
  padding: 3px 10px; border-radius: 999px;
}
.ann-close {
  background: transparent; color: var(--text-dim); border: none; box-shadow: none;
  padding: 4px 8px; font-size: 15px; line-height: 1;
}
.ann-close:hover { color: var(--text); }
.ann-title { margin: 12px 0 10px; font-size: 17px; }
.ann-content {
  white-space: pre-wrap; word-break: break-word; line-height: 1.75;
  color: var(--text); font-size: 14px;
}
.ann-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 18px; }
.ann-foot .muted { font-size: 12px; }
</style>
