import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getAdminPath, setAdminPath, probeAdminPath } from '../utils/request'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: () => import('../layouts/UserLayout.vue'),
    children: [
      { path: '', name: 'home', component: () => import('../views/Search.vue') },
      { path: 'guide', name: 'guide', component: () => import('../views/Guide.vue') },
      { path: 'account', name: 'account', component: () => import('../views/Account.vue') },
      { path: 'tasks', name: 'tasks', component: () => import('../views/Tasks.vue') },
      { path: 'files', name: 'files', component: () => import('../views/Files.vue') },
    ],
  },
  // 管理后台：地址段动态化（默认 /admin，可在后台配置改为自定义段）
  {
    path: '/:adminPath/login',
    name: 'admin-login',
    component: () => import('../views/admin/AdminLogin.vue'),
    meta: { adminLogin: true },
  },
  {
    path: '/:adminPath',
    component: () => import('../layouts/AdminLayout.vue'),
    meta: { admin: true },
    children: [
      { path: '', redirect: (to) => `/${to.params.adminPath}/cards` },
      { path: 'cards', name: 'admin-cards', component: () => import('../views/admin/AdminCards.vue') },
      { path: 'config', name: 'admin-config', component: () => import('../views/admin/AdminConfig.vue') },
      { path: 'interfaces', name: 'admin-interfaces', component: () => import('../views/admin/AdminInterfaces.vue') },
      { path: 'xm-accounts', name: 'admin-xm-accounts', component: () => import('../views/admin/AdminXmAccounts.vue') },
      { path: 'announcements', name: 'admin-announcements', component: () => import('../views/admin/AdminAnnouncements.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/login' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const isAdminRoute = to.matched.some((r) => r.meta.admin || r.meta.adminLogin)
  if (isAdminRoute) {
    // 动态段 /:adminPath 什么都能匹配，必须先探测后端确认该段是真实挂载的后台地址，
    // 否则垃圾段（如旧默认 admin）会被存进 localStorage，之后所有 adminApi 打向错误路径 → POST 全 405
    const seg = to.params.adminPath
    if (seg) {
      const ok = await probeAdminPath(seg)
      if (ok === true) setAdminPath(seg)          // 只有验证通过才采信
      else if (ok === false) {
        // 无效段：登录页放行（页面内展示「地址无效」提示），后台内页踢去登录页
        if (to.meta.adminLogin) return true
        return `/${seg}/login`
      }
      // ok === null（后端暂不可达）→ 不阻断、不覆盖已存储的地址段
    }
    if (to.meta.adminLogin) return true
    if (!auth.adminLoggedIn) return `/${getAdminPath()}/login`
    return true
  }
  if (to.path === '/login') {
    if (auth.isLoggedIn) return '/guide'
    return true
  }
  if (!auth.isLoggedIn) return '/login'
  return true
})

export default router
