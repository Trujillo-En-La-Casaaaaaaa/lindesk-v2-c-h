import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The SPA is served statically (see Dockerfile); runtime configuration is the
// single gateway base URL in `VITE_GATEWAY_BASE_URL` (contracts.md §11).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
  },
  preview: {
    port: 4173,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
});
