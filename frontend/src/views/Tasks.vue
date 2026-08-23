<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { bizApi, getBizToken } from '../utils/request'
import { connectSSE } from '../utils/sse'
import { useAuthStore } from '../stores/auth'
import TaskCard from '../components/TaskCard.vue'

const auth = useAuthStore()
const interfaces = ref([])
const tasksByInterface = ref({})
const loading = ref(true)
const notLoggedIn = ref(false)
let sseOff = []

function endpointFor(it) {
  if (it.type === 'official') return { list: '/download/batch', stream: '/api/download/batch/stream' }
  // 脚本接口：统一走 /api/intf/tasks 轮询（无 SSE）
  return { list: '/intf/tasks', stream: null }
}

let intfPollTimer = null
function clearIntfPoll() {
  if (intfPollTimer) { clearInterval(intfPollTimer); intfPollTimer = null }
}
async function pollIntfTasks() {
  try {
    const r = await bizApi.get('/intf/tasks')
    if (!r.data.success) return
    const all = r.data.tasks || []
    for (const it of interfaces.value.filter(x => x.type === 'script')) {
      const list = all.filter(t => (t.interface_name || t.interface) === it.name)
      tasksByInterface.value[it.name] = {
        tasks: list,
        active_count: list.filter(t => t.status === 'running').length,
      }
    }
  } catch {
    // 静默失败，避免弹窗打扰
  }
}

async function loadInterfaces() {
  try {
    const r = await bizApi.get('/interfaces')
    if (r.data.success) {
      interfaces.value = (r.data.interfaces || []).filter(it => it.enabled)
    }
  } catch {
    // 忽略：未登录等情况由 notLoggedIn 提示
  }
  notLoggedIn.value = !getBizToken()
}

async function loadOnce() {
  await loadInterfaces()
  const token = getBizToken()
  if (!token) { loading.value = false; return }

  for (const it of interfaces.value) {
    if (it.type !== 'official') continue // 脚本接口统一在 pollIntfTasks 中处理
    try {
      const ep = endpointFor(it)
      const r = await bizApi.get(ep.list)
      if (r.data.success) {
        tasksByInterface.value[it.name] = {
          tasks: r.data.tasks || [],
          active_count: r.data.active_count || 0,
        }
      }
    } catch {
      tasksByInterface.value[it.name] = { tasks: [], active_count: 0 }
    }
  }
  await pollIntfTasks()
  loading.value = false
}

function applySSE(name) {
  return (payload) => {
    tasksByInterface.value[name] = {
      tasks: payload.tasks || [],
      active_count: payload.active_count || 0,
    }
  }
}

function connectAll() {
  const token = getBizToken()
  if (!token) return
  clearIntfPoll()
  sseOff.forEach(f => f.close())
  sseOff = []
  for (const it of interfaces.value) {
    const ep = endpointFor(it)
    if (ep.stream) {
      sseOff.push(connectSSE(ep.stream, token, applySSE(it.name)))
    }
  }
  // 脚本接口无 SSE，走轮询
  if (interfaces.value.some(it => it.type === 'script')) {
    pollIntfTasks()
    intfPollTimer = setInterval(pollIntfTasks, 3000)
  }
}

onMounted(async () => {
  if (!auth.card) await auth.fetchMe()
  await loadOnce()
  connectAll()
})

onUnmounted(() => {
  sseOff.forEach((f) => f.close())
  sseOff = []
  clearIntfPoll()
})

const activeInterfaces = computed(() => {
  return interfaces.value.filter(it => (tasksByInterface.value[it.name]?.tasks || []).length > 0)
})
</script>

<template>
  <div>
    <div class="card-panel pad">
      <h3 style="margin:0 0 4px">📥 下载任务</h3>
      <p class="muted" style="margin:0">实时进度通过 SSE 推送，仅显示当前卡密的任务。没有任务的接口不会显示。</p>
    </div>

    <div v-if="loading" class="empty-state">加载中…</div>
    <div v-else-if="notLoggedIn" class="empty-state login-hint">
      🔒 请先<router-link to="/login">登录卡密</router-link>后查看下载任务。
    </div>
    <div v-else-if="!activeInterfaces.length" class="empty-state">暂无下载任务</div>

    <div v-else class="cols">
      <div v-for="it in activeInterfaces" :key="it.name">
        <h4 class="col-h">
          {{ it.display_name || it.name }}
          <span class="muted">({{ (tasksByInterface[it.name]?.tasks || []).length }})</span>
        </h4>
        <TaskCard
          v-for="t in tasksByInterface[it.name]?.tasks"
          :key="t.task_id"
          :task="t"
          @changed="loadOnce"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 18px 20px; margin-bottom: 18px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
.col-h { margin: 0 0 12px; font-size: 15px; }
.login-hint { font-size: 14px; }
.login-hint a { color: var(--primary); text-decoration: underline; }
</style>
