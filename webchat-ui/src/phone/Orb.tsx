import { useEffect, useRef, useState, type CSSProperties } from "react"
import { ORB_PALETTE } from "@/generated/orbPalette"
import { OrbModel, PHONE_SPHERE_POINTS, lookFor, type OrbState } from "@/phone/orbModel"

// The phone's orb: a canvas port of OrbWidget (desktop_app/orb_widget.py). The sphere, its atmosphere and the
// wake flash are drawn as additive light on the canvas; the title and the state label are real text over it.
// It animates only while the page and the orb are visible, and rests at a lower rate while idle.

export type OrbProps = {
  state: OrbState
  /** Text in place of the state's own label, or null. */
  label: string | null
  /** The voice level 0..1 to follow right now, or null for none. */
  level: () => number | null
  /** A small version beside its label, for when the conversation needs the room. */
  compact: boolean
  onTap: () => void
  ariaLabel: string
}

type Rgb = [number, number, number]

const ACTIVE_INTERVAL_MS = 30
const RESTING_INTERVAL_MS = 62
const COLOUR_RATE = 6
const LEVELS = 7
const MAX_DPR = 2
const COMPACT_HEIGHT = 72
const TAU = Math.PI * 2
const WHITE = parseHex(ORB_PALETTE.white)

function parseHex(hex: string): Rgb {
  const value = Number.parseInt(hex.slice(1), 16)
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255]
}

function mix(a: Rgb, b: Rgb, t: number): Rgb {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
}

function rgba(colour: Rgb, alpha: number): string {
  const a = Math.min(1, Math.max(0, alpha))
  return `rgba(${Math.round(colour[0])},${Math.round(colour[1])},${Math.round(colour[2])},${a.toFixed(3)})`
}

type Layout = { w: number; h: number; header: number; footer: number; cx: number; cy: number; radius: number }

function layoutFor(w: number, h: number, compact: boolean): Layout {
  if (compact) {
    const radius = Math.max(10, (h / 2) * 0.72)
    return { w, h, header: 0, footer: 0, cx: h / 2 + 8, cy: h / 2, radius }
  }
  const footer = Math.min(40, h * 0.14)
  const header = Math.min(34, h * 0.12)
  // The rest radius leaves room for the surface to swell with the voice.
  const radius = Math.max(10, (Math.min(w, h - header - footer) / 2) * 0.72)
  return { w, h, header, footer, cx: w / 2, cy: header + (h - header - footer) / 2, radius }
}

/** Orders the points by sphere and brightness step without allocating per frame. */
class Batches {
  readonly order: Uint16Array
  readonly bounds = new Uint32Array(2 * LEVELS + 1)
  private readonly keys: Uint8Array
  private readonly cursor = new Uint32Array(2 * LEVELS)

  constructor(count: number) {
    this.order = new Uint16Array(count)
    this.keys = new Uint8Array(count)
  }

  sort(light: Float32Array, inner: Uint8Array, energy: number): void {
    const count = this.order.length
    const gain = 0.3 + 0.7 * energy
    this.bounds.fill(0)
    for (let i = 0; i < count; i++) {
      const lit = Math.min(1, Math.max(0, light[i] * gain))
      // Steps on a square-root scale, so dim points still show and the bright ones stay distinct.
      const step = Math.min(Math.floor(Math.sqrt(lit) * LEVELS), LEVELS - 1)
      const key = inner[i] * LEVELS + step
      this.keys[i] = key
      this.bounds[key + 1]++
    }
    for (let k = 0; k < 2 * LEVELS; k++) {
      this.bounds[k + 1] += this.bounds[k]
      this.cursor[k] = this.bounds[k]
    }
    for (let i = 0; i < count; i++) this.order[this.cursor[this.keys[i]]++] = i
  }
}

type Scene = {
  model: OrbModel
  batches: Batches
  colour: Rgb
  accent: Rgb
  labelWidth: number
  layout: Layout
  dpr: number
}

function energyOf(model: OrbModel): number {
  return Math.min(1, Math.max(0, model.glow + 0.55 * model.flash)) * model.shimmer
}

function paint(g: CanvasRenderingContext2D, scene: Scene, compact: boolean): void {
  const { model, colour, accent, layout } = scene
  const { w, h, cx, cy, radius } = layout
  g.setTransform(scene.dpr, 0, 0, scene.dpr, 0, 0)
  g.globalCompositeOperation = "source-over"
  g.clearRect(0, 0, w, h)
  if (w < 8 || h < 8) return
  const energy = energyOf(model)
  const voice = model.voice

  // Soft light round the sphere with a glowing rim; both swell with the voice.
  const reach = radius * (1 + 0.1 * voice) * 1.75
  const glow = g.createRadialGradient(cx, cy, 0, cx, cy, reach)
  glow.addColorStop(0, rgba(colour, (0.04 + 0.05 * voice) * energy))
  glow.addColorStop(0.45, rgba(colour, (0.07 + 0.07 * voice) * energy))
  glow.addColorStop(0.57, rgba(colour, (0.2 + 0.18 * voice) * energy))
  glow.addColorStop(0.75, rgba(colour, (0.05 + 0.06 * voice) * energy))
  glow.addColorStop(1, rgba(colour, 0))
  g.fillStyle = glow
  g.beginPath()
  g.arc(cx, cy, reach, 0, TAU)
  g.fill()

  // Every point as a dot of light, batched by sphere and brightness step; the brightest are whiter.
  g.globalCompositeOperation = "lighter"
  const frame = model.sphereFrame()
  const { batches } = scene
  batches.sort(frame.light, frame.inner, energy)
  const size = Math.max(0.6, radius / 150)
  for (let key = 0; key < 2 * LEVELS; key++) {
    const step = key % LEVELS
    const start = batches.bounds[key]
    const stop = batches.bounds[key + 1]
    if (step === 0 || start === stop) continue
    const base = key >= LEVELS ? accent : colour
    const whiten = Math.max(0, (step - 3) / (LEVELS - 4)) * 0.75
    g.fillStyle = rgba(mix(base, WHITE, whiten), Math.min(1, ((step + 0.6) / LEVELS) ** 2 * 1.15))
    const width = size * (1 + 0.2 * step)
    g.beginPath()
    if (step >= 5) {
      // Bright dots are round.
      const r = width / 2
      for (let n = start; n < stop; n++) {
        const i = batches.order[n]
        const px = cx + frame.x[i] * radius
        g.moveTo(px + r, cy + frame.y[i] * radius)
        g.arc(px, cy + frame.y[i] * radius, r, 0, TAU)
      }
    } else {
      // Dim dots are small squares.
      const half = width / 2
      for (let n = start; n < stop; n++) {
        const i = batches.order[n]
        g.rect(cx + frame.x[i] * radius - half, cy + frame.y[i] * radius - half, width, width)
      }
    }
    g.fill()
  }

  // Wake flash: a soft ring of light spreading out from the sphere's centre.
  const flash = model.flash
  if (flash >= 0.01) {
    const ring = radius * (0.15 + (1 - flash))
    const bright = mix(colour, WHITE, 0.6)
    g.lineWidth = Math.max(6, radius * 0.1)
    g.strokeStyle = rgba(colour, 0.3 * flash)
    g.beginPath()
    g.arc(cx, cy, ring, 0, TAU)
    g.stroke()
    g.lineWidth = 1.5 + 2 * flash
    g.strokeStyle = rgba(bright, 0.8 * flash)
    g.beginPath()
    g.arc(cx, cy, ring, 0, TAU)
    g.stroke()
  }

  g.globalCompositeOperation = "source-over"
  if (!compact) drawReadoutLines(g, scene, energy)
}

/** The HUD readout look: lines with end markers flanking the label. */
function drawReadoutLines(g: CanvasRenderingContext2D, scene: Scene, energy: number): void {
  const { layout, labelWidth, colour } = scene
  const { w, h, footer } = layout
  if (labelWidth <= 0) return
  const y = h - footer * 1.1 + footer / 2
  const gap = 12
  const length = Math.min(48, w * 0.12)
  g.lineWidth = 1
  g.strokeStyle = rgba(colour, 0.25 + 0.4 * energy)
  g.beginPath()
  for (const direction of [-1, 1]) {
    const near = w / 2 + direction * (labelWidth / 2 + gap)
    const far = near + direction * length
    if (far > 0 && far < w) {
      g.moveTo(near, y)
      g.lineTo(far, y)
      g.moveTo(far, y - 3)
      g.lineTo(far, y + 3)
    }
  }
  g.stroke()
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return
    const query = window.matchMedia("(prefers-reduced-motion: reduce)")
    setReduced(query.matches)
    const change = (event: MediaQueryListEvent) => setReduced(event.matches)
    query.addEventListener("change", change)
    return () => query.removeEventListener("change", change)
  }, [])
  return reduced
}

export function Orb({ state, label, level, compact, onTap, ariaLabel }: OrbProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const labelRef = useRef<HTMLSpanElement>(null)
  const [size, setSize] = useState({ w: 0, h: 0 })
  const reduced = useReducedMotion()

  const live = useRef({ state, level, compact })
  const scene = useRef<Scene | null>(null)
  if (scene.current === null) {
    const look = lookFor(state)
    const model = new OrbModel(PHONE_SPHERE_POINTS)
    scene.current = {
      model,
      batches: new Batches(model.sphere.count),
      colour: parseHex(look.colour),
      accent: parseHex(look.accent),
      labelWidth: 0,
      layout: layoutFor(0, 0, compact),
      dpr: 1,
    }
  }

  const look = lookFor(state)
  const shownLabel = label ?? look.label

  // Size to the container.
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const measure = () => {
      const box = root.getBoundingClientRect()
      setSize((old) => (old.w === box.width && old.h === box.height ? old : { w: box.width, h: box.height }))
    }
    measure()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(measure)
    observer.observe(root)
    return () => observer.disconnect()
  }, [])

  // Keep the canvas at the container's size in device pixels and draw what the model holds now.
  useEffect(() => {
    live.current = { state, level, compact }
  })
  useEffect(() => {
    const canvas = canvasRef.current
    const s = scene.current
    if (!canvas || !s) return
    s.dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1)
    s.layout = layoutFor(size.w, size.h, compact)
    s.labelWidth = compact ? 0 : (labelRef.current?.offsetWidth ?? 0)
    const wide = Math.max(1, Math.round(size.w * s.dpr))
    const high = Math.max(1, Math.round(size.h * s.dpr))
    if (canvas.width !== wide) canvas.width = wide
    if (canvas.height !== high) canvas.height = high
    const g = canvas.getContext("2d")
    if (g) paint(g, s, compact)
  }, [size, compact, shownLabel])

  // Reduced motion: no loop, one still frame in the state's colour whenever something changes.
  useEffect(() => {
    if (!reduced) return
    const canvas = canvasRef.current
    const s = scene.current
    const g = canvas?.getContext("2d")
    if (!s || !g) return
    for (let i = 0; i < 30; i++) s.model.step(0.1, state, null)
    s.colour = parseHex(look.colour)
    s.accent = parseHex(look.accent)
    paint(g, s, compact)
  }, [reduced, state, shownLabel, size, compact, look.colour, look.accent])

  // The animation loop, running only while the page and the orb can be seen.
  useEffect(() => {
    if (reduced) return
    const root = rootRef.current
    const canvas = canvasRef.current
    const s = scene.current
    if (!root || !canvas || !s) return
    const g = canvas.getContext("2d")
    if (!g) return

    let raf = 0
    let lastDraw: number | null = null
    let onScreen = true
    const canRun = () => onScreen && document.visibilityState === "visible"

    const frame = (now: number) => {
      raf = 0
      if (!canRun()) return
      const { state: current, level: read, compact: small } = live.current
      const resting = s.model.isResting(current)
      const interval = resting ? RESTING_INTERVAL_MS : ACTIVE_INTERVAL_MS
      if (lastDraw === null || now - lastDraw >= interval - 1) {
        const dt = lastDraw === null ? 1 / 30 : (now - lastDraw) / 1000
        lastDraw = now
        s.model.step(dt, current, read())
        const next = lookFor(current)
        const rate = 1 - Math.exp(-COLOUR_RATE * Math.min(dt, 0.1))
        s.colour = mix(s.colour, parseHex(next.colour), rate)
        s.accent = mix(s.accent, parseHex(next.accent), rate)
        paint(g, s, small)
      }
      raf = requestAnimationFrame(frame)
    }
    const start = () => {
      if (raf || !canRun()) return
      lastDraw = null
      raf = requestAnimationFrame(frame)
    }
    const stop = () => {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
    }
    const visibility = () => (canRun() ? start() : stop())

    document.addEventListener("visibilitychange", visibility)
    let observer: IntersectionObserver | null = null
    if (typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver((entries) => {
        onScreen = entries[entries.length - 1]?.isIntersecting ?? true
        visibility()
      })
      observer.observe(root)
    }
    start()
    return () => {
      stop()
      document.removeEventListener("visibilitychange", visibility)
      observer?.disconnect()
    }
  }, [reduced])

  const { header, footer } = layoutFor(size.w, size.h, compact)
  // Each line of text sits in a box that places it; the text itself is what is measured and styled.
  const titleBox: CSSProperties = compact
    ? { top: 12, left: COMPACT_HEIGHT + 8, height: 16 }
    : { top: 0, left: 0, right: 0, height: header * 1.2 }
  const labelBox: CSSProperties = compact
    ? { top: 30, left: COMPACT_HEIGHT + 8, height: 20 }
    : { top: size.h - footer * 1.1, left: 0, right: 0, height: footer }
  const titleSize = compact ? 10 : Math.max(10, Math.floor(header * 0.4))
  const labelSize = compact ? 12 : Math.max(9, Math.floor(footer * 0.3))
  const box = "pointer-events-none absolute flex items-center justify-center"

  return (
    <div
      ref={rootRef}
      className="relative select-none overflow-hidden rounded-2xl"
      style={{
        width: "100%",
        height: compact ? COMPACT_HEIGHT : "100%",
        minHeight: compact ? COMPACT_HEIGHT : 160,
        background: ORB_PALETTE.backdrop,
        fontFamily: "var(--hud-font-ui, system-ui, sans-serif)",
        touchAction: "manipulation",
      }}
    >
      <canvas
        ref={canvasRef}
        aria-hidden="true"
        className="absolute inset-0"
        style={{ width: "100%", height: "100%", display: "block" }}
      />
      <div className={box} style={titleBox} aria-hidden="true">
        <span
          className="font-semibold uppercase"
          style={{
            color: look.accent,
            opacity: 0.85,
            transition: "color 0.4s",
            whiteSpace: "nowrap",
            fontSize: titleSize,
            letterSpacing: compact ? 3 : 5,
          }}
        >
          J.A.R.V.I.S.
        </span>
      </div>
      <div className={box} style={labelBox}>
        <span
          ref={labelRef}
          role="status"
          className="uppercase"
          style={{
            color: look.colour,
            opacity: 0.9,
            transition: "color 0.4s",
            whiteSpace: "nowrap",
            fontSize: labelSize,
            letterSpacing: 3,
          }}
        >
          {shownLabel}
        </span>
      </div>
      <button
        type="button"
        aria-label={ariaLabel}
        onClick={onTap}
        className="absolute inset-0 cursor-pointer rounded-[inherit] bg-transparent focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-[var(--hud-accent-primary)]"
        style={{ minWidth: 44, minHeight: 44, border: 0, padding: 0 }}
      />
    </div>
  )
}
