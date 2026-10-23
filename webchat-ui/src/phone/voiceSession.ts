import { ApiError, type VoiceAudio } from "@/api"
import { MicError } from "@/phone/micCapture"
import { Downsampler, Endpointer, floatToPcm16, levelFromRms, pcm16ToBytes, rmsOf, TARGET_RATE } from "@/phone/pcm"

// One spoken exchange at a time, from a tap to the end of Jarvis's reply, with no React in it so the order of
// events can be tested: record from the phone's microphone, end on silence (or a tap, or the cap), send the
// audio to the PC, show the transcript, wait for the PC's reply audio and play it, and in conversation mode
// listen again. Everything that touches the browser or the network comes in through `VoicePorts`.
// See remote/remote.spec.md, Voice.

export type Phase = "idle" | "listening" | "sending" | "waiting" | "speaking"

export type VoiceView = { phase: Phase; message: string | null; conversation: boolean }

export type MicPort = { rate: number; onChunk: ((chunk: Float32Array) => void) | null; close(): void }

export type SpokenReply = { status: "accepted"; text: string; query_id: number } | { status: "nothing_heard" }

export type VoicePorts = {
  /** Resume audio playback inside the tap (iOS plays nothing until a gesture has unlocked it). */
  unlock(): Promise<void>
  openMic(): Promise<MicPort>
  /** Whether the PC can take a spoken request now (from the last poll), and why not. */
  availability(): { available: boolean; reason: string | null }
  sendAudio(pcm: Uint8Array, signal: AbortSignal): Promise<SpokenReply>
  /** Stop the request in flight on the PC. */
  stopQuery(): Promise<void>
  /** The transcript shows as the owner's message at once; then the PC takes it, or does not. */
  begin(text: string): void
  accepted(): void
  failed(): void
  fetchAudio(url: string): Promise<ArrayBuffer>
  play(wav: ArrayBuffer): Promise<void>
  stopPlayback(): void
  playerLevel(): number | null
  sleep(ms: number): Promise<void>
}

const CONVERSATION_PAUSE_MS = 300
// A reply can take a long time to work out; after this the phone stops waiting for its audio.
const REPLY_TIMEOUT_MS = 10 * 60 * 1000
/** The shortest recording the PC accepts (0.3 s of 16-bit samples). */
export const MIN_RECORDING_BYTES = Math.floor(0.3 * TARGET_RATE) * 2

const MESSAGES: Record<string, string> = {
  busy: "Jarvis is busy right now. Try again in a moment.",
  not_ready: "Jarvis is still loading its speech model. Try again in a moment.",
  disabled: "Voice needs dictation turned on in Jarvis's settings on the PC.",
  unavailable: "Jarvis can't listen on the PC right now.",
  no_https: "Voice needs a secure connection. On the PC run python -m jarvis.remote cert, then open the https address.",
  insecure: "Voice needs a secure connection. On the PC run python -m jarvis.remote cert, then open the https address.",
  too_short: "That was too short.",
  too_large: "That recording was too long.",
  denied: "The microphone is blocked. Allow it for this site in the browser's settings.",
  missing: "No microphone was found.",
  unsupported: "This browser can't record audio.",
  nothing_heard: "I didn't catch that.",
  no_speech: "I didn't hear anything.",
  playback: "I couldn't play the reply.",
}

/** What the page says, in plain words, for a refusal code or a microphone problem. */
export function voiceMessage(code: string): string {
  return MESSAGES[code] ?? "Voice didn't work. Try again."
}

function messageFor(error: unknown): string {
  if (error instanceof MicError) return voiceMessage(error.reason)
  if (error instanceof ApiError) return voiceMessage(error.code)
  return voiceMessage("")
}

export class VoiceSession {
  private phase: Phase = "idle"
  private message: string | null = null
  private conversation = false
  private mic: MicPort | null = null
  private endpointer = new Endpointer()
  private downsampler: Downsampler | null = null
  private recorded: Int16Array[] = []
  private recordedBytes = 0
  private micLevel = 0
  private abort: AbortController | null = null
  // Bumped whenever the session is stopped or restarted, so a late answer from an older exchange is ignored.
  private epoch = 0
  private audio: Record<string, VoiceAudio> = {}
  private waiting: { queryId: number; wake: () => void } | null = null

  constructor(
    private readonly ports: VoicePorts,
    private readonly onChange: (view: VoiceView) => void = () => undefined,
  ) {}

  get view(): VoiceView {
    return { phase: this.phase, message: this.message, conversation: this.conversation }
  }

  /** The level the orb follows: the microphone while listening, the reply while it plays, otherwise none. */
  level(): number | null {
    if (this.phase === "listening") return this.micLevel
    if (this.phase === "speaking") return this.ports.playerLevel()
    return null
  }

  // -- what the owner does ---------------------------------------------------------------------------

  /** A tap on the orb or the microphone button: start, or end, or interrupt, depending on what is happening. */
  tap(): void {
    switch (this.phase) {
      case "idle":
        void this.start()
        break
      case "listening":
        void this.finish()
        break
      case "sending":
      case "waiting":
        this.cancelExchange()
        break
      case "speaking":
        // Barge-in: Jarvis stops talking, and a conversation ends with it.
        this.setConversation(false)
        this.ports.stopPlayback()
        break
    }
  }

  setConversation(on: boolean): void {
    this.conversation = on
    if (!on && this.phase === "idle") this.releaseMic()
    this.emit()
  }

  /** The page is leaving or hidden: let go of the microphone, the audio and anything in flight. */
  stop(): void {
    this.epoch++
    this.abort?.abort()
    this.abort = null
    this.releaseMic()
    this.ports.stopPlayback()
    this.waiting?.wake()
    this.waiting = null
    this.phase = "idle"
    this.emit()
  }

  /** The PC's reply audio statuses, from every poll. */
  audioChanged(audio: Record<string, VoiceAudio>): void {
    this.audio = audio
    if (this.waiting && audio[String(this.waiting.queryId)]?.status !== "pending") this.waiting.wake()
  }

  /** Speak the reply to a typed request that asked for it (the "read replies aloud" switch). */
  async speakReply(queryId: number): Promise<void> {
    if (this.phase !== "idle") return
    const epoch = this.epoch
    this.phase = "waiting"
    this.message = null
    this.emit()
    await this.playReply(queryId, epoch)
  }

  // -- recording -----------------------------------------------------------------------------------------

  private async start(): Promise<void> {
    const epoch = ++this.epoch
    this.message = null
    const availability = this.ports.availability()
    if (!availability.available) return this.fail(voiceMessage(availability.reason ?? ""))
    // Inside the tap: the page's audio is unlocked before anything is awaited for long.
    const unlocked = this.ports.unlock()
    this.phase = "listening"
    this.endpointer = new Endpointer()
    this.recorded = []
    this.recordedBytes = 0
    this.micLevel = 0
    this.emit()
    try {
      await unlocked
      const mic = this.mic ?? (await this.ports.openMic())
      if (epoch !== this.epoch || this.phase !== "listening") {
        // The owner tapped again, or left, while the microphone was still opening.
        if (mic !== this.mic) mic.close()
        return
      }
      this.mic = mic
      this.downsampler = new Downsampler(mic.rate)
      mic.onChunk = (chunk) => this.hear(chunk, mic.rate)
    } catch (error) {
      if (epoch === this.epoch) this.fail(messageFor(error))
    }
  }

  private hear(chunk: Float32Array, rate: number): void {
    if (this.phase !== "listening" || !this.downsampler) return
    this.micLevel = levelFromRms(rmsOf(chunk))
    const samples = floatToPcm16(this.downsampler.push(chunk))
    this.recorded.push(samples)
    this.recordedBytes += samples.length * 2
    const verdict = this.endpointer.push(this.micLevel, chunk.length / rate)
    if (verdict === "end" || verdict === "max") void this.finish()
    else if (verdict === "no_speech") {
      this.conversation = false
      this.releaseMic()
      this.fail(voiceMessage("no_speech"))
    }
  }

  private async finish(): Promise<void> {
    if (this.phase !== "listening") return
    const epoch = this.epoch
    if (!this.mic) {
      // Still opening: nothing was recorded. The microphone, when it arrives, is let go (see `start`).
      this.epoch++
      this.phase = "idle"
      this.emit()
      return
    }
    this.mic.onChunk = null
    if (!this.conversation) this.releaseMic()
    const bytes = pcm16ToBytes(this.recorded)
    this.recorded = []
    if (bytes.length < MIN_RECORDING_BYTES) {
      this.conversation = false
      this.releaseMic()
      return this.fail(voiceMessage("too_short"))
    }
    this.phase = "sending"
    this.emit()
    const abort = (this.abort = new AbortController())
    try {
      const reply = await this.ports.sendAudio(bytes, abort.signal)
      if (epoch !== this.epoch) return
      if (reply.status === "nothing_heard") {
        this.conversation = false
        this.releaseMic()
        return this.fail(voiceMessage("nothing_heard"))
      }
      this.ports.begin(reply.text)
      this.ports.accepted()
      this.phase = "waiting"
      this.emit()
      await this.playReply(reply.query_id, epoch)
    } catch (error) {
      if (epoch !== this.epoch) return
      this.conversation = false
      this.releaseMic()
      this.ports.failed()
      this.fail(messageFor(error))
    }
  }

  // -- the reply ---------------------------------------------------------------------------------------------

  private async playReply(queryId: number, epoch: number): Promise<void> {
    const entry = await this.audioFor(queryId, epoch)
    if (epoch !== this.epoch) return
    if (!entry || entry.status !== "ready" || !entry.url) {
      this.conversation = false
      this.releaseMic()
      this.phase = "idle"
      this.emit()
      return
    }
    try {
      const clip = await this.ports.fetchAudio(entry.url)
      if (epoch !== this.epoch) return
      this.phase = "speaking"
      this.emit()
      await this.ports.play(clip)
    } catch (error) {
      if (epoch === this.epoch) {
        this.conversation = false
        this.releaseMic()
        return this.fail(error instanceof ApiError ? messageFor(error) : voiceMessage("playback"))
      }
      return
    }
    if (epoch !== this.epoch) return
    this.phase = "idle"
    this.emit()
    if (this.conversation) {
      await this.ports.sleep(CONVERSATION_PAUSE_MS)
      if (epoch === this.epoch && this.conversation && this.phase === "idle") await this.start()
    }
  }

  // Waits until the PC reports this request's audio as ready or not coming, a tap, or the timeout.
  private async audioFor(queryId: number, epoch: number): Promise<VoiceAudio | null> {
    const timeout = this.ports.sleep(REPLY_TIMEOUT_MS).then(() => "timeout" as const)
    while (epoch === this.epoch) {
      const entry = this.audio[String(queryId)]
      if (entry && entry.status !== "pending") return entry
      const outcome = await Promise.race([
        new Promise<"wake">((resolve) => {
          this.waiting = { queryId, wake: () => resolve("wake") }
        }),
        timeout,
      ])
      this.waiting = null
      if (outcome === "timeout") return null
    }
    return null
  }

  // -- ending --------------------------------------------------------------------------------------------------

  private cancelExchange(): void {
    this.epoch++
    this.abort?.abort()
    this.abort = null
    this.waiting?.wake()
    this.waiting = null
    this.conversation = false
    this.releaseMic()
    this.phase = "idle"
    this.message = null
    this.emit()
    // A request the PC already took is stopped there too; its reply is dropped, as the Stop button does.
    void this.ports.stopQuery().catch(() => undefined)
  }

  private releaseMic(): void {
    if (this.mic) {
      this.mic.onChunk = null
      this.mic.close()
      this.mic = null
    }
    this.downsampler = null
  }

  private fail(message: string): void {
    this.phase = "idle"
    this.message = message
    this.emit()
  }

  private emit(): void {
    this.onChange(this.view)
  }
}
