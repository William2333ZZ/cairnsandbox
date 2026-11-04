import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The control plane serves web/dist at /ui/. In dev, API calls go to a locally forwarded control plane.
export default defineConfig({
  plugins: [react()],
  base: '/ui/',
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    proxy: { '^/(sandboxes|nodes|health|eval|intake|routes|store|events)(/|$)': { target: process.env.CP_URL || 'http://127.0.0.1:18700', changeOrigin: true } },
  },
})
