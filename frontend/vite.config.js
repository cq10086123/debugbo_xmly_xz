import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端托管 frontend/dist；开发模式用 Vite proxy 把 /api 转发到后端 6500
export default defineConfig({
  plugins: [vue()],
  server: {
    // port: 5173   // 本地开发前端(dev:local)专用端口；正式验证只用6500，请勿在验证环境运行 dev
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:6500',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
