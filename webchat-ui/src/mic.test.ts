import { describe, expect, it, vi } from "vitest"
import { ApiError } from "@/api"
import {
  HEARTBEAT_MS,
  MicRecorder,
  describeMicError,
  formatElapsed,
  isLastingRefusal,
  type MicApi,
  type MicTimers,
  type MicView,
} from "@/mic"

// The microphone button records in the daemon, never in the browser. These tests drive MicRecorder with a
// scripted server and a manual clock, so they check what the owner sees and what the server is told.

class Clock implements MicTimers {
  time = 0
  private next = 1
  private tasks = new Map<number, { every: number; due: number; run: () => void }>()
  now = () => this.time
  setInterval = (run: () => void, every: number) => {
    const id = this.next++
    this.tasks.set(id, { every, due: this.time + every, run })
    return id
  }
  clearInterval = (id: number) => {
    this.tasks.delete(id)
  }
  get running() {
    return this.tasks.size
  }
  async advance(ms: number) {
    const end = this.time + ms
    for (;;) {
      const due = [...this.tasks.values()].filter((t) => t.due <= end).sort((a, b) => a.due - b.due)[0]
      if (!due) break
      this.time = due.due
      due.due += due.every
      due.run()
      await settle()
    }
    this.time = end
  }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
async function settle() {
  for (let i = 0; i < 6; i++) await tick()
}

const SESSION = "a".repeat(32)

function setup(overrides: Partial<MicApi> = {}) {
  const clock = new Clock()
  const api: MicApi = {
    status: vi.fn(async () => ({ available: true, reason: null, max_seconds: 120 })),
    start: vi.fn(async () => ({ session: SESSION, max_seconds: 120 })),
    heartbeat: vi.fn(async () => ({ state: "recording" })),
    stop: vi.fn(async () => ({ text: "hello there" })),
    cancel: vi.fn(async () => undefined),
    ...overrides,
  }
  const delivered: string[] = []
  const views: MicView[] = []
  const recorder = new MicRecorder(api, (text) => delivered.push(text), (view) => views.push(view), clock)
  const view = () => views[views.length - 1]
  return { clock, api, recorder, delivered, views, view }
}

describe("recording", () => {
  it("starts on the first click, shows the recording and counts the time", async () => {
    const t = setup()
    await t.recorder.toggle()
    expect(t.view().phase).toBe("recording")
    expect(t.view().maxSeconds).toBe(120)
    await t.clock.advance(7_000)
    expect(t.view().elapsed).toBe(7)
    expect(formatElapsed(t.view().elapsed)).toBe("0:07")
  })

  it("stops on the second click and hands the transcript over without sending it", async () => {
    const t = setup()
    await t.recorder.toggle()
    const stopping = t.recorder.toggle()
    expect(t.view().phase).toBe("transcribing")
    await stopping
    expect(t.api.stop).toHaveBeenCalledWith(SESSION)
    expect(t.delivered).toEqual(["hello there"])
    expect(t.view().phase).toBe("idle")
    expect(t.clock.running).toBe(0)
  })

  it("stops on Escape the same way", async () => {
    const t = setup()
    await t.recorder.toggle()
    await t.recorder.stop()
    expect(t.delivered).toEqual(["hello there"])
  })

  it("says so when nothing was heard", async () => {
    const t = setup({ stop: vi.fn(async () => ({ text: "" })) })
    await t.recorder.toggle()
    await t.recorder.toggle()
    expect(t.delivered).toEqual([])
    expect(t.view().phase).toBe("idle")
    expect(t.view().message).toMatch(/nothing/i)
  })

  it("ignores clicks while it is starting or transcribing", async () => {
    let finishStart!: () => void
    const t = setup({ start: vi.fn(() => new Promise<{ session: string; max_seconds: number }>((resolve) => (finishStart = () => resolve({ session: SESSION, max_seconds: 120 })))) })
    const first = t.recorder.toggle()
    expect(t.view().phase).toBe("starting")
    await t.recorder.toggle()
    expect(t.api.start).toHaveBeenCalledTimes(1)
    expect(t.api.stop).not.toHaveBeenCalled()
    finishStart()
    await first
    expect(t.view().phase).toBe("recording")
  })
})

describe("staying alive", () => {
  it("tells the server the page is still there while recording", async () => {
    const t = setup()
    await t.recorder.toggle()
    await t.clock.advance(HEARTBEAT_MS * 3 + 10)
    expect(t.api.heartbeat).toHaveBeenCalledTimes(3)
    expect(t.api.heartbeat).toHaveBeenCalledWith(SESSION)
  })

  it("stops and collects the text when the server reports the length limit", async () => {
    const t = setup({ heartbeat: vi.fn(async () => ({ state: "full" })) })
    await t.recorder.toggle()
    await t.clock.advance(HEARTBEAT_MS + 10)
    expect(t.api.stop).toHaveBeenCalledWith(SESSION)
    expect(t.delivered).toEqual(["hello there"])
    expect(t.view().phase).toBe("idle")
    expect(t.view().message).toMatch(/limit|longest/i)
  })

  it("gives up when the server no longer has the recording", async () => {
    const t = setup({ heartbeat: vi.fn(async () => { throw new ApiError(404, "unknown") }) })
    await t.recorder.toggle()
    await t.clock.advance(HEARTBEAT_MS + 10)
    expect(t.view().phase).toBe("idle")
    expect(t.view().message).toMatch(/lost|ended/i)
    expect(t.api.stop).not.toHaveBeenCalled()
    expect(t.clock.running).toBe(0)
  })

  it("keeps recording through a heartbeat that could not reach the server", async () => {
    let calls = 0
    const t = setup({ heartbeat: vi.fn(async () => { if (calls++ === 0) throw new TypeError("network"); return { state: "recording" } }) })
    await t.recorder.toggle()
    await t.clock.advance(HEARTBEAT_MS * 2 + 10)
    expect(t.view().phase).toBe("recording")
  })
})

describe("refusals", () => {
  it.each([
    ["busy", /busy/i],
    ["unavailable", /isn't available/i],
    ["disabled", /settings/i],
    ["not_ready", /loading|ready/i],
    ["microphone", /microphone/i],
  ])("explains a %s refusal and stays idle", async (code, wording) => {
    const t = setup({ start: vi.fn(async () => { throw new ApiError(code === "busy" ? 409 : 503, code) }) })
    await t.recorder.toggle()
    expect(t.view().phase).toBe("idle")
    expect(t.view().message).toMatch(wording)
    expect(t.clock.running).toBe(0)
  })

  it("explains an unreachable Jarvis", () => {
    expect(describeMicError(new TypeError("network"))).toMatch(/reached/i)
  })

  it("forgets a message when the next recording starts", async () => {
    let refuse = true
    const t = setup({ start: vi.fn(async () => { if (refuse) throw new ApiError(409, "busy"); return { session: SESSION, max_seconds: 120 } }) })
    await t.recorder.toggle()
    expect(t.view().message).not.toBeNull()
    refuse = false
    await t.recorder.toggle()
    expect(t.view().message).toBeNull()
  })

  it("reports a transcript that could not be fetched", async () => {
    const t = setup({ stop: vi.fn(async () => { throw new TypeError("network") }) })
    await t.recorder.toggle()
    await t.recorder.toggle()
    expect(t.delivered).toEqual([])
    expect(t.view().phase).toBe("idle")
    expect(t.view().message).toMatch(/reached/i)
  })
})

describe("leaving", () => {
  it("cancels the recording, delivering nothing, when asked to", async () => {
    const t = setup()
    await t.recorder.toggle()
    t.recorder.cancel()
    expect(t.api.cancel).toHaveBeenCalledWith(SESSION)
    expect(t.view().phase).toBe("idle")
    expect(t.delivered).toEqual([])
    expect(t.clock.running).toBe(0)
  })

  it("cancels a recording that finished starting after the page was torn down", async () => {
    let finishStart!: () => void
    const t = setup({ start: vi.fn(() => new Promise<{ session: string; max_seconds: number }>((resolve) => (finishStart = () => resolve({ session: SESSION, max_seconds: 120 })))) })
    const starting = t.recorder.toggle()
    t.recorder.dispose()
    finishStart()
    await starting
    expect(t.api.cancel).toHaveBeenCalledWith(SESSION)
    expect(t.clock.running).toBe(0)
  })

  it("does nothing when there is no recording to cancel", () => {
    const t = setup()
    t.recorder.cancel()
    expect(t.api.cancel).not.toHaveBeenCalled()
  })

  it("delivers nothing for a transcript that arrives after the page was torn down", async () => {
    let finishStop!: () => void
    const t = setup({ stop: vi.fn(() => new Promise<{ text: string }>((resolve) => (finishStop = () => resolve({ text: "late" })))) })
    await t.recorder.toggle()
    const stopping = t.recorder.toggle()
    t.recorder.dispose()
    finishStop()
    await stopping
    expect(t.delivered).toEqual([])
  })
})

describe("formatting", () => {
  it.each([
    [0, "0:00"],
    [7, "0:07"],
    [65, "1:05"],
    [120, "2:00"],
  ])("shows %i seconds as %s", (seconds, text) => {
    expect(formatElapsed(seconds)).toBe(text)
  })
})

describe("which refusals disable the button", () => {
  it.each([
    ["disabled", true],
    ["unavailable", true],
    ["busy", false],
    ["not_ready", false],
    ["microphone", false],
    [null, false],
  ])("%s -> %s", (reason, lasting) => {
    expect(isLastingRefusal(reason)).toBe(lasting)
  })
})
