import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'

// Manager / operator UI build.
//
// Built SIDE-BY-SIDE with the existing hand-written UI. Output lands in
// ./dist-manager (NOT over assets/ui) so the legacy vanilla UI keeps working
// untouched while this new React surface is brought to parity. Because the
// backend is already a clean `/api/*` JSON + SSE layer served via StaticFiles,
// React is simply a second static surface on the same API — no Python changes
// are required to develop it, and switching the mount is a one-line change when
// you decide to cut over.
//
// Relative `base` keeps asset URLs working under whatever path it is mounted at.
export default defineConfig({
  root: resolve(__dirname, 'manager'),
  base: './',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: resolve(__dirname, 'dist-manager'),
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8800', changeOrigin: true },
    },
  },
})
