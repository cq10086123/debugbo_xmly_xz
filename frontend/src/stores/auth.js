import { defineStore } from 'pinia'
import { bizApi, adminApi, getBizToken, getAdminToken, setBizToken, setAdminToken, getSavedCardCode, setSavedCardCode } from '../utils/request'

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
      const { data } = await bizApi.post('/auth/login', {
        code, captchaId, captcha,
        client: 'web',
      })
      if (data.success) {
        this.bizToken = data.token
        this.card = data.card
        setBizToken(data.token)
        // 记住卡密：被踢/重启浏览器后下次自动回填，避免翻历史记录
        // 只信后端返回的 card.code（用户输入可能含前后空格/大小写差异，后端已规范化）
        if (data.card && data.card.code) setSavedCardCode(data.card.code)
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
      // 主动退出登录时清掉缓存的卡密——用户想换卡或不希望下次自动填入的场景
      setSavedCardCode('')
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
