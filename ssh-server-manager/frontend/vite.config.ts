import { defineConfig } from 'vite';

export default defineConfig({
  base: '/assets/',
  build: {
    outDir: '../scripts/ssh_server_manager/assets/ui', emptyOutDir: true,
    assetsDir: '', manifest: 'asset-manifest.json', sourcemap: false,
  },
});
