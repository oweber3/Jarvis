// What the phone app remembers in the browser's storage, when it allows: the device token and one switch.
// Storage can be blocked or empty (a private window, cleared site data), so every read and write is guarded and
// the page works without it (it then asks to pair again).

export type PhoneStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">

const TOKEN_KEY = "jarvis-phone-token"
const PREFS_KEY = "jarvis-phone-prefs"

export type PhonePrefs = {
  /** Speak the replies to typed requests (the PC's voice, played on this phone). */
  speakReplies: boolean
}

export const DEFAULT_PREFS: PhonePrefs = { speakReplies: false }

function browserStorage(): PhoneStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage
  } catch {
    return undefined
  }
}

export function readToken(storage: PhoneStorage | undefined = browserStorage()): string | null {
  try {
    const value = storage?.getItem(TOKEN_KEY)
    return value && value.trim() ? value : null
  } catch {
    return null
  }
}

export function storeToken(token: string | null, storage: PhoneStorage | undefined = browserStorage()): void {
  try {
    if (token) storage?.setItem(TOKEN_KEY, token)
    else storage?.removeItem(TOKEN_KEY)
  } catch {
    /* the token then lasts until the page closes */
  }
}

export function readPrefs(storage: PhoneStorage | undefined = browserStorage()): PhonePrefs {
  try {
    const parsed: unknown = JSON.parse(storage?.getItem(PREFS_KEY) ?? "null")
    if (parsed && typeof parsed === "object") {
      const found = parsed as Partial<Record<keyof PhonePrefs, unknown>>
      return { speakReplies: found.speakReplies === true }
    }
  } catch {
    /* unreadable settings read as the defaults */
  }
  return { ...DEFAULT_PREFS }
}

export function storePrefs(prefs: PhonePrefs, storage: PhoneStorage | undefined = browserStorage()): void {
  try {
    storage?.setItem(PREFS_KEY, JSON.stringify(prefs))
  } catch {
    /* the switches then last until the page closes */
  }
}
