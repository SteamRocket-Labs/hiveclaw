import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'

// Read version from local VERSION file first, fallback to root VERSION
let majorVersion = '0.0.0'
for (const candidate of ['./VERSION', '../VERSION']) {
  try {
    majorVersion = fs.readFileSync(path.resolve(__dirname, candidate), 'utf-8').trim()
    break
  } catch {
    // try next candidate
  }
}
const now = new Date()
const buildTimestamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}.${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`
const version = `${majorVersion}+${buildTimestamp}`
const devBackendUrl = process.env.HIVE_DEV_BACKEND_URL || 'http://localhost:8008'
const devBackendWsUrl = devBackendUrl.replace(/^http/, 'ws')

export default defineConfig({
    plugins: [react()],
    define: {
        __APP_VERSION__: JSON.stringify(version),
    },
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    server: {
        port: 3008,
        host: '0.0.0.0',
        proxy: {
            '/api': {
                target: devBackendUrl,
                changeOrigin: true,
            },
            '/ws': {
                target: devBackendWsUrl,
                ws: true,
            },
        },
    },
    build: {
        rollupOptions: {
            output: {
                manualChunks(id) {
                    if (!id.includes('node_modules')) return undefined
                    if (id.includes('recharts') || id.includes('d3-')) return 'vendor-charts'
                    if (id.includes('@tabler')) return 'vendor-icons'
                    return 'vendor'
                },
            },
        },
    },
    test: {
        exclude: ['node_modules/**', 'dist/**', 'e2e/**', 'test-results/**'],
    },
})
