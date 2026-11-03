// Audio maths for the phone's voice: the microphone's samples become 16 kHz, 16-bit mono PCM (what the PC
// accepts), the loudness the orb follows, and the end-point that decides when a request has been spoken.

/** The rate the PC takes (jarvis.remote.voice). */
export const TARGET_RATE = 16000
/** The longest recording, in seconds. */
export const MAX_RECORDING_SEC = 120
/** The most bytes the PC accepts: 120 s of 16-bit samples and one second of grace. */
export const MAX_VOICE_BYTES = MAX_RECORDING_SEC * TARGET_RATE * 2 + 32000

const FLOOR_DB = -52
const CEILING_DB = -6

/** Loudness 0..1 of audio with root-mean-square `rms` on a decibel scale (as jarvis.voice_levels.level_from_rms). */
export function levelFromRms(rms: number, fullScale = 1): number {
  if (!(rms > 0) || !(fullScale > 0)) return 0
  const db = 20 * Math.log10(rms / fullScale)
  return Math.min(1, Math.max(0, (db - FLOOR_DB) / (CEILING_DB - FLOOR_DB)))
}

export function rmsOf(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0
  let sum = 0
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i]
  return Math.sqrt(sum / samples.length)
}

/**
 * Streaming sample-rate conversion to 16 kHz. Above the target rate each output sample is the average of the
 * input span it covers; below it, a linear interpolation. The position carries across chunks, so any way of
 * cutting the stream gives the same samples.
 */
export class Downsampler {
  private readonly ratio: number
  // Box averaging: the output sample being built, and where its span ends on the whole stream.
  private index = 0
  private sum = 0
  private weight = 0
  private edge: number
  // Interpolation: the previous input sample and the position of the next output sample.
  private previous = 0
  private position = 0
  private produced = 0

  constructor(readonly inputRate: number) {
    if (!Number.isFinite(inputRate) || inputRate <= 0) throw new RangeError("sample rate must be positive")
    this.ratio = inputRate / TARGET_RATE
    this.edge = this.ratio
  }

  push(chunk: Float32Array): Float32Array {
    if (this.ratio === 1) return chunk.slice()
    return this.ratio > 1 ? this.average(chunk) : this.interpolate(chunk)
  }

  private average(chunk: Float32Array): Float32Array {
    const out = new Float32Array(Math.ceil(chunk.length / this.ratio) + 1)
    let made = 0
    for (let i = 0; i < chunk.length; i++) {
      const value = chunk[i]
      let low = this.index
      const end = this.index + 1
      while (low < end) {
        const high = Math.min(end, this.edge)
        const share = high - low
        this.sum += value * share
        this.weight += share
        low = high
        if (high >= this.edge) {
          out[made++] = this.sum / this.weight
          this.sum = 0
          this.weight = 0
          this.produced++
          this.edge = (this.produced + 1) * this.ratio
        }
      }
      this.index = end
    }
    return out.slice(0, made)
  }

  private interpolate(chunk: Float32Array): Float32Array {
    const step = this.ratio
    const out = new Float32Array(Math.ceil(chunk.length / step) + 1)
    let made = 0
    for (let i = 0; i < chunk.length; i++) {
      const value = chunk[i]
      const n = this.index
      while (this.position <= n) {
        out[made++] = n === 0 ? value : this.previous + (value - this.previous) * (this.position - (n - 1))
        this.produced++
        this.position = this.produced * step
      }
      this.previous = value
      this.index = n + 1
    }
    return out.slice(0, made)
  }
}

/** 16-bit samples from floats in -1..1 (anything outside is clipped, not wrapped). */
export function floatToPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length)
  for (let i = 0; i < samples.length; i++) {
    const value = samples[i]
    if (Number.isNaN(value)) continue
    const clipped = Math.min(1, Math.max(-1, value))
    out[i] = Math.round(clipped < 0 ? clipped * 32768 : clipped * 32767)
  }
  return out
}

/** The chunks as one little-endian byte string, whatever the platform's byte order. */
export function pcm16ToBytes(chunks: Int16Array[]): Uint8Array {
  let total = 0
  for (const chunk of chunks) total += chunk.length
  const bytes = new Uint8Array(total * 2)
  const view = new DataView(bytes.buffer)
  let at = 0
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i++) {
      view.setInt16(at, chunk[i], true)
      at += 2
    }
  }
  return bytes
}

export type EndpointOptions = {
  /** Seconds at the start used to measure the room's noise. */
  calibrateSec?: number
  /** How long the level must stay above the threshold to count as speech (a click does not). */
  minSpeechSec?: number
  /** How long below the threshold, after speech, ends the request. */
  endSilenceSec?: number
  /** How long with no speech gives up. */
  noSpeechSec?: number
  /** The longest recording. */
  maxSec?: number
}

export type EndpointResult = "listening" | "speech" | "end" | "no_speech" | "max"

const MIN_THRESHOLD = 0.2
const THRESHOLD_MARGIN = 0.12
const RELEASE = 0.8
// Speech that starts during the calibration would otherwise raise the floor out of reach of the speaker.
const MAX_FLOOR = 0.5

/**
 * Decides from a stream of levels (0..1) when a spoken request starts and ends. Time comes in as `dt`, so it
 * is pure. The room's noise floor is the mean level of the first moments; speech must rise above it, and
 * stays speech while the level holds above 80% of the threshold. Once a terminal result is returned it
 * keeps being returned.
 */
export class Endpointer {
  private readonly calibrateSec: number
  private readonly minSpeechSec: number
  private readonly endSilenceSec: number
  private readonly noSpeechSec: number
  private readonly maxSec: number
  private elapsed = 0
  private levelSum = 0
  private levelCount = 0
  private floorLevel = 0
  private limit = MIN_THRESHOLD
  private loud = 0
  private quiet = 0
  private heard = false
  private done: EndpointResult | null = null

  constructor(options: EndpointOptions = {}) {
    this.calibrateSec = options.calibrateSec ?? 0.3
    this.minSpeechSec = options.minSpeechSec ?? 0.12
    this.endSilenceSec = options.endSilenceSec ?? 1.2
    this.noSpeechSec = options.noSpeechSec ?? 8
    this.maxSec = options.maxSec ?? MAX_RECORDING_SEC
  }

  /** The measured noise floor (0 until the calibration has finished). */
  get floor(): number {
    return this.floorLevel
  }

  /** The level speech has to rise above. */
  get threshold(): number {
    return this.limit
  }

  /** Whether speech has been heard. */
  get speechHeard(): boolean {
    return this.heard
  }

  push(level: number, dt: number): EndpointResult {
    if (this.done) return this.done
    const value = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0
    const step = Number.isFinite(dt) && dt > 0 ? dt : 0
    const calibrating = this.elapsed < this.calibrateSec
    this.elapsed += step

    if (calibrating) {
      this.levelSum += value
      this.levelCount++
      if (this.elapsed >= this.calibrateSec) this.finishCalibration()
    } else {
      const result = this.follow(value, step)
      if (result) return (this.done = result)
    }

    if (this.elapsed >= this.maxSec) return (this.done = "max")
    if (!this.heard && this.elapsed >= this.noSpeechSec) return (this.done = "no_speech")
    return this.heard ? "speech" : "listening"
  }

  private finishCalibration(): void {
    const mean = this.levelCount > 0 ? this.levelSum / this.levelCount : 0
    this.floorLevel = Math.min(MAX_FLOOR, mean)
    this.limit = Math.max(MIN_THRESHOLD, this.floorLevel + THRESHOLD_MARGIN)
  }

  private follow(level: number, dt: number): EndpointResult | null {
    if (!this.heard) {
      if (level > this.limit) this.loud += dt
      else if (level <= this.limit * RELEASE) this.loud = 0
      if (this.loud >= this.minSpeechSec) {
        this.heard = true
        this.quiet = 0
      }
      return null
    }
    if (level > this.limit * RELEASE) this.quiet = 0
    else this.quiet += dt
    return this.quiet >= this.endSilenceSec ? "end" : null
  }
}
