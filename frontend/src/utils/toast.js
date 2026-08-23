import { reactive } from 'vue'

const state = reactive({ items: [] })
let _id = 0

export function useToast() {
  function toast(msg, type = 'info', duration = 2600) {
    const id = ++_id
    state.items.push({ id, msg, type })
    setTimeout(() => {
      const i = state.items.findIndex((x) => x.id === id)
      if (i >= 0) state.items.splice(i, 1)
    }, duration)
  }
  return {
    toasts: state.items,
    toast,
    success: (m) => toast(m, 'success'),
    error: (m) => toast(m, 'error'),
    info: (m) => toast(m, 'info'),
  }
}
