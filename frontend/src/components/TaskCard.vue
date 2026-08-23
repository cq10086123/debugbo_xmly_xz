<script setup>
import { ref, computed } from 'vue'
import { bizApi } from '../utils/request'
import { useToast } from '../utils/toast'

const props = defineProps({ task: Object })
const emit = defineEmits(['changed'])
const toast = useToast()
const showFailed = ref(false)

const interfaceName = computed(() => props.task.interface_name || 'official')
const isScript = computed(() => interfaceName.value !== 'official')

const baseUrl = computed(() => {
  const name = interfaceName.value
  if (name === 'official') return '/download/batch'
  // 脚本接口任务操作走统一任务端点
  return `/intf/tasks`
})

const engineLabel = computed(() => {
  const name = interfaceName.value
  if (name === 'official') return '官方'
  return name
})

async function act(action, okMsg) {
  try {
    const tid = props.task.task_id
    const url = `${baseUrl.value}/${tid}/${action}`
    const r = await bizApi.post(url)
    if (r.data.success) toast.success(okMsg)
    else toast.error(r.data.error || '操作失败')
  } catch (e) {
    toast.error(e.response?.data?.error || '操作失败')
  } finally {
    emit('changed')
  }
}

async function del() {
  try {
    const r = await bizApi.delete(`${baseUrl.value}/${props.task.task_id}`)
    if (r.data.success) toast.success('已删除')
    else toast.error(r.data.error || '删除失败')
  } catch (e) {
    toast.error(e.response?.data?.error || '删除失败')
  } finally {
    emit('changed')
  }
}

const statusText = {
  running: '下载中', done: '已完成', failed: '失败',
  cancelled: '已取消', interrupted: '中断(可恢复)',
}
</script>

<template>
  <div class="task card-panel">
    <div class="hd">
      <div class="info">
        <div class="title">{{ task.album_title || ('专辑 #' + (task.album_id || task.book_id)) }}</div>
        <div class="sub muted">
          接口：{{ engineLabel }}
          <span v-if="task.account_nickname"> · 账号：{{ task.account_nickname }}</span>
        </div>
      </div>
      <span class="tag" :class="task.status">{{ statusText[task.status] || task.status }}</span>
    </div>

    <div class="progress"><span :style="{ width: task.percent + '%' }"></span></div>
    <div class="stat muted">
      <span>{{ task.completed }} / {{ task.total }} 完成</span>
      <span v-if="task.skipped_count">· 跳过 {{ task.skipped_count }}</span>
      <span v-if="task.failed_count">· 失败 {{ task.failed_count }}</span>
      <span v-if="task.percent">· {{ task.percent }}%</span>
      <span v-if="task.eta_text">· 预计 {{ task.eta_text }}</span>
    </div>

    <div class="current muted" v-if="task.current_title && task.status==='running'">
      ▶ {{ task.current_title }}
    </div>
    <div class="warn-banner" v-if="task.last_error && task.status==='running'">
      ⚠ {{ task.last_error }}
    </div>
    <div class="error" v-if="task.error">⚠ {{ task.error }}</div>

    <div class="failed" v-if="task.failed_list && task.failed_list.length">
      <div class="failed-hd" @click="showFailed = !showFailed">
        {{ showFailed ? '▾' : '▸' }} 失败明细（{{ task.failed_list.length }} 集，点击{{ showFailed ? '收起' : '展开' }}）
      </div>
      <ul v-if="showFailed" class="failed-list">
        <li v-for="f in task.failed_list" :key="f.episode">
          <b>第{{ f.episode }}集</b> {{ f.title }}
          <span class="fe">— {{ f.error }}</span>
          <span class="muted" v-if="f.auto_retried">（自动重试 {{ f.auto_retried }} 次仍失败）</span>
        </li>
      </ul>
    </div>

    <div class="ops">
      <button v-if="task.status==='running' || task.status==='interrupted'" class="warn" @click="act('cancel','已取消')">取消</button>
      <button v-if="!isScript && (task.status==='interrupted' || task.status==='failed')" class="success" @click="act('resume','已恢复')">恢复</button>
      <button v-if="!isScript && task.failed_count" class="ghost" @click="act('retry','已重试失败集')">重试失败集</button>
      <button v-if="task.status!=='running'" class="ghost danger" @click="del">删除</button>
    </div>
  </div>
</template>

<style scoped>
.task { padding: 16px; margin-bottom: 14px; }
.hd { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.title { font-weight: 700; font-size: 15px; }
.sub { font-size: 12px; margin-top: 4px; }
.stat { display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; margin: 8px 0; }
.current { font-size: 13px; margin: 4px 0; }
.warn-banner {
  background: #fff4e5; border: 1px solid #ffb74d; color: #b26a00;
  font-size: 12.5px; line-height: 1.5; padding: 8px 10px; border-radius: 8px; margin: 6px 0;
}
.error { color: var(--danger); font-size: 12px; margin: 6px 0; }
.failed { margin: 8px 0; font-size: 12px; }
.failed-hd {
  cursor: pointer; color: var(--primary); font-weight: 600;
  padding: 4px 0; user-select: none;
}
.failed-list { margin: 4px 0 0; padding-left: 16px; }
.failed-list li { margin: 3px 0; line-height: 1.5; }
.failed-list .fe { color: var(--danger); }
.ops { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.ops button { padding: 7px 13px; font-size: 13px; }
</style>
