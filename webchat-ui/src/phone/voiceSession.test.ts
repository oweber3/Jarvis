import { describe, expect, it } from "vitest"
import { ApiError, type VoiceAudio } from "@/api"
import { MicError } from "@/phone/micCapture"
import {
  MIN_RECORDING_BYTES,
  VoiceSession,
  voiceMessage,
  type MicPort,
  type SpokenReply,
  type VoicePorts,
  type VoiceView,
} from "@/phone/voiceSession"

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
  // Feeds `seconds` of a constant-level signal in 2048-sample blocks, as the worklet does.
  feed(seconds: number, amplitude: number): void {
    const blocks = Math.round((seconds * this.rate) / 2048)
    for (let i = 0; i < blocks; i++) this.onChunk?.(new Float32Array(2048).fill(amplitude))
  }
}

type World = ReturnType<typeof world>

function world(options: { reply?: SpokenReply; sendError?: unknown; openError?: unknown; available?: { available: boolean; reason: string | null } } = {}) {
  const mic = new FakeMic()
  const log: string[] = []
  const sent: Uint8Array[] = []
  const views: VoiceView[] = []
  const state = {
    opens: 0,
    played: 0,
    stopped: 0,
    stoppedQueries: 0,
    aborted: false,
    playing: null as null | (() => void),
    playerLevel: 0.6 as number | null,
    release: null as null | (() => void),
  }
  const available = options.available ?? { available: true, reason: null }
  const reply: SpokenReply = options.reply ?? { status: "accepted", text: "what time is it", query_id: 7 }
  const ports: VoicePorts = {
    unlock: async () => void log.push("unlock"),
    openMic: async () => {
      state.opens++
      if (options.openError) throw options.openError
      return mic
    },
    availability: () => available,
    sendAudio: async (pcm, signal) => {
      sent.push(pcm)
      signal.addEventListener("abort", () => (state.aborted = true))
      if (options.sendError) throw options.sendError
      if (state.release) await new Promise<void>((resolve) => (state.release = resolve))
      return reply
    },
    stopQuery: async () => void state.stoppedQueries++,
    begin: (text) => void log.push(`begin:${text}`),
    accepted: () => void log.push("accepted"),
    failed: () => void log.push("failed"),
    fetchAudio: async (url) => {
      log.push(`fetch:${url}`)
      return new ArrayBuffer(8)
    },
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
    playerLevel: () => state.playerLevel,
    // A short pause passes at once; the long wait for a reply never ends on its own in a test.
    sleep: (ms) => (ms < 1000 ? Promise.resolve() : new Promise<void>(() => undefined)),
  }
  const session = new VoiceSession(ports, (view) => views.push(view))
  return { session, mic, log, sent, views, state, ports }
}

// A full request: a quiet moment, some speech, then enough silence to end it.
async function speakARequest(w: World) {
  w.session.tap()
  await tick()
  w.mic.feed(0.4, QUIET)
  w.mic.feed(0.6, SPEECH)
  w.mic.feed(1.5, QUIET)
  await tick()
}

const ready = (url = "/api/audio/abc"): Record<string, VoiceAudio> => ({ "7": { status: "ready", url } })

describe("a spoken exchange", () => {
  it("records, ends on silence, sends, shows the transcript, plays the reply and goes idle", async () => {
    const w = world()
    await speakARequest(w)
    expect(w.session.view.phase).toBe("waiting")
    expect(w.sent).toHaveLength(1)
    expect(w.sent[0]!.length).toBeGreaterThan(16000 * 2)
    expect(w.log).toEqual(["unlock", "begin:what time is it", "accepted"])
    expect(w.mic.closed).toBe(1)
    w.session.audioChanged({ "7": { status: "pending" } })
    await tick()
    expect(w.state.played).toBe(0)
    w.session.audioChanged(ready())
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    expect(w.log).toContain("fetch:/api/audio/abc")
    w.state.playing?.()
    await tick()
    expect(w.session.view.phase).toBe("idle")
    expect(w.session.view.message).toBeNull()
  })

  it("unlocks audio inside the tap, before the microphone is even open", async () => {
    const w = world()
    w.session.tap()
    expect(w.log).toEqual(["unlock"])
    expect(w.session.view.phase).toBe("listening")
  })

  it("sends what was recorded when the owner taps to stop", async () => {
    const w = world()
    w.session.tap()
    await tick()
    w.mic.feed(0.5, SPEECH)
    w.session.tap()
    await tick()
    expect(w.sent).toHaveLength(1)
    expect(w.session.view.phase).toBe("waiting")
  })

  it("does not send a recording shorter than 0.3 s", async () => {
    const w = world()
    w.session.tap()
    await tick()
    w.mic.feed(0.1, SPEECH)
    w.session.tap()
    await tick()
    expect(w.sent).toHaveLength(0)
    expect(w.session.view).toMatchObject({ phase: "idle", message: voiceMessage("too_short") })
    expect(w.mic.closed).toBe(1)
  })

  it("gives up on a room with nothing said, sending nothing and releasing the microphone", async () => {
    const w = world()
    w.session.tap()
    await tick()
    w.mic.feed(9, QUIET)
    await tick()
    expect(w.sent).toHaveLength(0)
    expect(w.session.view).toMatchObject({ phase: "idle", message: voiceMessage("no_speech") })
    expect(w.mic.closed).toBe(1)
  })

  it("says so when the PC heard nothing in what was sent", async () => {
    const w = world({ reply: { status: "nothing_heard" } })
    await speakARequest(w)
    expect(w.session.view).toMatchObject({ phase: "idle", message: voiceMessage("nothing_heard") })
    expect(w.log).not.toContain("accepted")
  })

  it("says nothing is coming when the reply has no audio", async () => {
    const w = world()
    await speakARequest(w)
    w.session.audioChanged({ "7": { status: "none" } })
    await tick()
    expect(w.state.played).toBe(0)
    expect(w.session.view.phase).toBe("idle")
  })

  it("follows the microphone while listening and the reply while it plays", async () => {
    const w = world()
    w.session.tap()
    await tick()
    w.mic.feed(0.4, QUIET)
    w.mic.feed(0.2, SPEECH)
    expect(w.session.level()).toBeGreaterThan(0.5)
    w.mic.feed(1.5, QUIET)
    await tick()
    expect(w.session.level()).toBeNull()
    w.session.audioChanged(ready())
    await tick()
    expect(w.session.level()).toBe(0.6)
  })
})

describe("refusals are said in plain words", () => {
  it.each([
    [new ApiError(409, "busy"), "busy"],
    [new ApiError(503, "not_ready"), "not_ready"],
    [new ApiError(503, "disabled"), "disabled"],
    [new ApiError(503, "no_https"), "no_https"],
    [new ApiError(400, "too_short"), "too_short"],
  ])("a %s from the PC", async (error, code) => {
    const w = world({ sendError: error })
    await speakARequest(w)
    expect(w.session.view).toMatchObject({ phase: "idle", message: voiceMessage(code) })
    expect(w.log).toContain("failed")
    expect(w.mic.closed).toBe(1)
  })

  it.each(["denied", "missing", "insecure", "unsupported"] as const)("a microphone that is %s", async (reason) => {
    const w = world({ openError: new MicError(reason) })
    w.session.tap()
    await tick()
    expect(w.session.view).toMatchObject({ phase: "idle", message: voiceMessage(reason) })
  })

  it.each(["no_https", "disabled", "unavailable", "not_ready"])("voice that is unavailable (%s) never opens the microphone", async (reason) => {
    const w = world({ available: { available: false, reason } })
    w.session.tap()
    await tick()
    expect(w.state.opens).toBe(0)
    expect(w.session.view.message).toBe(voiceMessage(reason))
  })

  it("has a plain sentence for every code the PC can refuse with", () => {
    for (const code of ["busy", "not_ready", "disabled", "unavailable", "no_https", "too_short", "too_large", "denied", "missing"]) {
      expect(voiceMessage(code)).not.toBe(voiceMessage("anything-else"))
    }
  })
})

describe("interrupting", () => {
  it("a tap while sending cancels the upload and the request", async () => {
    const w = world()
    w.state.release = () => undefined
    await speakARequest(w)
    expect(w.session.view.phase).toBe("sending")
    w.session.tap()
    await tick()
    expect(w.state.aborted).toBe(true)
    expect(w.session.view.phase).toBe("idle")
    expect(w.log).not.toContain("accepted")
  })

  it("a tap while waiting for the reply stops the request on the PC", async () => {
    const w = world()
    await speakARequest(w)
    w.session.tap()
    await tick()
    expect(w.session.view.phase).toBe("idle")
    expect(w.state.stoppedQueries).toBe(1)
    w.session.audioChanged(ready())
    await tick()
    expect(w.state.played).toBe(0)
  })

  it("a tap while Jarvis speaks a one-off reply stops the playback (barge-in) and goes idle", async () => {
    const w = world()
    await speakARequest(w)
    w.session.audioChanged(ready())
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    w.session.tap()
    await tick()
    expect(w.state.stopped).toBeGreaterThan(0)
    expect(w.session.view.phase).toBe("idle")
  })

  it("in conversation mode, a tap while Jarvis speaks stops the playback and listens straight away", async () => {
    const w = world()
    w.session.setConversation(true)
    await speakARequest(w)
    w.session.audioChanged(ready())
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    w.session.tap()
    await tick()
    expect(w.state.stopped).toBeGreaterThan(0)
    expect(w.session.view.conversation).toBe(true)
    expect(w.session.view.phase).toBe("listening")
    // The barge-in starts one recording, not a second one when the stopped reply's playback ends.
    expect(w.state.opens).toBe(1)
    w.mic.feed(0.4, QUIET)
    w.mic.feed(0.6, SPEECH)
    w.mic.feed(1.5, QUIET)
    await tick()
    expect(w.sent).toHaveLength(2)
  })

  it("stop() lets go of the microphone and the audio and ignores a late answer", async () => {
    const w = world()
    w.state.release = () => undefined
    await speakARequest(w)
    w.session.stop()
    expect(w.mic.closed).toBeGreaterThanOrEqual(1)
    expect(w.state.stopped).toBeGreaterThan(0)
    w.state.release?.()
    await tick()
    expect(w.log).not.toContain("accepted")
    expect(w.session.view.phase).toBe("idle")
  })
})

describe("conversation mode", () => {
  it("listens again after Jarvis has finished speaking, keeping the microphone open between turns", async () => {
    const w = world()
    w.session.setConversation(true)
    await speakARequest(w)
    expect(w.mic.closed).toBe(0)
    w.session.audioChanged(ready())
    await tick()
    w.state.playing?.()
    await tick()
    expect(w.session.view.phase).toBe("listening")
    expect(w.state.opens).toBe(1)
  })

  it("ends when nothing is said", async () => {
    const w = world()
    w.session.setConversation(true)
    w.session.tap()
    await tick()
    w.mic.feed(9, QUIET)
    await tick()
    expect(w.session.view.conversation).toBe(false)
    expect(w.mic.closed).toBe(1)
  })

  it("ends on a refusal", async () => {
    const w = world({ sendError: new ApiError(409, "busy") })
    w.session.setConversation(true)
    await speakARequest(w)
    expect(w.session.view.conversation).toBe(false)
  })

  it("turning it off while idle releases the microphone", async () => {
    const w = world()
    w.session.setConversation(true)
    await speakARequest(w)
    w.session.audioChanged({ "7": { status: "none" } })
    await tick()
    w.session.setConversation(false)
    expect(w.mic.closed).toBeGreaterThanOrEqual(1)
  })
})

describe("a typed request whose reply is read aloud", () => {
  it("waits for the audio and plays it", async () => {
    const w = world()
    const speaking = w.session.speakReply(7)
    await tick()
    expect(w.session.view.phase).toBe("waiting")
    w.session.audioChanged(ready())
    await tick()
    expect(w.session.view.phase).toBe("speaking")
    w.state.playing?.()
    await speaking
    expect(w.session.view.phase).toBe("idle")
  })

  it("is left alone while the owner is already speaking", async () => {
    const w = world()
    w.session.tap()
    await tick()
    await w.session.speakReply(7)
    expect(w.session.view.phase).toBe("listening")
  })
})

describe("the shortest recording", () => {
  it("is the PC's minimum", () => {
    expect(MIN_RECORDING_BYTES).toBe(9600)
  })
})

describe("a microphone that is slow to open", () => {
  it("is let go when the owner taps again before it arrives", async () => {
    const w = world()
    let arrive: (mic: MicPort) => void = () => undefined
    w.ports.openMic = () => new Promise<MicPort>((resolve) => (arrive = resolve))
    w.session.tap()
    await tick()
    w.session.tap()
    await tick()
    expect(w.session.view.phase).toBe("idle")
    arrive(w.mic)
    await tick()
    expect(w.mic.closed).toBe(1)
    expect(w.mic.onChunk).toBeNull()
    expect(w.session.view.phase).toBe("idle")
  })

  it("is let go when the page is left before it arrives", async () => {
    const w = world()
    let arrive: (mic: MicPort) => void = () => undefined
    w.ports.openMic = () => new Promise<MicPort>((resolve) => (arrive = resolve))
    w.session.tap()
    await tick()
    w.session.stop()
    arrive(w.mic)
    await tick()
    expect(w.mic.closed).toBe(1)
  })
})
