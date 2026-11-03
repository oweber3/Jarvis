import { levelFromRms, rmsOf } from "@/phone/pcm"

// Plays the PC's reply audio (a WAV) through the page's audio context, with an analyser so the orb can follow
// the voice. The context is created and resumed on a tap, as iOS requires, and shared by the microphone.

type WebkitGlobal = { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }

export function createAudioContext(): AudioContext {
  const win = window as unknown as WebkitGlobal
  const Context = win.AudioContext ?? win.webkitAudioContext
  if (!Context) throw new Error("This browser cannot play or record audio.")
  return new Context()
}

let shared: AudioContext | null = null

/** The page's one audio context, created on first use (call it from a tap the first time). */
export function sharedAudioContext(): AudioContext {
  if (!shared || shared.state === "closed") shared = createAudioContext()
  return shared
}

type Playing = { source: AudioBufferSourceNode; analyser: AnalyserNode; finish: () => void }

export class PhonePlayer {
  private active: Playing | null = null
  private ticket = 0
  private readonly samples = new Float32Array(1024)

  constructor(private readonly ctx: AudioContext) {}

  /** Resume the context and play one silent sample. Call it inside a tap; it is safe to call again. */
  async unlock(): Promise<void> {
    try {
      if (this.ctx.state !== "running") await this.ctx.resume()
      const buffer = this.ctx.createBuffer(1, 1, this.ctx.sampleRate)
      const source = this.ctx.createBufferSource()
      source.buffer = buffer
      source.connect(this.ctx.destination)
      source.start(0)
    } catch {
      // A browser that refuses here will refuse at the next tap too; playing reports its own failure.
    }
  }

  get playing(): boolean {
    return this.active !== null
  }

  /** Play `wav` and resolve when it ends or `stop` is called. Rejects when the audio cannot be decoded. */
  async play(wav: ArrayBuffer): Promise<void> {
    this.stop()
    const ticket = ++this.ticket
    let buffer: AudioBuffer
    try {
      if (this.ctx.state === "suspended") await this.ctx.resume().catch(() => undefined)
      buffer = await this.ctx.decodeAudioData(wav)
    } catch {
      throw new Error("The reply audio could not be played.")
    }
    // Stopped, or another reply started, while decoding.
    if (ticket !== this.ticket) return
    await new Promise<void>((resolve) => {
      const source = this.ctx.createBufferSource()
      const analyser = this.ctx.createAnalyser()
      analyser.fftSize = 1024
      source.buffer = buffer
      source.connect(analyser)
      analyser.connect(this.ctx.destination)
      let finished = false
      const finish = () => {
        if (finished) return
        finished = true
        source.onended = null
        try {
          source.stop()
        } catch {
          // Already stopped.
        }
        try {
          source.disconnect()
          analyser.disconnect()
        } catch {
          // Already disconnected.
        }
        if (this.active?.source === source) this.active = null
        resolve()
      }
      source.onended = finish
      this.active = { source, analyser, finish }
      source.start(0)
    })
  }

  /** Stop what is playing, if anything. Safe to call at any time. */
  stop(): void {
    this.ticket++
    this.active?.finish()
  }

  /** The loudness 0..1 of what is playing now, or null when nothing is. */
  level(): number | null {
    const active = this.active
    if (!active) return null
    const size = Math.min(this.samples.length, active.analyser.fftSize)
    const view = size === this.samples.length ? this.samples : this.samples.subarray(0, size)
    active.analyser.getFloatTimeDomainData(view)
    return levelFromRms(rmsOf(view))
  }
}
