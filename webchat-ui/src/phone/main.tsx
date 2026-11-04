import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "../index.css"
import "./phone.css"
import { applyTheme, readStoredTheme } from "@/theme"
import { prepareFormats } from "@/time"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { PhoneApp } from "@/phone/PhoneApp"

// Before the first paint, so the page never flashes the other theme.
applyTheme(readStoredTheme())

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <PhoneApp />
    </ErrorBoundary>
  </StrictMode>,
)

// The date formatters load locale data the first time they are built: do it after the first paint, while the page
// waits for Jarvis's first answer.
requestAnimationFrame(() => {
  if ("requestIdleCallback" in window) window.requestIdleCallback(() => prepareFormats(), { timeout: 500 })
  else setTimeout(() => prepareFormats(), 0)
})
