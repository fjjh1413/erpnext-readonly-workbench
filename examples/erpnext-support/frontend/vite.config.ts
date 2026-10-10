import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    proxy: { '/api': { target: 'http://127.0.0.1:8875', changeOrigin: false } },
  },
  build: { sourcemap: false },
  test: { environment: 'jsdom', include: ['src/**/*.test.js'], restoreMocks: true, pool: 'threads', maxWorkers: 1 },
})
