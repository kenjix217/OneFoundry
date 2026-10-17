import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export function localApiTarget(value: string | undefined): string {
  const target = new URL(value ?? 'http://127.0.0.1:8765');
  if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname)
    || !target.port || target.username || target.password || target.pathname !== '/'
    || target.search || target.hash) {
    throw new Error('ONEFOUNDRY_API_PROXY_TARGET must be an HTTP loopback origin with an explicit port.');
  }
  return target.origin;
}

const apiTarget = localApiTarget(process.env.ONEFOUNDRY_API_PROXY_TARGET);

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: { '/api': {
      target: apiTarget,
      changeOrigin: true,
      configure(proxy) {
        proxy.on('proxyReq', (proxyRequest, request) => {
          const host = request.headers.host;
          if (host && /^(?:127\.0\.0\.1|localhost):[0-9]+$/.test(host)
            && request.headers.origin === `http://${host}`) {
            proxyRequest.setHeader('Origin', apiTarget);
          }
        });
      },
    } },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    css: true,
    restoreMocks: true,
  },
});
