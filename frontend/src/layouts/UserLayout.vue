<script setup>
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

// 登录态心跳（60s）：服务端开启设备绑定后，本机被顶号/踢下线时
// 经 /auth/me 的 401 → 拦截器清 token + 提示 + 跳登录，实现分钟级感知。
let heartbeatTimer = null

onMounted(async () => {
  if (!auth.card) await auth.fetchMe()
  heartbeatTimer = setInterval(() => {
    if (auth.bizToken) auth.fetchMe()
  }, 60000)
})

onUnmounted(() => {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
})

async function logout() {
  await auth.logout()
  router.replace('/login')
}
</script>

<template>
  <div class="layout">
    <aside class="side">
      <div class="brand">
        <span class="logo">🎧</span>
        <span class="brand-name">有声书下载</span>
      </div>
      <nav class="nav">
        <router-link to="/guide" active-class="active"><span>📖</span> 使用说明</router-link>
        <router-link to="/" exact-active-class="active"><span>🔍</span> 搜索 / 下载</router-link>
        <router-link to="/account" active-class="active"><span>👤</span> 我的账号</router-link>
        <router-link to="/tasks" active-class="active"><span>📥</span> 下载任务</router-link>
        <router-link to="/files" active-class="active"><span>📁</span> 文件管理</router-link>
      </nav>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="card-chip" v-if="auth.card">
          <span class="dot" :class="auth.card.status"></span>
          <span class="code">{{ auth.card.code }}</span>
          <span class="muted" v-if="auth.card.remaining_text">· 剩 {{ auth.card.remaining_text }}</span>
        </div>
        <div class="spacer"></div>
        <button class="ghost" @click="logout">退出登录</button>
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
.logo { font-size: 22px; }
.brand-name { font-weight: 800; font-size: 16px; letter-spacing: .5px; }
.nav { display: flex; flex-direction: column; gap: 6px; }
.nav a {
  display: flex; align-items: center; gap: 10px;
  color: var(--text-dim); padding: 11px 12px; border-radius: 10px;
  font-weight: 600; transition: all .15s ease;
}
.nav a:hover { background: var(--panel-2); color: var(--text); }
.nav a.active { background: linear-gradient(135deg, rgba(91,140,255,.22), rgba(124,92,255,.18)); color: #fff; }

.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
  height: 60px; display: flex; align-items: center; gap: 12px;
  padding: 0 22px; border-bottom: 1px solid var(--border);
  background: rgba(15,18,32,.6); backdrop-filter: blur(6px);
}
.spacer { flex: 1; }
.card-chip {
  display: flex; align-items: center; gap: 8px;
  background: var(--panel); border: 1px solid var(--border);
  padding: 6px 12px; border-radius: 999px; font-size: 13px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); }
.dot.active, .dot.used { background: var(--success); }
.dot.disabled, .dot.expired { background: var(--danger); }
.code { font-weight: 700; letter-spacing: .5px; }
.content { padding: 22px; flex: 1; }
</style>
