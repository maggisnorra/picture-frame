import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: { 
    //target: 'chrome115',
    outDir: "../../backend/static",
    emptyOutDir: true,
  }
})
