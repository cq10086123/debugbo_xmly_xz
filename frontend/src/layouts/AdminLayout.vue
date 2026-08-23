<script setup>
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getAdminPath } from '../utils/request'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

// 当前后台地址段（默认 admin，随自定义后台地址变化）
const adminPath = computed(() => route.params.adminPath || getAdminPath())

function logout() {
  auth.adminLogout()
  router.replace(`/${adminPath.value}/login`)
}
</script>

<template>
  <div class="layout">
    <aside class="side">
      <div class="brand"><span class="logo">⚙️</span><span class="brand-name">管理后台</span></div>
      <nav class="nav">
        <router-link :to="`/${adminPath}/cards`" active-class="active"><span>🔑</span> 卡密管理</router-link>
        <router-link :to="`/${adminPath}/interfaces`" active-class="active"><span>🧩</span> 接口管理</router-link>
        <router-link :to="`/${adminPath}/xm-accounts`" active-class="active"><span>🍪</span> 后端账号</router-link>
        <router-link :to="`/${adminPath}/announcements`" active-class="active"><span>📢</span> 公告管理</router-link>
        <router-link :to="`/${adminPath}/config`" active-class="active"><span>🔌</span> 接口配置</router-link>
      </nav>
      <div class="side-foot">
        <router-link to="/" class="ghost-link">← 返回前台</router-link>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="muted">管理员已登录</div>
        <div class="spacer"></div>
        <button class="ghost" @click="logout">退出管理</button>
      </header>
      <main class="content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout { display: flex; min-height: 100vh; }
.side {
  width: 220px; flex-shrink: 0;
  background: linear-gradient(180deg, var(--panel), var(--bg-soft));
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; padding: 18px 14px;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 6px 8px 18px; }
.logo { font-size: 20px; }
.brand-name { font-weight: 800; font-size: 16px; }
.nav { display: flex; flex-direction: column; gap: 6px; }
.nav a {
  display: flex; align-items: center; gap: 10px;
  color: var(--text-dim); padding: 11px 12px; border-radius: 10px; font-weight: 600;
}
.nav a:hover { background: var(--panel-2); color: var(--text); }
.nav a.active { background: linear-gradient(135deg, rgba(91,140,255,.22), rgba(124,92,255,.18)); color: #fff; }
.side-foot { margin-top: auto; }
.ghost-link { display: block; padding: 10px 12px; color: var(--text-dim); font-size: 13px; }
.ghost-link:hover { color: var(--text); }
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar { height: 60px; display: flex; align-items: center; gap: 12px; padding: 0 22px; border-bottom: 1px solid var(--border); background: rgba(15,18,32,.6); }
.spacer { flex: 1; }
.content { padding: 22px; flex: 1; }
</style>
