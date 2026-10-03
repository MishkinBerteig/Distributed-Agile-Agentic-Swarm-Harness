import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, proxy /api -> coordinator so the dashboard works without CORS config.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 7173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
