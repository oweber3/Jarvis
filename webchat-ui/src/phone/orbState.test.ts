import { describe, expect, it } from "vitest"
import { KEYBOARD_MIN_HEIGHT, keyboardOpen, orbStateFor, type OrbInput } from "@/phone/orbState"

const base: OrbInput = { reachable: true, ready: true, pcState: "idle", phoneListening: false, phoneSpeaking: false }
const view = (over: Partial<OrbInput>) => orbStateFor({ ...base, ...over })

describe("orbStateFor", () => {
  it("maps the PC's assistant states like the desktop orb", () => {
    const expected: Record<string, string> = {
      asleep: "offline",
      idle: "idle",
      listening: "listening",
      thinking: "thinking",
      speaking: "speaking",
      dictating: "dictating",
      dictation_processing: "thinking",
    }
    for (const [pc, orb] of Object.entries(expected)) {
      expect(view({ pcState: pc })).toEqual({ state: orb, label: null })
    }
  })

  it("reads an unknown PC state as offline", () => {
    expect(view({ pcState: "mystery" })).toEqual({ state: "offline", label: null })
    expect(view({ pcState: "" }).state).toBe("offline")
  })

  it("says it cannot reach the PC whatever else is going on", () => {
    const lost = { state: "offline", label: "can't reach the PC" }
    expect(view({ reachable: false })).toEqual(lost)
    expect(view({ reachable: false, phoneListening: true, phoneSpeaking: true, pcState: "speaking" })).toEqual(lost)
  })

  it("shows the phone's own listening and speaking before the PC's state", () => {
    expect(view({ phoneListening: true, pcState: "thinking" })).toEqual({ state: "listening", label: null })
    expect(view({ phoneSpeaking: true, pcState: "idle" })).toEqual({ state: "speaking", label: null })
    expect(view({ phoneListening: true, phoneSpeaking: true }).state).toBe("listening")
  })

  it("says starting up while the PC is not ready", () => {
    expect(view({ ready: false })).toEqual({ state: "offline", label: "starting up" })
    expect(view({ ready: false, pcState: "listening" })).toEqual({ state: "offline", label: "starting up" })
  })

  it("lets the phone's voice show while the PC is still starting up", () => {
    expect(view({ ready: false, phoneListening: true }).state).toBe("listening")
  })
})

describe("keyboardOpen", () => {
  it("is open when the visual viewport is at least 150 px shorter", () => {
    expect(keyboardOpen(800, 500)).toBe(true)
    expect(keyboardOpen(800, 800 - KEYBOARD_MIN_HEIGHT)).toBe(true)
  })

  it("is closed for small differences such as a collapsing address bar", () => {
    expect(keyboardOpen(800, 800)).toBe(false)
    expect(keyboardOpen(800, 800 - KEYBOARD_MIN_HEIGHT + 1)).toBe(false)
    expect(keyboardOpen(800, 900)).toBe(false)
  })
})
