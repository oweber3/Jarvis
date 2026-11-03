import { MoonIcon, SunIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { applyTheme, nextTheme, readStoredTheme, storeTheme, type Theme } from "@/theme"

// Light or dark, remembered in the browser. Dark is the Jarvis look.
export const ThemeSwitch: FC = () => {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme())
  useEffect(() => {
    applyTheme(theme)
    storeTheme(theme)
  }, [theme])
  const light = theme === "light"
  return (
    <button
      type="button"
      role="switch"
      aria-checked={light}
      aria-label="Light mode"
      title={light ? "Switch to dark mode" : "Switch to light mode"}
      onClick={() => setTheme(nextTheme(theme))}
      className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-8 shrink-0 items-center justify-center rounded-md"
    >
      {light ? <SunIcon className="size-4" /> : <MoonIcon className="size-4" />}
    </button>
  )
}
