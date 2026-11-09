import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import {fileURLToPath} from 'node:url';
export default defineConfig({
  plugins:[react()],
  build:{outDir:'../cleaner/static',emptyOutDir:true,
    rollupOptions:{input:fileURLToPath(new URL('./cleaner.html',import.meta.url))}},
});
