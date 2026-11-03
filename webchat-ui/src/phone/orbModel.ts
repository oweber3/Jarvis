import { ORB_LOOKS } from "@/generated/orbPalette"

// The orb's animation maths, a port of OrbModel in desktop_app/orb_widget.py: a sphere of points whose
// surface moves with the voice, turns, glows per state and flashes on a wake. Pure (no DOM, no React),
// so the same script gives the same frames as the desktop orb (see orbModel.golden.json).

export type OrbState =
  | "offline"
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "dictating"
  | "muted"
  | "error"

export type OrbLook = {
  glow: number
  spin: number
  think: number
  voiceGain: number
  stir: number
  colour: string
  accent: string
  label: string
  swell: number
}

export const ORB_STATES: readonly OrbState[] = [
  "offline",
  "idle",
  "listening",
  "thinking",
  "speaking",
  "dictating",
  "muted",
  "error",
]

/** States in which the orb is at rest; moving from one of these into any other is a wake and fires the flash. */
export const RESTING_STATES: ReadonlySet<OrbState> = new Set<OrbState>(["offline", "idle", "muted", "error"])

/** Points of the phone's sphere (the desktop draws 5000; fewer spares the battery). */
export const PHONE_SPHERE_POINTS = 3200

const INNER_SHARE = 0.2
const INNER_RADIUS = 0.55
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))
const VIEW_TILT = (20 * Math.PI) / 180
const MAX_DT = 0.1
const FLASH_DECAY = 2.2

export function lookFor(state: OrbState): OrbLook {
  return ORB_LOOKS[state]
}

/** Speech-like 0..1 envelope at time `t` for when no real amplitude is available. */
export function syntheticSpeechLevel(t: number): number {
  const syllables = 0.5 + 0.5 * Math.sin(t * 9) * Math.sin(t * 2.3 + 1)
  return 0.25 + 0.55 * Math.max(0, syllables)
}

function approach(value: number, target: number, rate: number, dt: number): number {
  return value + (target - value) * (1 - Math.exp(-rate * dt))
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}

export type Sphere = {
  count: number
  /** x, y, z per point (outer sphere at radius 1, then the inner one). */
  position: Float64Array
  /** 1 for points of the inner sphere. */
  inner: Uint8Array
}

function fibonacci(into: Float64Array, from: number, count: number, offset: number, radius: number): void {
  for (let i = 0; i < count; i++) {
    const y = 1 - (2 * (i + 0.5)) / count
    const r = Math.sqrt(1 - y * y)
    const a = i * GOLDEN_ANGLE + offset
    const at = (from + i) * 3
    into[at] = Math.cos(a) * r * radius
    into[at + 1] = y * radius
    into[at + 2] = Math.sin(a) * r * radius
  }
}

/** The orb's points: an evenly spread outer sphere and a smaller inner one. */
export function buildSphere(count: number = PHONE_SPHERE_POINTS): Sphere {
  const total = Math.max(2, Math.floor(count))
  const innerCount = Math.floor(total * INNER_SHARE)
  const outerCount = total - innerCount
  const position = new Float64Array(total * 3)
  fibonacci(position, 0, outerCount, 0, 1)
  fibonacci(position, outerCount, innerCount, 1, INNER_RADIUS)
  const inner = new Uint8Array(total)
  inner.fill(1, outerCount)
  return { count: total, position, inner }
}

/** The smooth, slowly changing shape over the sphere, about -1..1: where the surface rises and falls. */
export function surfaceField(x: number, y: number, z: number, phase: number): number {
  return (
    Math.sin(x * 2.6 + phase * 1.7) * Math.sin(y * 2.9 - phase * 1.3) * Math.sin(z * 2.4 + phase * 2.1) +
    0.55 * Math.sin(x * 5.1 - phase * 2.6 + y * 1.5) * Math.sin(z * 5.6 + phase * 2.2) +
    0.3 * Math.sin(y * 9 - phase * 4 + z * 2) * Math.sin(x * 8.5 + phase * 3.1)
  )
}

/** One projected frame: unit coordinates (centre 0, rest radius 1), depth -1 (back) to 1 (front), light 0..1. */
export type SphereFrame = {
  count: number
  x: Float32Array
  y: Float32Array
  depth: Float32Array
  light: Float32Array
  inner: Uint8Array
}

export class OrbModel {
  glow = 0
  /** Smoothed voice level 0..1: the surface swells and ripples with it. */
  voice = 0
  /** Thinking glow 0..1: inner light and a sweeping band. */
  think = 0
  /** Wake flash 0..1, decays after a wake. */
  flash = 0
  /** Hologram brightness flicker, close to 1. */
  shimmer = 1
  /** Rotation in radians, unbounded. */
  spin = 0
  time = 0

  readonly sphere: Sphere
  private spinSpeed = 0
  private stir = 0
  private swell = 1
  private phase = 0
  private previous: OrbState | null = null
  private readonly direction: Float64Array
  private readonly phaseOffset: Float64Array
  private readonly frame: SphereFrame

  constructor(count: number = PHONE_SPHERE_POINTS) {
    this.sphere = buildSphere(count)
    const n = this.sphere.count
    this.direction = new Float64Array(n * 3)
    this.phaseOffset = new Float64Array(n)
    const { position, inner } = this.sphere
    for (let i = 0; i < n; i++) {
      const at = i * 3
      const size = Math.hypot(position[at], position[at + 1], position[at + 2])
      this.direction[at] = position[at] / size
      this.direction[at + 1] = position[at + 1] / size
      this.direction[at + 2] = position[at + 2] / size
      // The inner sphere moves out of step with the outer one.
      this.phaseOffset[i] = inner[i] ? 2 : 0
    }
    this.frame = {
      count: n,
      x: new Float32Array(n),
      y: new Float32Array(n),
      depth: new Float32Array(n),
      light: new Float32Array(n),
      inner,
    }
  }

  /** Advance by `dt` seconds towards the look of `state`. `level` is the voice level 0..1, or null for none. */
  step(dt: number, state: OrbState, level: number | null): void {
    dt = Number.isFinite(dt) ? Math.min(Math.max(dt, 0), MAX_DT) : 0
    const look = lookFor(state)
    this.time += dt

    const previous = this.previous
    this.previous = state
    if (previous !== null && RESTING_STATES.has(previous) && !RESTING_STATES.has(state)) this.flash = 1
    this.flash = clamp01(this.flash * Math.exp(-FLASH_DECAY * dt))

    const t = this.time
    const flicker = 0.5 + 0.5 * Math.sin(t * 37) * Math.sin(t * 11.3 + 0.7)
    this.shimmer = clamp01(1 - 0.03 * flicker)

    this.glow = clamp01(approach(this.glow, look.glow, 4, dt))
    this.think = clamp01(approach(this.think, look.think, 5, dt))
    this.stir = approach(this.stir, look.stir, 3, dt)
    this.swell = approach(this.swell, look.swell, 3, dt)
    this.spinSpeed = approach(this.spinSpeed, look.spin, 3, dt)
    this.spin += this.spinSpeed * dt

    let heard = level
    if (heard === null || !Number.isFinite(heard)) heard = state === "speaking" ? syntheticSpeechLevel(t) : 0
    const target = clamp01(heard) * look.voiceGain
    // Quick to rise, slower to fall, like a level meter.
    this.voice = clamp01(approach(this.voice, target, target > this.voice ? 18 : 7, dt))
    this.phase += dt * (0.6 + 5 * this.swell * this.voice)
  }

  /** Whether the orb is at rest: a resting state with no flash left. */
  isResting(state: OrbState): boolean {
    return RESTING_STATES.has(state) && this.flash < 0.01
  }

  /**
   * Project the sphere for this frame: the surface shaped by the voice, turned, see-through. The arrays
   * belong to the model and are overwritten by the next call.
   */
  sphereFrame(): SphereFrame {
    const t = this.time
    const { position, inner } = this.sphere
    const { direction, phaseOffset } = this
    const { x: outX, y: outY, depth: outDepth, light: outLight, count } = this.frame
    const think = this.think
    const voice = this.voice

    const breath =
      1 + 0.015 * Math.sin(t * 1.3) - 0.03 * think * (0.5 + 0.5 * Math.sin(t * 4)) + 0.08 * this.flash
    const baseAmplitude = this.stir + 0.22 * this.swell * voice

    // View: turn about the vertical axis, then tilt (seen from a little above); rows of tilt @ turn.
    const cy = Math.cos(this.spin)
    const sy = Math.sin(this.spin)
    const ct = Math.cos(VIEW_TILT)
    const st = Math.sin(VIEW_TILT)
    const r00 = cy
    const r02 = sy
    const r10 = st * sy
    const r11 = ct
    const r12 = -st * cy
    const r20 = -ct * sy
    const r21 = st
    const r22 = ct * cy

    const band = ((t * 0.65) % 1) * 2.4 - 1.2
    const sweeping = think > 1e-6
    const innerLight = 0.35 + 0.5 * think
    const voiceLift = 0.3 + voice

    for (let i = 0; i < count; i++) {
      const at = i * 3
      const dx = direction[at]
      const dy = direction[at + 1]
      const dz = direction[at + 2]
      const isInner = inner[i] === 1
      const shape = surfaceField(dx, dy, dz, this.phase + phaseOffset[i])
      const amplitude = (isInner ? 1.5 : 1) * baseAmplitude
      const ripple = 0.05 * think * Math.sin(dy * 10 - t * 6)
      const scale = breath * (1 + amplitude * shape + ripple)
      const px = position[at] * scale
      const py = position[at + 1] * scale
      const pz = position[at + 2] * scale
      const x = r00 * px + r02 * pz
      const y = r10 * px + r11 * py + r12 * pz
      const z = r20 * px + r21 * py + r22 * pz

      const size = Math.max(Math.sqrt(x * x + y * y + z * z), 1e-6)
      const depth = Math.min(1, Math.max(-1, z / size))
      const front = (depth + 1) / 2
      const limb = Math.hypot(x, y) / size
      const limb2 = limb * limb
      // Front brighter than back, the outline brightest, and the raised parts of the surface catch more light.
      let light =
        (0.1 + 0.9 * Math.pow(front, 1.7)) *
        (0.4 + 0.6 * limb2 * limb2) *
        (0.75 + 0.6 * Math.max(shape, 0) * voiceLift)
      if (isInner) {
        // The inner sphere is dim, and lights up from within while thinking.
        light *= innerLight
      } else if (sweeping) {
        // Thinking: a band of light sweeps over the outer sphere, top to bottom.
        const away = dy - band
        light += think * 0.9 * Math.exp(-(away * away) / 0.012) * (0.3 + 0.7 * front)
      }
      outX[i] = x
      outY[i] = -y
      outDepth[i] = depth
      outLight[i] = Math.min(1, Math.max(0, light))
    }
    return this.frame
  }
}
