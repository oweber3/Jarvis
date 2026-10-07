// Dark is the Jarvis look; light is the same palette's light variant. Both come from the desktop theme
// (generated/theme.css). The choice is remembered in the browser's storage when it allows.

export type Theme = "dark" | "light"
export type ThemeStorage = Pick<Storage, "getItem" | "setItem">

const KEY = "jarvis-webchat-theme"

export function readStoredTheme(storage: ThemeStorage | undefined = browserStorage()): Theme {
  try {
    const value = storage?.getItem(KEY)
    return value === "light" ? "light" : "dark"
  } catch {
    return "dark"
  }
}

export function storeTheme(theme: Theme, storage: ThemeStorage | undefined = browserStorage()): void {
  try {
    storage?.setItem(KEY, theme)
  } catch {
    /* storage can be blocked; the choice then lasts until the page closes */
  }
}

export function nextTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark"
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset["theme"] = theme
}

function browserStorage(): ThemeStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage
  } catch {
    return undefined
  }
}
