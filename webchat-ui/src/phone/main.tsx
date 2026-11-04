import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "../index.css"
import "./phone.css"
import { applyTheme, readStoredTheme } from "@/theme"
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
