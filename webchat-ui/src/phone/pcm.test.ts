import { describe, expect, it } from "vitest"
import {
  Downsampler,
  Endpointer,
  MAX_RECORDING_SEC,
  MAX_VOICE_BYTES,
  TARGET_RATE,
  floatToPcm16,
  levelFromRms,
  pcm16ToBytes,
  rmsOf,
  type EndpointResult,
} from "@/phone/pcm"

function sine(rate: number, seconds: number, hz: number, amplitude = 0.5): Float32Array {
  const out = new Float32Array(Math.round(rate * seconds))
  for (let i = 0; i < out.length; i++) out[i] = amplitude * Math.sin((2 * Math.PI * hz * i) / rate)
  return out
}

function concat(parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const part of parts) {
    out.set(part, at)
    at += part.length
  }
  return out
}

describe("constants", () => {
  it("match the PC's limits", () => {
    expect(TARGET_RATE).toBe(16000)
    expect(MAX_RECORDING_SEC).toBe(120)
    expect(MAX_VOICE_BYTES).toBe(3872000)
  })
})

describe("levelFromRms", () => {
  it("reads silence as 0 and a full-scale signal as 1", () => {
    expect(levelFromRms(0)).toBe(0)
    expect(levelFromRms(-1)).toBe(0)
    expect(levelFromRms(Number.NaN)).toBe(0)
    expect(levelFromRms(1)).toBe(1)
    expect(levelFromRms(5)).toBe(1)
  })

  it("puts -52 dB at 0, -6 dB at 1 and -29 dB halfway, as the desktop does", () => {
    expect(levelFromRms(10 ** (-52 / 20))).toBeCloseTo(0, 9)
    expect(levelFromRms(10 ** (-6 / 20))).toBeCloseTo(1, 9)
    expect(levelFromRms(10 ** (-29 / 20))).toBeCloseTo(0.5, 9)
    expect(levelFromRms(10 ** (-80 / 20))).toBe(0)
  })

  it("scales to the given full scale", () => {
    expect(levelFromRms(16384, 32768)).toBeCloseTo(levelFromRms(0.5), 9)
    expect(levelFromRms(1, 0)).toBe(0)
  })
})

describe("rmsOf", () => {
  it("is 0 for nothing and the root mean square otherwise", () => {
    expect(rmsOf(new Float32Array(0))).toBe(0)
    expect(rmsOf(new Float32Array([0.5, -0.5, 0.5, -0.5]))).toBeCloseTo(0.5, 6)
    expect(rmsOf(sine(16000, 1, 440, 1))).toBeCloseTo(Math.SQRT1_2, 3)
  })
})

describe("Downsampler", () => {
  it("passes 16 kHz through unchanged", () => {
    const input = sine(16000, 0.1, 300)
    const out = new Downsampler(16000).push(input)
    expect(Array.from(out)).toEqual(Array.from(input))
    expect(out).not.toBe(input)
  })

  it("makes a third of the samples from 48 kHz and averages each span", () => {
    const input = new Float32Array([1, 2, 3, 4, 5, 6, 7, 8, 9])
    const out = new Downsampler(48000).push(input)
    expect(Array.from(out)).toEqual([2, 5, 8])
  })

  it("keeps a steady level steady, at 44.1 kHz too", () => {
    for (const rate of [48000, 44100, 32000, 22050]) {
      const out = new Downsampler(rate).push(new Float32Array(rate).fill(0.25))
      expect(Math.abs(out.length - 16000)).toBeLessThanOrEqual(1)
      for (const value of out) expect(value).toBeCloseTo(0.25, 5)
    }
  })

  it("keeps a tone's loudness and frequency", () => {
    const out = new Downsampler(44100).push(sine(44100, 1, 440))
    expect(rmsOf(out)).toBeGreaterThan(0.34)
    expect(rmsOf(out)).toBeLessThan(0.36)
    let crossings = 0
    for (let i = 1; i < out.length; i++) if (out[i - 1] < 0 && out[i] >= 0) crossings++
    expect(Math.abs(crossings - 440)).toBeLessThanOrEqual(2)
  })

  it("gives the same samples however the stream is cut", () => {
    for (const rate of [48000, 44100, 8000]) {
      const input = sine(rate, 0.5, 523)
      const whole = new Downsampler(rate).push(input)
      const cut = new Downsampler(rate)
      const parts: Float32Array[] = []
      for (let at = 0, size = 1; at < input.length; at += size, size = (size * 7 + 3) % 311 + 1) {
        parts.push(cut.push(input.subarray(at, Math.min(input.length, at + size))))
      }
      const joined = concat(parts)
      expect(joined.length).toBe(whole.length)
      for (let i = 0; i < whole.length; i++) expect(Math.abs(joined[i] - whole[i])).toBeLessThan(1e-5)
    }
  })

  it("returns nothing for an empty chunk", () => {
    expect(new Downsampler(48000).push(new Float32Array(0)).length).toBe(0)
    expect(new Downsampler(8000).push(new Float32Array(0)).length).toBe(0)
  })

  it("interpolates linearly below 16 kHz", () => {
    const out = new Downsampler(8000).push(new Float32Array([0, 1, 2, 3]))
    // Output positions 0, 0.5, 1, 1.5 ... in input samples, kept while inside the chunk.
    expect(Array.from(out).map((v) => Math.round(v * 100) / 100)).toEqual([0, 0.5, 1, 1.5, 2, 2.5, 3])
  })

  it("rejects an impossible rate", () => {
    expect(() => new Downsampler(0)).toThrow(RangeError)
    expect(() => new Downsampler(Number.NaN)).toThrow(RangeError)
  })
})

describe("floatToPcm16", () => {
  it("scales the ends to the full 16-bit range", () => {
    expect(Array.from(floatToPcm16(new Float32Array([0, 1, -1])))).toEqual([0, 32767, -32768])
  })

  it("clips instead of wrapping and rounds", () => {
    expect(Array.from(floatToPcm16(new Float32Array([2, -3, 0.5, -0.5])))).toEqual([32767, -32768, 16384, -16384])
    expect(Array.from(floatToPcm16(new Float32Array([Number.NaN, Infinity, -Infinity])))).toEqual([0, 32767, -32768])
  })
})

describe("pcm16ToBytes", () => {
  it("writes little-endian whatever the platform", () => {
    const bytes = pcm16ToBytes([Int16Array.from([1, -2]), Int16Array.from([0x1234])])
    expect(Array.from(bytes)).toEqual([0x01, 0x00, 0xfe, 0xff, 0x34, 0x12])
  })

  it("is empty for no chunks", () => {
    expect(pcm16ToBytes([]).length).toBe(0)
  })
})

// Level scripts for the end-point: [level, seconds] runs fed in 50 ms steps.
const DT = 0.05
function drive(end: Endpointer, runs: [number, number][]): EndpointResult[] {
  const seen: EndpointResult[] = []
  for (const [level, seconds] of runs) {
    const steps = Math.round(seconds / DT)
    for (let i = 0; i < steps; i++) seen.push(end.push(level, DT))
  }
  return seen
}

describe("Endpointer", () => {
  it("ends a request after speech and 1.2 s of quiet", () => {
    const end = new Endpointer()
    const quiet = drive(end, [[0.05, 0.6]])
    expect(new Set(quiet)).toEqual(new Set(["listening"]))
    const spoken = drive(end, [[0.7, 0.5], [0.1, 0.3], [0.65, 0.5]])
    expect(spoken).not.toContain("end")
    expect(spoken[spoken.length - 1]).toBe("speech")
    const after = drive(end, [[0.05, 1.5]])
    expect(after.indexOf("end")).toBeGreaterThanOrEqual(0)
    // About 1.2 s of quiet, not sooner.
    expect(after.indexOf("end") + 1).toBeGreaterThanOrEqual(23)
    expect(after.indexOf("end") + 1).toBeLessThanOrEqual(25)
  })

  it("does not end during a short pause between words", () => {
    const end = new Endpointer()
    drive(end, [[0.05, 0.4], [0.7, 0.4]])
    const pause = drive(end, [[0.05, 1.0], [0.7, 0.3]])
    expect(pause).not.toContain("end")
  })

  it("gives up with no_speech when only noise is heard", () => {
    const end = new Endpointer()
    const seen = drive(end, [[0.06, 9]])
    const at = seen.indexOf("no_speech")
    expect(at).toBeGreaterThan(0)
    expect(seen.slice(0, at).every((r) => r === "listening")).toBe(true)
    // About 8 s.
    expect((at + 1) * DT).toBeGreaterThanOrEqual(7.9)
    expect((at + 1) * DT).toBeLessThanOrEqual(8.1)
  })

  it("does not count a short click as speech", () => {
    const end = new Endpointer()
    const seen = drive(end, [[0.05, 0.5], [0.95, 0.05], [0.05, 0.5], [0.95, 0.05], [0.05, 8]])
    expect(seen).not.toContain("speech")
    expect(seen).not.toContain("end")
    expect(seen).toContain("no_speech")
    expect(end.speechHeard).toBe(false)
  })

  it("counts speech once it has lasted long enough", () => {
    const end = new Endpointer()
    drive(end, [[0.05, 0.4]])
    const seen = drive(end, [[0.7, 0.1], [0.7, 0.1]])
    expect(seen.slice(-1)[0]).toBe("speech")
    expect(end.speechHeard).toBe(true)
  })

  it("stops at the cap", () => {
    const end = new Endpointer({ maxSec: 3 })
    const seen = drive(end, [[0.05, 0.4], [0.7, 5]])
    const at = seen.indexOf("max")
    expect(at).toBeGreaterThan(0)
    expect((at + 1) * DT).toBeGreaterThanOrEqual(2.9)
    expect((at + 1) * DT).toBeLessThanOrEqual(3.1)
    expect(seen.slice(at).every((r) => r === "max")).toBe(true)
  })

  it("defaults the cap to 120 s", () => {
    const end = new Endpointer({ noSpeechSec: 1000 })
    let last: EndpointResult = "listening"
    for (let t = 0; t < 119; t += 1) last = end.push(0.05, 1)
    expect(last).toBe("listening")
    expect(end.push(0.05, 1)).toBe("max")
  })

  it("keeps returning a terminal result", () => {
    const end = new Endpointer()
    drive(end, [[0.05, 0.4], [0.7, 0.5], [0.05, 2]])
    expect(end.push(0.9, DT)).toBe("end")
    expect(end.push(0.9, DT)).toBe("end")
    const lost = new Endpointer()
    drive(lost, [[0.05, 9]])
    expect(lost.push(0.9, DT)).toBe("no_speech")
  })

  it("still works in a loud room", () => {
    const end = new Endpointer()
    drive(end, [[0.4, 0.5]])
    expect(end.floor).toBeCloseTo(0.4, 6)
    expect(end.threshold).toBeCloseTo(0.52, 6)
    // Noise alone is not speech.
    expect(drive(end, [[0.45, 1]])).not.toContain("speech")
    expect(drive(end, [[0.9, 0.5]]).slice(-1)[0]).toBe("speech")
    const after = drive(end, [[0.4, 1.5]])
    expect(after).toContain("end")
  })

  it("never lets the floor rise out of reach of someone who starts speaking at once", () => {
    const end = new Endpointer()
    const seen = drive(end, [[0.9, 1]])
    expect(seen.slice(-1)[0]).toBe("speech")
    expect(end.floor).toBeLessThanOrEqual(0.5)
  })

  it("uses at least 0.2 as the threshold in a very quiet room", () => {
    const end = new Endpointer()
    drive(end, [[0, 0.5]])
    expect(end.floor).toBe(0)
    expect(end.threshold).toBeCloseTo(0.2, 9)
    expect(drive(end, [[0.19, 1]])).not.toContain("speech")
  })

  it("holds speech while the level stays above 80% of the threshold", () => {
    const end = new Endpointer()
    drive(end, [[0, 0.5], [0.6, 0.3]])
    // Threshold 0.2, so 0.17 is above 0.16 and still speech.
    expect(drive(end, [[0.17, 3]])).not.toContain("end")
    expect(drive(end, [[0.15, 1.5]])).toContain("end")
  })

  it("ignores bad numbers", () => {
    const end = new Endpointer()
    expect(end.push(Number.NaN, DT)).toBe("listening")
    expect(end.push(0.5, Number.NaN)).toBe("listening")
    expect(end.push(0.5, -1)).toBe("listening")
  })

  it("honours the options", () => {
    const end = new Endpointer({ calibrateSec: 0.1, minSpeechSec: 0.05, endSilenceSec: 0.3, noSpeechSec: 1 })
    drive(end, [[0.05, 0.1], [0.6, 0.2]])
    expect(end.speechHeard).toBe(true)
    expect(drive(end, [[0.05, 0.5]])).toContain("end")
    const idle = new Endpointer({ noSpeechSec: 1 })
    expect(drive(idle, [[0.05, 1.5]])).toContain("no_speech")
  })
})
