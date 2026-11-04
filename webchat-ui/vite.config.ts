import fs from "node:fs"
import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vitest/config"

// Two apps are built from one set of components, each written next to the Python code that serves it and
// committed, so running Jarvis never needs Node:
//   vite build                 the web chat   -> src/jarvis/webchat/static   (index.html)
//   vite build --mode phone    the phone app  -> src/jarvis/remote/static    (phone.html, served as index.html)
// `npm run dev` proxies the API to a running Jarvis (web_chat_port, or remote_access_port in phone mode).
const root = import.meta.dirname

// The phone page is built from phone.html and served as the app's index.
function phoneIndex(): Plugin {
  return {
    name: "jarvis-phone-index",
    apply: "build",
    closeBundle() {
      const out = path.resolve(root, "../src/jarvis/remote/static")
      const built = path.join(out, "phone.html")
      if (fs.existsSync(built)) fs.renameSync(built, path.join(out, "index.html"))
    },
  }
}

export default defineConfig(({ mode }) => {
  const phone = mode === "phone"
  const jarvis = process.env.JARVIS_WEB_CHAT_URL ?? (phone ? "http://127.0.0.1:8765" : "http://127.0.0.1:8766")
  return {
    plugins: [react(), tailwindcss(), ...(phone ? [phoneIndex()] : [])],
    resolve: { alias: { "@": path.resolve(root, "src") } },
    publicDir: phone ? path.resolve(root, "public-phone") : false,
    build: {
      outDir: path.resolve(root, phone ? "../src/jarvis/remote/static" : "../src/jarvis/webchat/static"),
      emptyOutDir: true,
      sourcemap: false,
      ...(phone ? { rollupOptions: { input: path.resolve(root, "phone.html") } } : {}),
    },
    server: { proxy: { "/api": { target: jarvis, changeOrigin: false } } },
    test: { environment: "node", setupFiles: ["src/test/setup.ts"] },
  }
})
