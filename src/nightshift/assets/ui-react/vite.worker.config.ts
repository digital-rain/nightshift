import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'

// Worker UI build — sibling of vite.config.ts.
//
// Same side-by-side strategy: output to ./dist-worker, leaving the legacy
// assets/ui-worker vanilla UI in place. Dev server proxies /api to the worker
// UI backend (default port 8810). The worker UI reuses shared brand assets that
// the Python side mounts at /shared from the operator UI dir; in dev those are
// served from worker/public/shared so the same `/shared/...` URLs resolve.
export default defineConfig({
  root: resolve(__dirname, 'worker'),
  base: './',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: resolve(__dirname, 'dist-worker'),
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    host: true,
    port: 5273,
    proxy: {
      // 127.0.0.1, not localhost — the worker UI binds 0.0.0.0 (IPv4) but
      // localhost resolves to ::1 (IPv6) first on many hosts → ECONNREFUSED.
      '/api': { target: 'http://127.0.0.1:8810', changeOrigin: true },
    },
  },
})
