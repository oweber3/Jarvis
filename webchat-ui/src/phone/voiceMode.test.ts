import { describe, expect, it } from "vitest"
import type { PhoneSnapshot, VoiceAudio } from "@/api"
import { pendingConfirmation } from "@/phone/confirmation"
import {
  inlineVoiceStatus,
  latestExchange,
  leaveWhenHidden,
  VoiceMode,
  voiceModeCaption,
  type ExchangeMessage,
} from "@/phone/voiceMode"
import { VoiceSession, voiceMessage, type MicPort, type VoicePorts, type VoiceView } from "@/phone/voiceSession"

const tick = async () => {
  for (let i = 0; i < 8; i++) await new Promise<void>((resolve) => setTimeout(resolve, 0))
}

const QUIET = 0.0004
const SPEECH = 0.1

class FakeMic implements MicPort {
  rate = 16000
  onChunk: ((chunk: Float32Array) => void) | null = null
  closed = 0
  close(): void {
    this.closed++
  }
  feed(seconds: number, amplitude: number): void {
    const blocks = Math.round((seconds * this.rate) / 2048)
    for (let i = 0; i < blocks; i++) this.onChunk?.(new Float32Array(2048).fill(amplitude))
  }
}

// A real voice session on fake browser and network ports, and voice mode around it.
function world() {
  const mic = new FakeMic()
  const state = { opens: 0, played: 0, stopped: 0, unlocked: 0, playing: null as null | (() => void) }
  const ports: VoicePorts = {
    unlock: async () => void state.unlocked++,
    openMic: async () => {
      state.opens++
      return mic
    },
    availability: () => ({ available: true, reason: null }),
    sendAudio: async () => ({ status: "accepted", text: "what time is it", query_id: 7 }),
    stopQuery: async () => undefined,
    begin: () => undefined,
    accepted: () => undefined,
    failed: () => undefined,
    fetchAudio: async () => new ArrayBuffer(8),
    play: () =>
      new Promise<void>((resolve) => {
        state.played++
        state.playing = resolve
      }),
    stopPlayback: () => {
      state.stopped++
      state.playing?.()
      state.playing = null
    },
    playerLevel: () => 0.5,
    sleep: (ms) => (ms < 1000 ? Promise.resolve() : new Promise<void>(() => undefined)),
  }
  const session = new VoiceSession(ports)
  const shown: boolean[] = []
  const mode = new VoiceMode(session, (open) => shown.push(open))
  return { mic, state, session, mode, shown }
}

type World = ReturnType<typeof world>

const ready: Record<string, VoiceAudio> = { "7": { status: "ready", url: "/api/audio/abc" } }

// Speech, then silence long enough to end the request.
async function say(w: World) {
  await tick()
  w.mic.feed(0.4, QUIET)
  w.mic.feed(0.6, SPEECH)
  w.mic.feed(1.5, QUIET)
  await tick()
}

// The reply arrives and plays to its end.
async function hearTheReply(w: World) {
  w.session.audioChanged(ready)
  await tick()
  w.state.playing?.()
  await tick()
}

describe("opening voice mode", () => {
  it("shows voice mode and starts listening in the same tap, with audio unlocked inside it", () => {
    const w = world()
    w.mode.enter()
    expect(w.mode.open).toBe(true)
    expect(w.shown).toEqual([true])
    expect(w.session.view.phase).toBe("listening")
    expect(w.state.unlocked).toBe(1)
  })

  it("turns conversation mode on", () => {
    const w = world()
    w.mode.enter()
    expect(w.session.view.conversation).toBe(true)
  })

  it("listens again by itself after Jarvis has spoken", async () => {
    const w = world()
    w.mode.enter()
    await say(w)
    await hearTheReply(w)
    expect(w.session.view.phase).toBe("listening")
    expect(w.state.opens).toBe(1)
  })

  it("does not cut off a reply that is already being read aloud", async () => {
    const w = world()
    void w.session.speakReply(7)
    w.session.audioChanged(ready)
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    w.mode.enter()
    expect(w.session.view.phase).toBe("speaking")
    expect(w.state.stopped).toBe(0)
  })

  it("keeps conversation mode off when it is turned off inside voice mode", async () => {
    const w = world()
    w.mode.enter()
    w.session.setConversation(false)
    await say(w)
    await hearTheReply(w)
    expect(w.session.view.phase).toBe("idle")
    expect(w.mic.closed).toBe(1)
  })
})

describe("closing voice mode", () => {
  it("stops recording and releases the microphone without sending anything", async () => {
    const w = world()
    w.mode.enter()
    await tick()
    w.mic.feed(0.4, SPEECH)
    w.mode.leave()
    await tick()
    expect(w.mode.open).toBe(false)
    expect(w.shown).toEqual([true, false])
    expect(w.session.view.phase).toBe("idle")
    expect(w.mic.closed).toBe(1)
    expect(w.mic.onChunk).toBeNull()
  })

  it("stops Jarvis speaking and releases the microphone held for the next turn", async () => {
    const w = world()
    w.mode.enter()
    await say(w)
    w.session.audioChanged(ready)
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    expect(w.mic.closed).toBe(0)
    w.mode.leave()
    expect(w.state.stopped).toBeGreaterThan(0)
    expect(w.mic.closed).toBe(1)
    await tick()
    expect(w.session.view.phase).toBe("idle")
  })

  it("turns conversation mode off, so the message box's microphone makes one request only", async () => {
    const w = world()
    w.mode.enter()
    w.mode.leave()
    expect(w.session.view.conversation).toBe(false)
    w.session.tap()
    await say(w)
    await hearTheReply(w)
    expect(w.session.view.phase).toBe("idle")
  })

  it("lets go of everything when the page is hidden or left, even outside voice mode", async () => {
    const page = new EventTarget() as EventTarget & { visibilityState: string }
    const win = new EventTarget()
    const w = world()
    const unbind = leaveWhenHidden(page, win, () => w.mode.leave())

    w.mode.enter()
    await tick()
    page.visibilityState = "hidden"
    page.dispatchEvent(new Event("visibilitychange"))
    expect(w.mode.open).toBe(false)
    expect(w.mic.closed).toBe(1)

    page.visibilityState = "visible"
    w.mode.enter()
    await tick()
    win.dispatchEvent(new Event("pagehide"))
    expect(w.mode.open).toBe(false)
    expect(w.mic.closed).toBe(2)
    expect(w.session.view.phase).toBe("idle")

    unbind()
    w.mode.enter()
    win.dispatchEvent(new Event("pagehide"))
    expect(w.mode.open).toBe(true)
  })
})

describe("what voice mode says under the orb", () => {
  const view = (phase: VoiceView["phase"], extra: Partial<VoiceView> = {}): VoiceView => ({
    phase,
    message: null,
    conversation: true,
    ...extra,
  })

  it("says what the owner can do in each phase", () => {
    const captions = (["idle", "listening", "sending", "waiting", "speaking"] as const).map((p) => voiceModeCaption(view(p)))
    expect(new Set(captions).size).toBe(captions.length)
    for (const caption of captions) expect(caption.length).toBeGreaterThan(0)
  })

  it("puts a refusal in plain words first", () => {
    expect(voiceModeCaption(view("idle", { message: voiceMessage("busy") }))).toBe(voiceMessage("busy"))
  })
})

describe("the message box's small voice indicator", () => {
  it("is absent while nothing is happening", () => {
    expect(inlineVoiceStatus({ phase: "idle", message: null, conversation: false })).toBeNull()
  })

  it("shows recording and working, and a refusal as a warning", () => {
    expect(inlineVoiceStatus({ phase: "listening", message: null, conversation: false })).toMatchObject({ tone: "live" })
    expect(inlineVoiceStatus({ phase: "waiting", message: null, conversation: false })).toMatchObject({ tone: "working" })
    expect(inlineVoiceStatus({ phase: "idle", message: voiceMessage("denied"), conversation: false })).toEqual({
      text: voiceMessage("denied"),
      tone: "warn",
    })
  })
})

describe("the latest exchange under the orb", () => {
  const m = (role: ExchangeMessage["role"], text: string, notice = false): ExchangeMessage => ({ role, text, notice })

  it("is the last thing the owner said and Jarvis's answer to it", () => {
    expect(latestExchange([m("user", "hello"), m("assistant", "hi"), m("user", "what time is it"), m("assistant", "Ten past three.")])).toEqual({
      said: "what time is it",
      reply: "Ten past three.",
    })
  })

  it("has no reply yet while Jarvis works on it", () => {
    expect(latestExchange([m("user", "hello"), m("assistant", "hi"), m("user", "and now")])).toEqual({ said: "and now", reply: null })
  })

  it("leaves out local notices and empty parts", () => {
    expect(latestExchange([m("user", "hello"), m("assistant", "Jarvis is busy.", true), m("assistant", "  ")])).toEqual({
      said: "hello",
      reply: null,
    })
  })

  it("is empty for an empty chat", () => {
    expect(latestExchange([])).toEqual({ said: null, reply: null })
  })
})

describe("a pending confirmation", () => {
  const confirmation = { id: "c1", tool: "localFiles", action: "Delete", target: "notes.txt", consequence: "It cannot be undone.", expires_in: 40 }
  const phone = (allow: boolean, pending = true) =>
    ({
      confirmation: pending ? confirmation : null,
      pc: null,
      quick_actions: [],
      allow_confirm: allow,
      voice: { available: true, reason: null, audio: {} },
    }) as PhoneSnapshot

  it("is shown when the PC allows phones to answer", () => {
    expect(pendingConfirmation(phone(true))).toEqual(confirmation)
  })

  it("is not shown when answering from a phone is turned off, or nothing waits", () => {
    expect(pendingConfirmation(phone(false))).toBeNull()
    expect(pendingConfirmation(phone(true, false))).toBeNull()
    expect(pendingConfirmation(null)).toBeNull()
  })
})
