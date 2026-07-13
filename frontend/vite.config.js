import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    // Must match the real directory name exactly. macOS is case-insensitive so
    // '../backend/static' worked locally, but on Linux (Docker/Cloud Run) it
    // silently writes to a different folder than FastAPI serves from.
    outDir: '../Backend/static',
    emptyOutDir: true,
  },
})
