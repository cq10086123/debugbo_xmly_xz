import { defineStore } from 'pinia'
import { bizApi, adminApi, getBizToken, getAdminToken, setBizToken, setAdminToken } from '../utils/request'
import { getDeviceId } from '../utils/deviceId'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    bizToken: getBizToken(),
    card: null,          // 当前卡密信息
    adminToken: getAdminToken(),
    adminLoggedIn: !!getAdminToken(),
  }),
  getters: {
    isLoggedIn: (s) => !!s.bizToken,
  },
  actions: {
    async login(code, captchaId, captcha) {
      // 设备绑定：携带本浏览器设备 ID 与端类型（服务端开关关闭时忽略）
      const { data } = await bizApi.post('/auth/login', {
        code, captchaId, captcha,
        deviceId: getDeviceId(),
        client: 'web',
      })
      if (data.success) {
        this.bizToken = data.token
        this.card = data.card
        setBizToken(data.token)
      }
      return data
    },
    async fetchMe() {
      if (!this.bizToken) return null
      try {
        const { data } = await bizApi.get('/auth/me')
        if (data.success) {
          this.card = data.card
          return data.card
        }
      } catch (e) {
        this.bizToken = ''
        setBizToken('')
      }
      return null
    },
    async logout() {
      try { await bizApi.post('/auth/logout') } catch (e) {}
      this.bizToken = ''
      this.card = null
      setBizToken('')
    },
    async adminLogin(username, password) {
      const { data } = await adminApi.post('/login', { username, password })
      if (data.success) {
        this.adminToken = data.token
        this.adminLoggedIn = true
        setAdminToken(data.token)
      }
      return data
    },
    adminLogout() {
      this.adminToken = ''
      this.adminLoggedIn = false
      setAdminToken('')
    },
  },
})
