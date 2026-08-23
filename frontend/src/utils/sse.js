// 基于 fetch + ReadableStream 的 SSE 客户端。
// 因为 EventSource 无法携带 Authorization 请求头，这里用 fetch 手动读取流。
// 服务端 /api/.../batch/stream 返回 `data: {json}\n\n` 事件，心跳为 `: heartbeat`。

export function connectSSE(url, token, onMessage) {
  const ctrl = new AbortController()
  let stopped = false

  async function run() {
    try {
      const resp = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
        signal: ctrl.signal,
      })
      if (resp.status === 401) {
        // SSE 走 fetch 不经 axios 拦截器：token 失效时主动清理登录态并重定向，避免任务页静默假死
        try {
          const { setBizToken } = await import('../utils/request')
          setBizToken('')
          const m = await import('../stores/auth')
          const s = m.useAuthStore()
          s.bizToken = ''
          s.card = null
        } catch (e) {}
        if (location.hash !== '#/login') location.hash = '#/login'
        return
      }
      if (!resp.ok || !resp.body) {
        // 其他非 SSE 响应：交给调用方处理
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (!stopped) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const chunk = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          const line = chunk.split('\n').find((l) => l.startsWith('data: '))
          if (!line) continue
          const payload = line.slice(6).trim()
          if (!payload) continue
          try {
            onMessage(JSON.parse(payload))
          } catch (e) {
            // 忽略心跳等非 JSON 行
          }
        }
      }
    } catch (e) {
      // 连接中断（如组件卸载、网络波动）：静默退出
    }
  }

  run()
  return {
    close() {
      stopped = true
      ctrl.abort()
    },
  }
}
