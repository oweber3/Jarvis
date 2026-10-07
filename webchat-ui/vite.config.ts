import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// The build is written next to the Python code that serves it and is committed, so running Jarvis
// never needs Node. `npm run dev` proxies the API to a running Jarvis (port: web_chat_port).
const jarvis = process.env.JARVIS_WEB_CHAT_URL ?? "http://127.0.0.1:8766"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  build: {
    outDir: path.resolve(import.meta.dirname, "../src/jarvis/webchat/static"),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: { proxy: { "/api": { target: jarvis, changeOrigin: false } } },
  test: { environment: "node" },
})
