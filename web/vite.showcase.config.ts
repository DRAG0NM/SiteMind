import { existsSync, renameSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { viteSingleFile } from 'vite-plugin-singlefile'
import { operatorHeaders } from './vite.operator-token.ts'

const root = fileURLToPath(new URL('.', import.meta.url))

/** 静态回放页：build:showcase 产出单文件 dist-showcase/index.html，拷到任意静态托管即可，离线双击也能打开。 */
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    viteSingleFile(),
    {
      name: 'showcase-index-entry',
      closeBundle() {
        const out = resolve(root, 'dist-showcase')
        if (existsSync(resolve(out, 'showcase.html')) && !existsSync(resolve(out, 'index.html'))) {
          renameSync(resolve(out, 'showcase.html'), resolve(out, 'index.html'))
        }
      },
    },
  ],
  base: './',
  // 开发预览时页面带 ?run_id 直接读本机后端；离线单文件构建不受影响。
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
  },
  build: {
    outDir: 'dist-showcase',
    emptyOutDir: true,
    rollupOptions: { input: { showcase: resolve(root, 'showcase.html') } },
  },
})
