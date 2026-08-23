<script setup>
import { ref, reactive, onMounted } from 'vue'
import { adminApi } from '../../utils/request'
import { useToast } from '../../utils/toast'

const toast = useToast()
const list = ref([])
const loading = ref(false)

// 新建 / 编辑 弹窗（共用）
const editor = reactive({ show: false, id: null, title: '', content: '', active: true, saving: false })

async function load() {
  loading.value = true
  try {
    const r = await adminApi.get('/announcements')
    if (r.data.success) list.value = r.data.announcements
  } catch (e) {}
  loading.value = false
}

function openCreate() {
  editor.id = null
  editor.title = ''
  editor.content = ''
  editor.active = true
  editor.show = true
}

function openEdit(a) {
  editor.id = a.id
  editor.title = a.title
  editor.content = a.content
  editor.active = a.active
  editor.show = true
}

async function save() {
  if (!editor.title.trim()) { toast.error('请填写标题'); return }
  if (!editor.content.trim()) { toast.error('请填写内容'); return }
  editor.saving = true
  const payload = { title: editor.title.trim(), content: editor.content.trim(), active: editor.active }
  try {
    const r = editor.id
      ? await adminApi.put(`/announcements/${editor.id}`, payload)
      : await adminApi.post('/announcements', payload)
    if (r.data.success) {
      toast.success(editor.id ? '公告已更新' : '公告已发布')
      editor.show = false
      load()
    } else toast.error(r.data.error || '保存失败')
  } catch (e) { toast.error(e.response?.data?.detail || '保存失败') }
  finally { editor.saving = false }
}

async function toggle(a) {
  try {
    const r = await adminApi.patch(`/announcements/${a.id}/active`)
    if (r.data.success) {
      toast.success(r.data.announcement.active ? '已启用' : '已停用')
      load()
    } else toast.error(r.data.error || '操作失败')
  } catch (e) { toast.error(e.response?.data?.detail || '操作失败') }
}

async function del(a) {
  if (!confirm(`确认删除公告「${a.title}」？删除后前端/插件不再展示。`)) return
  try {
    const r = await adminApi.delete(`/announcements/${a.id}`)
    if (r.data.success) { toast.success('已删除'); load() }
    else toast.error(r.data.error || '删除失败')
  } catch (e) { toast.error('删除失败') }
}

function fmtDate(s) {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="card-panel pad">
      <div class="head-row">
        <div>
          <h3 style="margin:0 0 4px">📢 公告管理</h3>
          <p class="muted" style="margin:0;font-size:13px">
            发布的公告会推送到业务网页端与浏览器插件；用户读过后不再弹出。同一时间仅展示最新一条「启用」公告。
          </p>
        </div>
        <button class="success" @click="openCreate">＋ 新建公告</button>
      </div>
    </div>

    <div class="card-panel pad">
      <div v-if="loading" class="empty-state">加载中…</div>
      <table v-else class="tbl">
        <thead>
          <tr><th style="width:60px">ID</th><th>标题</th><th style="width:90px">状态</th><th style="width:170px">更新时间</th><th style="width:170px">操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in list" :key="a.id">
            <td class="mono">{{ a.id }}</td>
            <td>{{ a.title }}</td>
            <td><span class="tag" :class="a.active ? 'done' : 'disabled'">{{ a.active ? '启用' : '停用' }}</span></td>
            <td class="muted">{{ fmtDate(a.updated_at) }}</td>
            <td class="ops">
              <a @click="openEdit(a)">编辑</a>
              <a @click="toggle(a)">{{ a.active ? '停用' : '启用' }}</a>
              <a class="del" @click="del(a)">删除</a>
            </td>
          </tr>
          <tr v-if="!list.length"><td colspan="5" class="empty-state">暂无公告，点击右上角「新建公告」发布第一条</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 / 编辑弹窗 -->
    <div v-if="editor.show" class="modal-mask" @click.self="editor.show=false">
      <div class="modal card-panel">
        <h3 style="margin:0 0 14px">{{ editor.id ? '✏️ 编辑公告' : '📢 新建公告' }}</h3>
        <div class="form">
          <div class="row">
            <label>标题</label>
            <input v-model="editor.title" placeholder="公告标题（200 字内）" maxlength="200" />
          </div>
          <div class="row top">
            <label>内容</label>
            <textarea v-model="editor.content" rows="8" placeholder="公告正文，支持换行（10000 字内）"></textarea>
          </div>
          <div class="row">
            <label>状态</label>
            <label class="switch">
              <input type="checkbox" v-model="editor.active" />
              <span>{{ editor.active ? '启用（发布后立即推送）' : '停用（仅保存，不推送）' }}</span>
            </label>
          </div>
        </div>
        <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end">
          <button class="ghost" @click="editor.show=false">取消</button>
          <button class="success" :disabled="editor.saving" @click="save">{{ editor.saving ? '保存中…' : (editor.id ? '保存修改' : '发布公告') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 18px 20px; margin-bottom: 16px; }
.head-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--text-dim); font-weight: 600; }
.mono { font-family: monospace; }
.ops { display: flex; gap: 12px; }
.ops a { cursor: pointer; }
.ops .del { color: var(--danger); }
.tag.done { color: var(--success); border-color: rgba(52,211,153,.4); background: rgba(52,211,153,.08); }
.tag.disabled { color: var(--text-dim); }
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { width: 560px; max-width: 94vw; padding: 22px; }
.form { display: flex; flex-direction: column; gap: 14px; }
.row { display: flex; align-items: center; gap: 14px; }
.row.top { align-items: flex-start; }
.row > label:first-child { width: 56px; color: var(--text-dim); font-size: 13px; flex-shrink: 0; }
.row input[type=text], .row input:not([type]), .row textarea { flex: 1; }
textarea { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-family: inherit; font-size: 14px; resize: vertical; }
textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(91,140,255,.18); outline: none; }
.switch { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.switch input { width: auto; }
</style>
