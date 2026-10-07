import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import "./index.css"
import { applyTheme, readStoredTheme } from "./theme"

// Before the first paint, so the page never flashes the other theme.
applyTheme(readStoredTheme())

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
