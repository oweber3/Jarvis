import { describe, expect, it } from "vitest"
import { nextTheme, readStoredTheme, storeTheme, type ThemeStorage } from "@/theme"

const memory = (initial: Record<string, string> = {}): ThemeStorage & { data: Record<string, string> } => {
  const data = { ...initial }
  return { data, getItem: (key) => data[key] ?? null, setItem: (key, value) => void (data[key] = value) }
}

const blocked: ThemeStorage = {
  getItem: () => {
    throw new Error("blocked")
  },
  setItem: () => {
    throw new Error("blocked")
  },
}

describe("theme", () => {
  it("is dark, the Jarvis look, until the owner chooses", () => {
    expect(readStoredTheme(memory())).toBe("dark")
    expect(readStoredTheme(undefined)).toBe("dark")
  })

  it("remembers the choice", () => {
    const storage = memory()
    storeTheme("light", storage)
    expect(readStoredTheme(storage)).toBe("light")
    storeTheme("dark", storage)
    expect(readStoredTheme(storage)).toBe("dark")
  })

  it("ignores a stored value it does not know", () => {
    expect(readStoredTheme(memory({ "jarvis-webchat-theme": "sepia" }))).toBe("dark")
  })

  it("still works when the browser blocks storage", () => {
    expect(readStoredTheme(blocked)).toBe("dark")
    expect(() => storeTheme("light", blocked)).not.toThrow()
  })

  it("switches to the other one", () => {
    expect(nextTheme("dark")).toBe("light")
    expect(nextTheme("light")).toBe("dark")
  })
})
