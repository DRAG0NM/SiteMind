import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { operatorHeaders } from './vite.operator-token.ts'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8700',
        // B35 登录接入后删除：本机开发时由代理带上运维令牌，批准/驳回才不会被 403。
        configure: proxy => {
          proxy.on('proxyReq', proxyReq => {
            const headers = operatorHeaders()
            if (!headers) return
            for (const [name, value] of Object.entries(headers)) proxyReq.setHeader(name, value)
          })
        },
      },
    },
    fs: { allow: ['..'] },
  },
})
