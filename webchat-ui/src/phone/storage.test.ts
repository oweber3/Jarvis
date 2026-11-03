import { describe, expect, it } from "vitest"
import { DEFAULT_PREFS, readPrefs, readToken, storePrefs, storeToken, type PhoneStorage } from "@/phone/storage"

function memory(initial: Record<string, string> = {}): PhoneStorage & { data: Record<string, string> } {
  const data = { ...initial }
  return {
    data,
    getItem: (key) => data[key] ?? null,
    setItem: (key, value) => void (data[key] = value),
    removeItem: (key) => void delete data[key],
  }
}

const blocked: PhoneStorage = {
  getItem: () => {
    throw new Error("blocked")
  },
  setItem: () => {
    throw new Error("blocked")
  },
  removeItem: () => {
    throw new Error("blocked")
  },
}

describe("the device token", () => {
  it("is stored and read back, and forgotten", () => {
    const storage = memory()
    storeToken("abc", storage)
    expect(readToken(storage)).toBe("abc")
    storeToken(null, storage)
    expect(readToken(storage)).toBeNull()
  })

  it("reads as none when nothing is stored or it is blank", () => {
    expect(readToken(memory())).toBeNull()
    expect(readToken(memory({ "jarvis-phone-token": "   " }))).toBeNull()
  })

  it("survives storage that throws", () => {
    expect(readToken(blocked)).toBeNull()
    expect(() => storeToken("abc", blocked)).not.toThrow()
    expect(() => storeToken(null, blocked)).not.toThrow()
  })
})

describe("the switch", () => {
  it("are off by default", () => {
    expect(readPrefs(memory())).toEqual(DEFAULT_PREFS)
    expect(DEFAULT_PREFS).toEqual({ speakReplies: false })
  })

  it("are stored and read back", () => {
    const storage = memory()
    storePrefs({ speakReplies: true }, storage)
    expect(readPrefs(storage)).toEqual({ speakReplies: true })
  })

  it("read as off when the stored text is damaged or odd", () => {
    expect(readPrefs(memory({ "jarvis-phone-prefs": "{nope" }))).toEqual(DEFAULT_PREFS)
    expect(readPrefs(memory({ "jarvis-phone-prefs": '{"speakReplies":"yes"}' }))).toEqual(DEFAULT_PREFS)
    expect(readPrefs(memory({ "jarvis-phone-prefs": "5" }))).toEqual(DEFAULT_PREFS)
  })

  it("survive storage that throws", () => {
    expect(readPrefs(blocked)).toEqual(DEFAULT_PREFS)
    expect(() => storePrefs(DEFAULT_PREFS, blocked)).not.toThrow()
  })
})
