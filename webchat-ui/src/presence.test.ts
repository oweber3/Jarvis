import { describe, expect, it } from "vitest"
import { presence, type PresenceInput } from "@/presence"

// The header's presence line, as the desktop chat's contact header has it ("Online", "Typing…", "Offline").

const online: PresenceInput = { reachable: true, ready: true, state: "idle", busy: false, working: false }

describe("presence", () => {
  it("reads Online when Jarvis is ready and free", () => {
    expect(presence(online)).toEqual({ tone: "online", label: "Online" })
  })

  it("reads Offline when the page cannot reach Jarvis, whatever it last heard", () => {
    expect(presence({ ...online, reachable: false, working: true })).toEqual({ tone: "offline", label: "Offline" })
  })

  it("reads Starting until Jarvis is ready", () => {
    expect(presence({ ...online, ready: false })).toEqual({ tone: "waiting", label: "Starting" })
  })

  it("says what Jarvis is doing: thinking on a typed request, or what the voice side is doing", () => {
    expect(presence({ ...online, working: true }).label).toBe("Thinking")
    expect(presence({ ...online, state: "thinking" }).label).toBe("Thinking")
    expect(presence({ ...online, state: "speaking" }).label).toBe("Speaking")
    expect(presence({ ...online, state: "listening" }).label).toBe("Listening")
    expect(presence({ ...online, state: "dictating" }).label).toBe("Taking dictation")
    expect(presence({ ...online, busy: true }).label).toBe("Busy")
    for (const state of ["thinking", "speaking", "listening", "dictating"]) {
      expect(presence({ ...online, state }).tone).toBe("working")
    }
  })
})
