import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],

  server: {
    // Vue 前端端口
    port: 5174,

    // 转发前端 API 请求
    proxy: {
      '/api': {
        // FastAPI 后端端口
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})
