import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import "./index.css"
import { applyTheme, readStoredTheme } from "./theme"
import { prepareFormats } from "./time"

// Before the first paint, so the page never flashes the other theme.
applyTheme(readStoredTheme())

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// The date formatters load locale data the first time they are built: do it after the first paint, while the page
// waits for Jarvis's first answer.
requestAnimationFrame(() => {
  if ("requestIdleCallback" in window) window.requestIdleCallback(() => prepareFormats(), { timeout: 500 })
  else setTimeout(() => prepareFormats(), 0)
})
