import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发时把 /api（含 SSE 推送）代理到本机后端；生产构建由后端同源提供 dist/index.html 与 /assets/*。
// 后端 CSP 为 script-src 'self'，因此构建产物不能包含内联脚本或外部 CDN。
export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8710',
        changeOrigin: false,
        ws: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 2500,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd', '@ant-design/icons', 'dayjs'],
          charts: ['recharts'],
        },
      },
    },
  },
});
