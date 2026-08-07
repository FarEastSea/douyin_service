import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/static/app/',
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
