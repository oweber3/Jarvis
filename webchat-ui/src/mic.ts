import { ApiError, request } from "@/api"

// The microphone button. Jarvis records, not the browser: the page never asks for the microphone or uses a
// speech API, it only tells the daemon to start and stop (jarvis/webchat/mic.py) and receives the text.
// While recording it keeps telling the daemon it is still here; a page that closes or loses the connection
// stops doing so and the daemon releases the microphone by itself.

export const HEARTBEAT_MS = 2000
const TICK_MS = 250

export type MicPhase = "idle" | "starting" | "recording" | "transcribing"

export type MicView = {
  phase: MicPhase
  /** Whole seconds recorded so far. */
  elapsed: number
  /** The longest a recording may run, as the daemon reported when it started. */
  maxSeconds: number
  /** Something to tell the owner (a refusal, or that nothing was heard), until the next recording. */
  message: string | null
}

export type MicStatus = { available: boolean; reason: string | null; max_seconds: number }

export type MicApi = {
  status: () => Promise<MicStatus>
  start: () => Promise<{ session: string; max_seconds: number }>
  heartbeat: (session: string) => Promise<{ state: string }>
  stop: (session: string) => Promise<{ text: string }>
  cancel: (session: string) => Promise<unknown>
}

export type MicTimers = {
  now: () => number
  setInterval: (run: () => void, ms: number) => number
  clearInterval: (id: number) => void
}

const REAL_TIMERS: MicTimers = {
  now: () => Date.now(),
  setInterval: (run, ms) => globalThis.setInterval(run, ms) as unknown as number,
  clearInterval: (id) => globalThis.clearInterval(id),
}

export const micApi: MicApi = {
  status: () => request<MicStatus>("GET", "/api/mic"),
  start: () => request("POST", "/api/mic/start"),
  heartbeat: (session) => request("POST", "/api/mic/heartbeat", { session }),
  stop: (session) => request("POST", "/api/mic/stop", { session }),
  // Sent as the page closes too, so it must outlive the page.
  cancel: (session) =>
    fetch("/api/mic/cancel", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session }),
    }),
}

export function formatElapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`
}

// What the page says when the microphone is refused. Codes are the server's; the wording is the page's.
export function describeMicError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Jarvis could not be reached."
  switch (error.code) {
    case "busy":
      return "Jarvis is busy. Try again in a moment."
    case "unavailable":
      return "Voice input isn't available here: speech recognition or the microphone isn't set up."
    case "disabled":
      return "Dictation is turned off in Settings."
    case "not_ready":
      return "Speech recognition is still loading. Try again in a moment."
    case "microphone":
      return "Jarvis couldn't open the microphone."
    case "unknown":
      return "The recording ended before it could be used."
    default:
      return "Voice input didn't work."
  }
}

/** Reasons a recording cannot start that will not clear by waiting, so the button is shown as unavailable. */
export const isLastingRefusal = (reason: string | null) => reason === "disabled" || reason === "unavailable"

export class MicRecorder {
  private phase: MicPhase = "idle"
  private session: string | null = null
  private startedAt = 0
  private maxSeconds = 0
  private message: string | null = null
  private elapsedShown = -1
  private tickId: number | null = null
  private beatId: number | null = null
  private beating = false
  private disposed = false

  constructor(
    private readonly api: MicApi,
    private readonly deliver: (text: string) => void,
    private readonly onChange: (view: MicView) => void,
    private readonly timers: MicTimers = REAL_TIMERS,
  ) {}

  /** One click: start when idle, stop while recording, nothing in between. */
  async toggle(): Promise<void> {
    if (this.phase === "idle") await this.start()
    else if (this.phase === "recording") await this.stop()
  }

  async start(): Promise<void> {
    if (this.phase !== "idle" || this.disposed) return
    this.message = null
    this.setPhase("starting")
    let started: { session: string; max_seconds: number }
    try {
      started = await this.api.start()
    } catch (error) {
      if (this.disposed) return
      this.message = describeMicError(error)
      this.setPhase("idle")
      return
    }
    if (this.disposed) {
      void this.api.cancel(started.session).catch(() => undefined)
      return
    }
    this.session = started.session
    this.maxSeconds = started.max_seconds
    this.startedAt = this.timers.now()
    this.elapsedShown = -1
    this.tickId = this.timers.setInterval(() => this.tick(), TICK_MS)
    this.beatId = this.timers.setInterval(() => void this.beat(), HEARTBEAT_MS)
    this.setPhase("recording")
  }

  /** End the recording and hand the transcript to the caller; it is never sent anywhere from here. */
  async stop(reason?: "limit"): Promise<void> {
    const session = this.session
    if (this.phase !== "recording" || session === null) return
    this.clearTimers()
    this.setPhase("transcribing")
    let text = ""
    try {
      text = (await this.api.stop(session)).text.trim()
    } catch (error) {
      this.session = null
      if (this.disposed) return
      this.message = describeMicError(error)
      this.setPhase("idle")
      return
    }
    this.session = null
    if (this.disposed) return
    if (text) {
      this.deliver(text)
      this.message = reason === "limit" ? `Reached the longest recording (${formatElapsed(this.maxSeconds)}).` : null
    } else {
      this.message = "Nothing was heard."
    }
    this.setPhase("idle")
  }

  /** Drop the recording and release the microphone; the page is leaving or the owner gave up. */
  cancel(): void {
    const session = this.session
    if (this.phase !== "recording" || session === null) return
    this.clearTimers()
    this.session = null
    void this.api.cancel(session).catch(() => undefined)
    this.setPhase("idle")
  }

  dispose(): void {
    this.disposed = true
    this.cancel()
    this.clearTimers()
  }

  private tick(): void {
    const elapsed = Math.floor((this.timers.now() - this.startedAt) / 1000)
    if (elapsed !== this.elapsedShown) this.emit()
  }

  private async beat(): Promise<void> {
    const session = this.session
    if (this.beating || session === null || this.phase !== "recording") return
    this.beating = true
    try {
      const { state } = await this.api.heartbeat(session)
      if (state === "full") await this.stop("limit")
    } catch (error) {
      // An unreachable server is waited out (the daemon decides when the page has left); a recording the
      // daemon no longer has is over.
      if (error instanceof ApiError && error.status === 404 && this.phase === "recording" && this.session === session && !this.disposed) {
        this.clearTimers()
        this.session = null
        this.message = describeMicError(error)
        this.setPhase("idle")
      }
    } finally {
      this.beating = false
    }
  }

  private clearTimers(): void {
    if (this.tickId !== null) this.timers.clearInterval(this.tickId)
    if (this.beatId !== null) this.timers.clearInterval(this.beatId)
    this.tickId = this.beatId = null
  }

  private setPhase(phase: MicPhase): void {
    this.phase = phase
    this.emit()
  }

  private emit(): void {
    const elapsed = this.phase === "recording" ? Math.floor((this.timers.now() - this.startedAt) / 1000) : 0
    this.elapsedShown = elapsed
    this.onChange({ phase: this.phase, elapsed, maxSeconds: this.maxSeconds, message: this.message })
  }
}
