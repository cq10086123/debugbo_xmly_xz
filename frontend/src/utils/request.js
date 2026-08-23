import axios from 'axios'

// 业务 token（卡密登录）
const BIZ_KEY = 'auth_token'
// 管理 token（管理员登录）
const ADMIN_KEY = 'admin_token'
// 管理后台地址段（与后端 api_config.admin_path 对应，默认 admin）
const ADMIN_PATH_KEY = 'admin_path'

export function getBizToken() {
  return localStorage.getItem(BIZ_KEY) || ''
}
export function setBizToken(t) {
  if (t) localStorage.setItem(BIZ_KEY, t)
  else localStorage.removeItem(BIZ_KEY)
}
export function getAdminToken() {
  return localStorage.getItem(ADMIN_KEY) || ''
}
export function setAdminToken(t) {
  if (t) localStorage.setItem(ADMIN_KEY, t)
  else localStorage.removeItem(ADMIN_KEY)
}

// 后台地址段：访问 /#/{path}/... 时由路由守卫写入；缺省 admin（兼容默认部署）
export function getAdminPath() {
  return localStorage.getItem(ADMIN_PATH_KEY) || 'admin'
}
export function setAdminPath(p) {
  if (p) localStorage.setItem(ADMIN_PATH_KEY, p)
}

// 探测后台地址段是否真实有效（防动态路由把任意垃圾段当真地址存下来）：
// GET /api/{seg}/config —— 有效段路由存在 → 401（未带 token）；无效段落到 StaticFiles → 404。
// （不能用 GET /login 探测：路径匹配但方法不符时 Starlette 会继续落到 StaticFiles，两者都是 404 无法区分）
// 返回 true=有效 / false=无效 / null=后端不可达（不确定，调用方应放行而非阻断）
export async function probeAdminPath(p) {
  if (!p) return null
  const ck = 'admin_path_probe:' + p
  const cached = sessionStorage.getItem(ck)
  if (cached === '1') return true
  if (cached === '0') return false
  try {
    const resp = await fetch(`/api/${encodeURIComponent(p)}/config`, { method: 'GET' })
    // 200=已登录可达 / 401=路由存在未授权 / 403=路径命中后台前缀但非局域网（也是有效段）
    const ok = resp.status === 200 || resp.status === 401 || resp.status === 403
    sessionStorage.setItem(ck, ok ? '1' : '0')
    return ok
  } catch (e) {
    return null
  }
}

function makeInstance(baseURL, tokenGetter, onUnauthorized) {
  const inst = axios.create({ baseURL, timeout: 60000 })
  inst.interceptors.request.use((cfg) => {
    const t = tokenGetter()
    if (t) cfg.headers = cfg.headers || {}
    if (t) cfg.headers.Authorization = `Bearer ${t}`
    return cfg
  })
  inst.interceptors.response.use(
    (resp) => resp,
    (err) => {
      if (err.response && err.response.status === 401) {
        onUnauthorized()
      }
      return Promise.reject(err)
    }
  )
  return inst
}

// 业务接口实例（带卡密 token）—— baseURL /api
export const bizApi = makeInstance(
  '/api',
  getBizToken,
  () => {
    setBizToken('')
    // 同步清空 Pinia 登录态，避免守卫短时间内仍判已登录导致 API 层反复 401/重定向
    import('../stores/auth').then((m) => {
      const s = m.useAuthStore()
      s.bizToken = ''
      s.card = null
    }).catch(() => {})
    if (location.hash.startsWith(`#/${getAdminPath()}`)) return
    if (location.pathname !== '/login' && location.hash !== '#/login') {
      location.hash = '#/login'
    }
  }
)

// 管理接口实例（带 admin token）—— baseURL 动态：/api/{admin_path}
export const adminApi = makeInstance(
  '/api/admin',
  getAdminToken,
  () => {
    setAdminToken('')
    import('../stores/auth').then((m) => {
      const s = m.useAuthStore()
      s.adminToken = ''
      s.adminLoggedIn = false
    }).catch(() => {})
    const loginHash = `#/${getAdminPath()}/login`
    if (location.hash !== loginHash) {
      location.hash = loginHash
    }
  }
)

// 每次请求前按当前 admin_path 重写 baseURL，支持自定义后台地址
adminApi.interceptors.request.use((cfg) => {
  cfg.baseURL = `/api/${getAdminPath()}`
  return cfg
})
