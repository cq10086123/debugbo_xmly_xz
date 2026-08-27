<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { adminApi } from '../../utils/request'
import { useToast } from '../../utils/toast'

const toast = useToast()
const loading = ref(false)
const list = ref([])
const showForm = ref(false)
const formError = ref('')
const editing = ref(false)
const saving = ref(false)
const form = reactive({
  name: '', display_name: '', type: 'script', enabled: true,
  priority: 100, description: '',
})

const TYPE_OPTIONS = [
  { value: 'official', label: '官方接口（喜马拉雅）' },
  { value: 'script', label: 'Python 脚本接口' },
]

const DEFAULT_SCRIPT_SEARCH = `import requests

# 脚本自己发请求 + 解析。引擎注入 params：keyword / encoded_keyword / timestamp / timestamp_sec / page
def parse(params):
    """搜索：返回 list[dict]，每项必含 id、bookTitle"""
    keyword = params.get('keyword', '')
    url = "https://api.example.com/search"
    try:
        data = requests.get(url, params={"kw": keyword}, timeout=15).json()
    except Exception as e:
        print("请求失败:", e)
        return []
    result = []
    for item in (data.get('data', {}).get('list', []) or []):
        result.append({
            "id": str(item.get('id', '')),
            "bookTitle": item.get('title', ''),
            "bookImage": item.get('cover', ''),
            "bookAnchor": item.get('author', ''),
            "count": item.get('trackCount', 0),
        })
    return result
`

const DEFAULT_SCRIPT_CHAPTERS = `import requests

# 引擎注入 params：bookId / page / page0 / size / count 及搜索结果透传的自定义字段
def parse(params):
    """章节：返回 list[dict]，每项必含 chapter_id、title"""
    book_id = params.get('bookId', '')
    url = f"https://api.example.com/chapters?albumId={book_id}"
    try:
        data = requests.get(url, timeout=15).json()
    except Exception as e:
        print("请求失败:", e)
        return []
    result = []
    for idx, item in enumerate(data.get('chapters', []) or []):
        result.append({
            "chapter_id": str(item.get('id', '')),
            "title": item.get('name', '') or item.get('title', ''),
            "order": idx + 1,
        })
    return result
`

const DEFAULT_SCRIPT_AUDIO = `import requests

# 引擎注入 params：bookId / chapterId / trackId / rid 及章节结果透传的自定义字段
def parse(params):
    """音频：返回音频直链字符串（http/https 开头）"""
    chapter_id = params.get('chapterId') or params.get('trackId', '')
    url = f"https://api.example.com/audio?id={chapter_id}"
    try:
        data = requests.get(url, timeout=15).json()
    except Exception as e:
        print("请求失败:", e)
        return None
    return data.get('url', '') or data.get('playUrl', '')
`

const STAGE_OPTIONS = [
  { value: 'search', label: '搜索脚本 (parse)' },
  { value: 'chapters', label: '章节脚本 (parse)' },
  { value: 'audio', label: '音频脚本 (parse)' },
]

const currentStage = ref('search')
const scriptTimeout = ref(30)
const scriptForm = reactive({
  search: '',
  chapters: '',
  audio: '',
})

// 在线调试面板
const debugStage = ref('search')
const debugParams = reactive({ keyword: '斗破苍穹', page: 1, book_id: '', chapter_id: '' })
const debugRunning = ref(false)
const debugResult = ref(null)

// 允许导入的额外模块（接口级白名单追加；与调试面板共用）
const allowedModules = ref('')

function parseModules(str) {
  return String(str || '')
    .split(/[\s,，;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

// 从后端加载示例脚本（与参考项目 py 一致的 parse(params) 契约）
const examplesCache = ref(null)
const loadingExample = ref(false)
async function loadExample() {
  const stage = currentStage.value
  loadingExample.value = true
  try {
    if (!examplesCache.value) {
      const r = await adminApi.get('/interfaces/examples')
      if (r.data.success) examplesCache.value = r.data.examples || {}
    }
    const ex = examplesCache.value[stage]
    if (ex) {
      scriptForm[stage] = ex
      toast.success(`已加载${stage === 'search' ? '搜索' : stage === 'chapters' ? '章节' : '音频'}示例脚本`)
    } else {
      toast.error('未找到该阶段示例')
    }
  } catch (e) {
    toast.error('加载示例失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loadingExample.value = false
  }
}

// 测试弹窗（列表页一键测试）
const showTest = ref(false)
const testName = ref('')
const testing = ref(false)
const testForm = reactive({ keyword: '斗破苍穹', book_id: '', chapter_id: '' })
const testResult = ref(null)

async function load() {
  loading.value = true
  try {
    const r = await adminApi.get('/interfaces')
    if (r.data.success) list.value = r.data.interfaces || []
  } catch (e) {}
  loading.value = false
}

function typeLabel(t) {
  return TYPE_OPTIONS.find((o) => o.value === t)?.label || t
}

function resetScriptForm() {
  currentStage.value = 'search'
  debugStage.value = 'search'
  scriptTimeout.value = 30
  allowedModules.value = ''
  Object.assign(scriptForm, {
    search: DEFAULT_SCRIPT_SEARCH,
    chapters: DEFAULT_SCRIPT_CHAPTERS,
    audio: DEFAULT_SCRIPT_AUDIO,
  })
}

function openCreate() {
  editing.value = false
  formError.value = ''
  Object.assign(form, {
    name: '', display_name: '', type: 'script', enabled: true,
    priority: 100, description: '',
  })
  resetScriptForm()
  debugResult.value = null
  showForm.value = true
}

function openEdit(it) {
  editing.value = true
  formError.value = ''
  Object.assign(form, {
    name: it.name, display_name: it.display_name, type: it.type,
    enabled: it.enabled, priority: it.priority ?? 100,
    description: it.description || '',
  })

  const scripts = (it.config && it.config.scripts) || {}
  currentStage.value = 'search'
  debugStage.value = 'search'
  scriptTimeout.value = (it.config && it.config.script_timeout) || 30
  allowedModules.value = (it.config && Array.isArray(it.config.allowed_modules))
    ? it.config.allowed_modules.join(', ')
    : ''
  Object.assign(scriptForm, {
    search: scripts.search || DEFAULT_SCRIPT_SEARCH,
    chapters: scripts.chapters || DEFAULT_SCRIPT_CHAPTERS,
    audio: scripts.audio || DEFAULT_SCRIPT_AUDIO,
  })
  debugResult.value = null
  showForm.value = true
}

async function submitForm() {
  formError.value = ''
  let config = {}
  if (form.type === 'script') {
    const to = Number(scriptTimeout.value)
    if (!to || to < 1 || to > 120) {
      formError.value = '脚本超时时间必须在 1~120 秒之间'
      return
    }
    config = {
      script_timeout: to,
      allowed_modules: parseModules(allowedModules.value),
      scripts: {
        search: scriptForm.search,
        chapters: scriptForm.chapters,
        audio: scriptForm.audio,
      },
    }
  }
  const payload = {
    display_name: form.display_name,
    type: form.type,
    enabled: form.enabled,
    priority: Number(form.priority) || 100,
    description: form.description,
    config,
  }
  if (!editing.value) {
    if (!form.name || !/^[a-zA-Z0-9_]+$/.test(form.name)) {
      formError.value = '接口标识只能包含字母、数字、下划线（≤64 字符）'
      return
    }
    if (!form.display_name) { formError.value = '显示名称不能为空'; return }
    payload.name = form.name
  }
  saving.value = true
  try {
    const url = editing.value ? `/interfaces/${form.name}` : '/interfaces'
    const method = editing.value ? 'put' : 'post'
    const r = await adminApi[method](url, payload)
    if (r.data.success) {
      toast.success(editing.value ? '接口已更新' : '接口已创建')
      showForm.value = false
      await load()
    } else {
      formError.value = r.data.error || r.data.message || '操作失败'
    }
  } catch (e) {
    formError.value = e.response?.data?.detail || e.response?.data?.error || '请求失败'
  } finally { saving.value = false }
}

async function remove(it) {
  if (it.builtin) { toast.error('内置接口不可删除，仅可禁用'); return }
  if (!window.confirm(`确定删除接口「${it.display_name}」(${it.name})？此操作不可恢复。`)) return
  try {
    const r = await adminApi.delete(`/interfaces/${it.name}`)
    if (r.data.success) { toast.success('已删除'); await load() }
    else toast.error(r.data.message || '删除失败')
  } catch (e) { toast.error(e.response?.data?.detail || '删除失败') }
}

function openTest(it) {
  testName.value = it.name
  testResult.value = null
  Object.assign(testForm, { keyword: '斗破苍穹', book_id: '', chapter_id: '' })
  showTest.value = true
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    const r = await adminApi.post(`/interfaces/${testName.value}/test`, { ...testForm })
    if (r.data.success) testResult.value = r.data.result
    else toast.error('测试失败')
  } catch (e) { toast.error(e.response?.data?.detail || '测试失败') }
  finally { testing.value = false }
}

// 在线调试：运行当前编辑器中的脚本源码
async function runDebug() {
  debugRunning.value = true
  debugResult.value = null
  try {
    const stage = debugStage.value
    const params = {}
    if (stage === 'search') {
      params.keyword = debugParams.keyword || ''
      params.page = Number(debugParams.page) || 1
    } else if (stage === 'chapters') {
      params.book_id = debugParams.book_id || ''
    } else {
      params.book_id = debugParams.book_id || ''
      params.chapter_id = debugParams.chapter_id || ''
    }
    const r = await adminApi.post('/interfaces/test-script', {
      stage,
      source: scriptForm[stage],
      timeout: Number(scriptTimeout.value) || 30,
      params,
      allowed_modules: parseModules(allowedModules.value),
    })
    debugResult.value = r.data
  } catch (e) {
    debugResult.value = { success: false, error: e.response?.data?.detail || String(e) }
  } finally {
    debugRunning.value = false
  }
}

// 当切换脚本标签时，默认把调试目标也切到对应 stage
watch(currentStage, (val) => { debugStage.value = val })

// 根据当前调试阶段预填必要提示
const debugParamHints = computed(() => {
  if (debugStage.value === 'search') return { keyword: '搜索关键词', page: '页码' }
  if (debugStage.value === 'chapters') return { book_id: '书籍 ID（albumId）' }
  return { book_id: '书籍 ID', chapter_id: '章节 ID（trackId）' }
})

onMounted(load)
</script>

<template>
  <div class="card-panel pad">
    <div class="head">
      <div>
        <h3 style="margin:0 0 6px">🧩 接口管理</h3>
        <p class="muted" style="margin:0">在此增删音源接口。官方接口为系统内置；Python 脚本接口可自行编写任意第三方音源，支持在线调试。</p>
      </div>
      <button class="success" @click="openCreate">＋ 新增接口</button>
    </div>

    <div v-if="loading" class="empty-state">加载中…</div>

    <table v-else class="tbl">
      <thead>
        <tr>
          <th>标识</th><th>显示名称</th><th>类型</th><th>优先级</th><th>状态</th><th>说明</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="it in list" :key="it.name">
          <td><code>{{ it.name }}</code></td>
          <td>{{ it.display_name }}</td>
          <td><span class="badge" :class="'b-' + it.type">{{ typeLabel(it.type) }}</span></td>
          <td>{{ it.priority }}</td>
          <td>
            <span class="dot" :class="it.enabled ? 'on' : 'off'"></span>
            {{ it.enabled ? '已启用' : '已禁用' }}
            <span v-if="it.builtin" class="builtin">内置</span>
          </td>
          <td class="desc">{{ it.description || '—' }}</td>
          <td class="ops">
            <button class="ghost sm" @click="openTest(it)">测试</button>
            <button class="ghost sm" @click="openEdit(it)">编辑</button>
            <button class="danger sm" :disabled="it.builtin" @click="remove(it)">删除</button>
          </td>
        </tr>
        <tr v-if="!list.length"><td colspan="7" class="empty-state">暂无接口</td></tr>
      </tbody>
    </table>

    <!-- 独立编辑页面弹窗 -->
    <div v-if="showForm" class="mask" @click.self="showForm = false">
      <div class="editor-dialog">
        <div class="editor-head">
          <h4 style="margin:0">{{ editing ? '编辑接口' : '新增接口' }}</h4>
          <button class="ghost sm" @click="showForm = false">退出</button>
        </div>

        <!-- 基础信息 -->
        <div class="basic-row">
          <div class="basic-item">
            <label>接口标识</label>
            <input v-model="form.name" :disabled="editing" placeholder="如 my_source" />
          </div>
          <div class="basic-item">
            <label>显示名称</label>
            <input v-model="form.display_name" placeholder="如 我的自定义音源" />
          </div>
          <div class="basic-item narrow">
            <label>类型</label>
            <select v-model="form.type">
              <option v-for="o in TYPE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="basic-item narrow">
            <label>优先级</label>
            <input v-model="form.priority" type="number" />
          </div>
          <div class="basic-item narrow">
            <label>启用</label>
            <label class="switch">
              <input type="checkbox" v-model="form.enabled" />
              <span>{{ form.enabled ? '启用' : '禁用' }}</span>
            </label>
          </div>
          <div class="basic-item">
            <label>说明</label>
            <input v-model="form.description" placeholder="可选备注" />
          </div>
        </div>

        <!-- Python 脚本编辑 + 在线调试 -->
        <div v-if="form.type === 'script'" class="script-workspace">
          <div class="editor-main">
            <div class="stage-tabs">
              <button v-for="s in STAGE_OPTIONS" :key="s.value" type="button"
                :class="{active: currentStage===s.value}" @click="currentStage=s.value">
                {{ s.label }}
              </button>
              <button type="button" class="example-btn" :disabled="loadingExample" @click="loadExample">
                {{ loadingExample ? '加载中…' : '📥 加载示例' }}
              </button>
              <div class="timeout-box">
                <label>超时</label>
                <input v-model="scriptTimeout" type="number" min="1" max="120" />
                <span>秒</span>
              </div>
            </div>
            <div class="code-box">
              <textarea v-show="currentStage==='search'" v-model="scriptForm.search" spellcheck="false" class="code"></textarea>
              <textarea v-show="currentStage==='chapters'" v-model="scriptForm.chapters" spellcheck="false" class="code"></textarea>
              <textarea v-show="currentStage==='audio'" v-model="scriptForm.audio" spellcheck="false" class="code"></textarea>
            </div>
            <div class="modules-box">
              <label>允许导入的额外模块（可选，一般无需填写）</label>
              <input v-model="allowedModules" placeholder="逗号或换行分隔，如 secrets,warnings,Crypto,jwt" />
              <span class="m-hint">引擎默认已放行绝大多数模块（含 requests 及第三方 Crypto / jwt 等），仅拦截 os / sys / subprocess / socket / pickle / ctypes 等危险模块。仅当你的脚本用到极特殊的模块且被误拦时，才在此追加。</span>
            </div>
            <p class="hint">
              脚本在常驻沙箱进程中运行，只需定义一个函数
              <code>def parse(params):</code>（与参考项目
              <code>C:\Users\11754\Desktop\1\py</code> 的脚本契约完全一致）。脚本自己发请求并解析：
              <br />· <strong>搜索</strong>：返回 <code>list[dict]</code>，每项必含 <code>id</code>、<code>bookTitle</code>（可选 bookImage / bookAnchor / count）
              <br />· <strong>章节</strong>：返回 <code>list[dict]</code>，每项必含 <code>chapter_id</code>、<code>title</code>（可选 order / duration）
              <br />· <strong>音频</strong>：返回音频直链 <code>字符串</code>（http/https 开头）
              <br />引擎自动注入 <code>params</code>：搜索含 <code>keyword</code> / <code>encoded_keyword</code> / <code>timestamp</code> / <code>page</code>；
              章节含 <code>bookId</code> / <code>page</code> / <code>size</code> / <code>count</code>；音频含 <code>bookId</code> / <code>chapterId</code> / <code>trackId</code>。
              搜索结果、章节结果的自定义字段会自动透传给下一阶段（<code>params.get('xxx')</code> 读取）。
              <br />可用：内置 <code>requests</code>（无需 import）、<code>json</code> <code>re</code> <code>hashlib</code> <code>base64</code>
              <code>time</code> <code>datetime</code> <code>hmac</code> <code>random</code> <code>math</code>
              <code>secrets</code> <code>struct</code> 等标准库，以及便捷函数
              <code>http_get</code> <code>http_post</code> <code>md5</code> <code>sha1</code>
              <code>base64_encode</code> <code>url_encode</code> <code>timestamp</code>
              <code>timestamp_ms</code> <code>get_nested</code>。
            </p>
          </div>

          <div class="debug-panel">
            <div class="panel-title">在线调试</div>
            <div class="debug-modules">
              <label>额外模块（可选）</label>
              <input v-model="allowedModules" placeholder="调试同样适用，如 Crypto,jwt" />
            </div>
            <div class="debug-stage">
              <label>调试目标</label>
              <select v-model="debugStage">
                <option v-for="s in STAGE_OPTIONS" :key="s.value" :value="s.value">{{ s.label }}</option>
              </select>
            </div>
            <div v-if="debugStage === 'search'" class="debug-fields">
              <div class="field"><label>搜索词</label><input v-model="debugParams.keyword" :placeholder="debugParamHints.keyword" /></div>
              <div class="field"><label>页码</label><input v-model="debugParams.page" type="number" :placeholder="debugParamHints.page" /></div>
            </div>
            <div v-else-if="debugStage === 'chapters'" class="debug-fields">
              <div class="field"><label>书籍 ID</label><input v-model="debugParams.book_id" :placeholder="debugParamHints.book_id" /></div>
            </div>
            <div v-else class="debug-fields">
              <div class="field"><label>书籍 ID</label><input v-model="debugParams.book_id" :placeholder="debugParamHints.book_id" /></div>
              <div class="field"><label>章节 ID</label><input v-model="debugParams.chapter_id" :placeholder="debugParamHints.chapter_id" /></div>
            </div>
            <button class="success run-btn" :disabled="debugRunning" @click="runDebug">
              {{ debugRunning ? '运行中…' : '▶ 运行当前脚本' }}
            </button>
            <div class="debug-output">
              <div class="out-title">输出结果</div>
              <pre v-if="debugResult" :class="debugResult.success ? 'ok' : 'err'">{{ JSON.stringify(debugResult, null, 2) }}</pre>
              <div v-else class="out-empty">点击「运行当前脚本」查看结果</div>
            </div>
          </div>
        </div>

        <div v-if="form.type === 'official'" class="hint-box">
          官方接口由喜马拉雅登录账号驱动，无需额外密钥配置。
        </div>

        <div v-if="formError" class="err">{{ formError }}</div>
        <div class="editor-actions">
          <button class="ghost" @click="showForm = false">取消</button>
          <button class="success" :disabled="saving" @click="submitForm">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- 测试弹窗 -->
    <div v-if="showTest" class="mask" @click.self="showTest = false">
      <div class="dialog">
        <h4 style="margin:0 0 12px">测试接口：<code>{{ testName }}</code></h4>
        <div class="form">
          <div class="row"><label>搜索词</label><input v-model="testForm.keyword" placeholder="如 斗破苍穹" /></div>
          <div class="row"><label>书籍 ID</label><input v-model="testForm.book_id" placeholder="可选，测试章节" /></div>
          <div class="row"><label>章节 ID</label><input v-model="testForm.chapter_id" placeholder="可选，测试音频" /></div>
          <div class="actions">
            <button class="ghost" @click="showTest = false">关闭</button>
            <button class="success" :disabled="testing" @click="runTest">{{ testing ? '测试中…' : '开始测试' }}</button>
          </div>
        </div>

        <div v-if="testResult" class="result">
          <div v-for="sec in ['search', 'chapters', 'audio']" :key="sec" class="sec">
            <div class="sec-h">
              <strong>{{ sec === 'search' ? '搜索' : sec === 'chapters' ? '章节' : '音频' }}</strong>
              <span class="dot" :class="testResult[sec] && testResult[sec].success ? 'on' : 'off'"></span>
              <span>{{ testResult[sec] && testResult[sec].success ? '通过' : '失败' }}</span>
            </div>
            <div v-if="testResult[sec] && testResult[sec].error" class="err">{{ testResult[sec].error }}</div>
            <div v-else-if="testResult[sec]" class="ok">
              <span v-if="testResult[sec].count !== undefined">命中 {{ testResult[sec].count }} 条</span>
              <span v-if="testResult[sec].album_title"> · 书名：{{ testResult[sec].album_title }}</span>
              <span v-if="testResult[sec].url"> · {{ testResult[sec].url }}</span>
              <div v-if="testResult[sec].cover" class="cover-check"
                   :class="testResult[sec].cover.ok ? 'cover-ok' : 'cover-warn'">
                <template v-if="testResult[sec].cover.ok">
                  ✅ 封面已读取（{{ testResult[sec].cover.detected }}/{{ testResult[sec].cover.total }} 条），
                  网页与 AI 均可显示封面
                </template>
                <template v-else>
                  ⚠️ 未读取到封面 —— 网页搜索页与 AI 都将没有封面图。
                  <div class="cover-hint">{{ testResult[sec].cover.hint }}</div>
                </template>
              </div>
              <ul v-if="testResult[sec].sample && testResult[sec].sample.length">
                <li v-for="(s, i) in testResult[sec].sample" :key="i">{{ JSON.stringify(s) }}</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cover-check { margin: 8px 0; padding: 8px 10px; border-radius: 6px; font-size: 13px; line-height: 1.5; }
.cover-ok { background: rgba(34, 197, 94, .12); color: #16a34a; }
.cover-warn { background: rgba(234, 179, 8, .12); color: #b45309; }
.cover-hint { margin-top: 4px; opacity: .9; word-break: break-all; }
.pad { padding: 20px; }
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 16px; }
.head .success { flex-shrink: 0; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 10px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.tbl th { color: var(--text-dim); font-weight: 700; font-size: 12px; }
.tbl code { background: var(--panel-2); padding: 2px 6px; border-radius: 5px; font-size: 12px; }
.desc { color: var(--text-dim); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops { white-space: nowrap; }
.badge { padding: 3px 8px; border-radius: 20px; font-size: 11px; font-weight: 700; }
.b-official { background: rgba(91,140,255,.2); color: #8fb0ff; }
.b-script { background: rgba(155,89,182,.2); color: #d7b8e6; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.dot.on { background: #2ecc71; }
.dot.off { background: #888; }
.builtin { margin-left: 6px; font-size: 11px; color: var(--text-dim); border: 1px solid var(--border); padding: 1px 6px; border-radius: 10px; }
.sm { padding: 5px 10px; font-size: 12px; margin-right: 6px; }
.danger { background: rgba(255,107,107,.15); color: #ff8585; border: 1px solid rgba(255,107,107,.4); border-radius: 8px; cursor: pointer; }
.danger:disabled { opacity: .4; cursor: not-allowed; }
.mask { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 20px; }
.dialog { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 22px; width: 100%; max-width: 640px; max-height: 88vh; overflow: auto; }
.form { display: flex; flex-direction: column; }
.result { margin-top: 18px; border-top: 1px solid var(--border); padding-top: 14px; }
.sec { margin-bottom: 14px; }
.sec-h { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.sec .err { color: #ff8585; font-size: 12px; }
.sec .ok { font-size: 12px; color: var(--text-dim); }
.sec .ok ul { margin: 6px 0 0; padding-left: 16px; }
.sec .ok li { margin-bottom: 4px; word-break: break-all; }
.empty-state { color: var(--text-dim); padding: 24px; text-align: center; }

/* 独立编辑页 */
.editor-dialog { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 20px; width: 100%; max-width: 1100px; max-height: 92vh; overflow: auto; display: flex; flex-direction: column; }
.editor-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.basic-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 14px; }
.basic-item { display: flex; flex-direction: column; gap: 4px; }
.basic-item.narrow { grid-column: span 1; }
.basic-item label { color: var(--text-dim); font-size: 12px; }
.basic-item input, .basic-item select { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
.switch { display: flex; align-items: center; gap: 8px; }
.switch input { width: auto; }

.script-workspace { display: grid; grid-template-columns: 1.5fr 1fr; gap: 16px; flex: 1; min-height: 0; margin-bottom: 14px; }
.editor-main { display: flex; flex-direction: column; min-height: 0; }
.stage-tabs { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.stage-tabs button { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }
.stage-tabs button.active { background: rgba(91,140,255,.2); border-color: rgba(91,140,255,.5); color: #8fb0ff; }
.example-btn { background: rgba(120,200,140,.18); border: 1px solid rgba(120,200,140,.45); color: #9fe0b0; border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; margin-left: 4px; }
.example-btn:disabled { opacity: .5; cursor: not-allowed; }
.timeout-box { margin-left: auto; display: flex; align-items: center; gap: 6px; color: var(--text-dim); font-size: 12px; }
.timeout-box input { width: 60px; background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 4px 6px; font-size: 12px; }
.code-box { flex: 1; min-height: 360px; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.code-box textarea { width: 100%; height: 100%; min-height: 360px; border: none; background: var(--bg-soft); color: var(--text); font-family: ui-monospace, Menlo, Consolas, 'Courier New', monospace; font-size: 13px; line-height: 1.55; padding: 12px; resize: none; outline: none; }
.hint { font-size: 12px; color: var(--text-dim); margin: 8px 0 0; }
.hint code { background: var(--panel-2); padding: 1px 5px; border-radius: 4px; }
.modules-box { display: flex; flex-direction: column; gap: 4px; margin: 10px 0 0; }
.modules-box label { color: var(--text-dim); font-size: 12px; }
.modules-box input { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
.m-hint { font-size: 11px; color: var(--text-dim); opacity: .85; }
.debug-modules { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.debug-modules label { color: var(--text-dim); font-size: 12px; }
.debug-modules input { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }

.debug-panel { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 14px; display: flex; flex-direction: column; min-height: 0; }
.debug-stage { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.debug-stage label { color: var(--text-dim); font-size: 12px; }
.debug-stage select { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
.debug-fields { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field label { color: var(--text-dim); font-size: 12px; }
.field input { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
.run-btn { width: 100%; margin-bottom: 12px; }
.debug-output { flex: 1; min-height: 120px; display: flex; flex-direction: column; min-height: 0; }
.out-title { font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }
.debug-output pre { flex: 1; background: var(--bg-soft); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.5; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
.debug-output pre.ok { color: #7ee787; }
.debug-output pre.err { color: #ff8585; }
.out-empty { flex: 1; background: var(--bg-soft); border: 1px dashed var(--border); border-radius: 8px; padding: 10px; font-size: 12px; color: var(--text-dim); display: flex; align-items: center; justify-content: center; }

.hint-box { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 14px; color: var(--text-dim); font-size: 13px; margin-bottom: 14px; }
.err { color: #ff6b6b; font-size: 13px; margin: 8px 0; }
.editor-actions { display: flex; gap: 10px; justify-content: flex-end; }

.row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.row > label:first-child { width: 110px; color: var(--text-dim); font-size: 13px; flex-shrink: 0; }
.row input { flex: 1; background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
.actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }
</style>
