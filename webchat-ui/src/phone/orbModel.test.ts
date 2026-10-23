import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { ORB_LOOKS } from "@/generated/orbPalette"
import {
  OrbModel,
  ORB_STATES,
  PHONE_SPHERE_POINTS,
  RESTING_STATES,
  buildSphere,
  lookFor,
  syntheticSpeechLevel,
  type OrbState,
} from "@/phone/orbModel"

// The golden file is made by running the desktop OrbModel through a fixed script; the port must follow it.

type Checkpoint = {
  step: number
  glow: number
  voice: number
  think: number
  flash: number
  spin: number
  shimmer: number
  x: number[]
  y: number[]
  depth: number[]
  light: number[]
  inner: number[]
}
type Golden = {
  count: number
  dt: number
  stride: number
  script: { state: OrbState; level: number | null; steps: number }[]
  checkpoints: Checkpoint[]
}

const golden: Golden = JSON.parse(readFileSync(new URL("./orbModel.golden.json", import.meta.url), "utf-8"))

function near(actual: ArrayLike<number>, expected: number[], tolerance: number, stride: number) {
  expect(actual.length).toBeGreaterThanOrEqual(expected.length * stride - stride + 1)
  expected.forEach((value, k) => expect(Math.abs(actual[k * stride] - value)).toBeLessThan(tolerance))
}

function run(model: OrbModel, state: OrbState, level: number | null, steps: number, dt = 1 / 30) {
  for (let i = 0; i < steps; i++) model.step(dt, state, level)
}

describe("OrbModel against the desktop orb", () => {
  it("follows the desktop model through the scripted states", () => {
    const model = new OrbModel(golden.count)
    let step = 0
    let checked = 0
    for (const part of golden.script) {
      for (let i = 0; i < part.steps; i++) {
        model.step(golden.dt, part.state, part.level)
        const point = golden.checkpoints.find((c) => c.step === step)
        if (point) {
          checked++
          for (const field of ["glow", "voice", "think", "flash", "spin", "shimmer"] as const) {
            expect(Math.abs(model[field] - point[field]), `${field} at step ${step}`).toBeLessThan(1e-4)
          }
          const frame = model.sphereFrame()
          near(frame.x, point.x, 1e-4, golden.stride)
          near(frame.y, point.y, 1e-4, golden.stride)
          near(frame.depth, point.depth, 1e-4, golden.stride)
          near(frame.light, point.light, 1e-3, golden.stride)
          point.inner.forEach((value, k) => expect(frame.inner[k * golden.stride]).toBe(value))
        }
        step++
      }
    }
    expect(checked).toBe(golden.checkpoints.length)
    expect(checked).toBeGreaterThan(3)
  })
})

describe("buildSphere", () => {
  it("makes an outer unit sphere and an inner sphere of a fifth of the points", () => {
    const sphere = buildSphere(1000)
    expect(sphere.count).toBe(1000)
    let inner = 0
    for (let i = 0; i < sphere.count; i++) {
      const radius = Math.hypot(sphere.position[i * 3], sphere.position[i * 3 + 1], sphere.position[i * 3 + 2])
      if (sphere.inner[i]) {
        inner++
        expect(radius).toBeCloseTo(0.55, 9)
      } else {
        expect(radius).toBeCloseTo(1, 9)
      }
    }
    expect(inner).toBe(200)
  })

  it("defaults to the phone's point count", () => {
    expect(buildSphere().count).toBe(PHONE_SPHERE_POINTS)
    expect(PHONE_SPHERE_POINTS).toBe(3200)
  })

  it("spreads the outer points evenly top to bottom", () => {
    const sphere = buildSphere(2000)
    let above = 0
    let outer = 0
    for (let i = 0; i < sphere.count; i++) {
      if (sphere.inner[i]) continue
      outer++
      if (sphere.position[i * 3 + 1] > 0) above++
    }
    expect(Math.abs(above - outer / 2)).toBeLessThanOrEqual(1)
  })
})

describe("OrbModel behaviour", () => {
  it("fires the flash only when moving from a resting state into an active one", () => {
    const model = new OrbModel(200)
    run(model, "idle", null, 10)
    expect(model.flash).toBe(0)
    model.step(1 / 30, "listening", 0)
    expect(model.flash).toBeGreaterThan(0.9)
    run(model, "thinking", null, 3)
    // Active to active does not fire it again; it only decays.
    const decayed = model.flash
    run(model, "speaking", null, 1)
    expect(model.flash).toBeLessThan(decayed)
    run(model, "idle", null, 90)
    expect(model.flash).toBeLessThan(0.01)
    // Active to resting never fires.
    run(model, "listening", 0.5, 90)
    run(model, "muted", null, 1)
    expect(model.flash).toBeLessThan(0.01)
  })

  it("does not flash on the very first step", () => {
    const model = new OrbModel(200)
    model.step(1 / 30, "listening", 0.5)
    expect(model.flash).toBe(0)
  })

  it("fires the flash from error and offline as well", () => {
    for (const rest of ["offline", "muted", "error"] as const) {
      const model = new OrbModel(100)
      run(model, rest, null, 3)
      model.step(1 / 30, "speaking", null)
      expect(model.flash).toBeGreaterThan(0.9)
    }
  })

  it("follows the level while listening", () => {
    const loud = new OrbModel(100)
    const quiet = new OrbModel(100)
    run(loud, "listening", 0.9, 30)
    run(quiet, "listening", 0.1, 30)
    expect(loud.voice).toBeGreaterThan(quiet.voice)
    expect(loud.voice).toBeGreaterThan(0.8)
  })

  it("ignores the level while thinking", () => {
    const model = new OrbModel(100)
    run(model, "thinking", 1, 60)
    expect(model.voice).toBeLessThan(0.001)
    expect(model.think).toBeGreaterThan(0.9)
  })

  it("stays calm while listening with no level, but speaks with a synthetic envelope", () => {
    const listening = new OrbModel(100)
    run(listening, "listening", null, 60)
    expect(listening.voice).toBeLessThan(0.001)
    const speaking = new OrbModel(100)
    run(speaking, "speaking", null, 60)
    expect(speaking.voice).toBeGreaterThan(0.1)
  })

  it("rises faster than it falls", () => {
    const rising = new OrbModel(50)
    rising.step(0.05, "listening", 1)
    const rise = rising.voice
    const falling = new OrbModel(50)
    run(falling, "listening", 1, 60)
    const before = falling.voice
    falling.step(0.05, "listening", 0)
    expect(rise).toBeGreaterThan(0.5)
    expect(before - falling.voice).toBeLessThan(rise)
  })

  it("approaches the look of the state and turns", () => {
    const model = new OrbModel(100)
    run(model, "speaking", 0.5, 150)
    expect(model.glow).toBeCloseTo(lookFor("speaking").glow, 2)
    expect(model.spin).toBeGreaterThan(1)
  })

  it("clamps a long frame gap and ignores bad numbers", () => {
    const model = new OrbModel(100)
    model.step(5, "idle", null)
    expect(model.time).toBeCloseTo(0.1, 9)
    model.step(-1, "idle", null)
    model.step(Number.NaN, "idle", Number.NaN)
    model.step(1 / 30, "listening", 7)
    expect(model.voice).toBeLessThanOrEqual(1)
    for (const value of [model.glow, model.voice, model.think, model.flash, model.shimmer, model.spin, model.time]) {
      expect(Number.isFinite(value)).toBe(true)
    }
  })

  it("keeps every frame value finite and within range in every state", () => {
    for (const state of ORB_STATES) {
      const model = new OrbModel(400)
      run(model, "idle", null, 3)
      run(model, state, 0.8, 40)
      const frame = model.sphereFrame()
      for (let i = 0; i < frame.count; i++) {
        expect(Number.isFinite(frame.x[i])).toBe(true)
        expect(Number.isFinite(frame.y[i])).toBe(true)
        expect(frame.depth[i]).toBeGreaterThanOrEqual(-1)
        expect(frame.depth[i]).toBeLessThanOrEqual(1)
        expect(frame.light[i]).toBeGreaterThanOrEqual(0)
        expect(frame.light[i]).toBeLessThanOrEqual(1)
      }
    }
  })

  it("keeps the points near the rest radius", () => {
    const model = new OrbModel(400)
    run(model, "speaking", 1, 60)
    const frame = model.sphereFrame()
    for (let i = 0; i < frame.count; i++) {
      const reach = Math.hypot(frame.x[i], frame.y[i])
      expect(reach).toBeLessThan(1.8)
    }
  })

  it("reuses the same buffers for every frame", () => {
    const model = new OrbModel(200)
    run(model, "listening", 0.5, 5)
    const first = model.sphereFrame()
    const x = first.x
    const light = first.light
    model.step(1 / 30, "listening", 0.5)
    const second = model.sphereFrame()
    expect(second).toBe(first)
    expect(second.x).toBe(x)
    expect(second.light).toBe(light)
  })

  it("lights the thinking band only on the outer sphere", () => {
    const thinking = new OrbModel(400)
    const idle = new OrbModel(400)
    run(thinking, "thinking", null, 60)
    run(idle, "idle", null, 60)
    const a = thinking.sphereFrame()
    let bright = 0
    for (let i = 0; i < a.count; i++) if (!a.inner[i] && a.light[i] > 0.9) bright++
    expect(bright).toBeGreaterThan(0)
  })

  it("reports resting only for a resting state with no flash left", () => {
    const model = new OrbModel(50)
    run(model, "idle", null, 3)
    expect(model.isResting("idle")).toBe(true)
    expect(model.isResting("listening")).toBe(false)
    model.step(1 / 30, "listening", 0)
    model.step(1 / 30, "idle", null)
    expect(model.isResting("idle")).toBe(false)
  })
})

describe("syntheticSpeechLevel", () => {
  it("stays within 0.25..0.8", () => {
    for (let t = 0; t < 30; t += 0.05) {
      const level = syntheticSpeechLevel(t)
      expect(level).toBeGreaterThanOrEqual(0.25)
      expect(level).toBeLessThanOrEqual(0.8)
    }
  })
})

describe("the looks", () => {
  it("cover every state, and resting states are the quiet ones", () => {
    for (const state of ORB_STATES) {
      expect(lookFor(state)).toBe(ORB_LOOKS[state])
      expect(lookFor(state).colour).toMatch(/^#[0-9a-f]{6}$/i)
    }
    expect([...RESTING_STATES].sort()).toEqual(["error", "idle", "muted", "offline"])
    for (const state of RESTING_STATES) expect(lookFor(state).voiceGain).toBe(0)
  })
})
