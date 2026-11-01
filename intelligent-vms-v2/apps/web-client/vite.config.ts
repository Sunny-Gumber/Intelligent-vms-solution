import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// The dev server proxies /api to the control plane so the browser sees a
// single origin. In production the built assets are served by the same
// reverse proxy that terminates TLS (see deploy/ubuntu/nginx-vms.conf).
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: process.env.VMS_API ?? 'http://127.0.0.1:8080', changeOrigin: true },
      '/events': { target: process.env.VMS_API ?? 'http://127.0.0.1:8080', ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true, target: 'es2022' },
});
