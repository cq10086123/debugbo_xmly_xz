// deviceId.js — 设备 ID 工具（设备绑定用）
// 生成一次后持久化在 localStorage 的独立 key（'device_id'），退出登录不清除：
// 用户清 token 重新登录时仍是同一台设备，不触发顶号换绑。
const DEVICE_KEY = 'device_id'

/**
 * 获取本浏览器设备 ID（不存在则生成）。
 * crypto.randomUUID 现代浏览器均支持；退回方案用 getRandomValues 手工拼 UUID v4。
 */
export function getDeviceId() {
  let id = ''
  try { id = localStorage.getItem(DEVICE_KEY) || '' } catch (e) { /* 隐私模式等 */ }
  if (id) return id
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    id = window.crypto.randomUUID()
  } else if (window.crypto && window.crypto.getRandomValues) {
    // UUID v4 手工实现
    const b = new Uint8Array(16)
    window.crypto.getRandomValues(b)
    b[6] = (b[6] & 0x0f) | 0x40
    b[8] = (b[8] & 0x3f) | 0x80
    const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
    id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  } else {
    id = 'dev-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)
  }
  try { localStorage.setItem(DEVICE_KEY, id) } catch (e) { /* 存不下则每次临时生成 */ }
  return id
}
