import { describe, expect, it } from "vitest"
import { PhonePlayer } from "@/phone/audioPlayer"

// A fake audio context: sources start and end on command, the analyser reports a chosen level.

class FakeSource {
  buffer: unknown = null
  started = 0
  stopped = 0
  onended: (() => void) | null = null
  connected: unknown[] = []
  connect(to: unknown) {
    this.connected.push(to)
  }
  disconnect() {}
  start() {
    this.started++
  }
  stop() {
    this.stopped++
  }
  end() {
    this.onended?.()
  }
}

class FakeAnalyser {
  fftSize = 2048
  fill = 0
  connected: unknown[] = []
  connect(to: unknown) {
    this.connected.push(to)
  }
  disconnect() {}
  getFloatTimeDomainData(into: Float32Array) {
    into.fill(this.fill)
  }
}

function fakeContext(options: { state?: string; failDecode?: boolean; resumeFails?: boolean } = {}) {
  const sources: FakeSource[] = []
  const analysers: FakeAnalyser[] = []
  const decoding: { resolve: (buffer: unknown) => void }[] = []
  let resumed = 0
  const ctx = {
    state: options.state ?? "running",
    sampleRate: 44100,
    destination: { name: "destination" },
    resume: async () => {
      resumed++
      if (options.resumeFails) throw new Error("not allowed")
      ctx.state = "running"
    },
    createBuffer: (channels: number, length: number, rate: number) => ({ channels, length, rate }),
    createBufferSource: () => {
      const source = new FakeSource()
      sources.push(source)
      return source
    },
    createAnalyser: () => {
      const analyser = new FakeAnalyser()
      analysers.push(analyser)
      return analyser
    },
    decodeAudioData: (wav: ArrayBuffer) => {
      if (options.failDecode) return Promise.reject(new Error("bad data"))
      return new Promise((resolve) => decoding.push({ resolve: () => resolve({ length: wav.byteLength }) }))
    },
  }
  return { ctx: ctx as unknown as AudioContext, raw: ctx, sources, analysers, decoding, resumed: () => resumed }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
const wav = () => new ArrayBuffer(64)

async function started(f: ReturnType<typeof fakeContext>, player: PhonePlayer) {
  const done = player.play(wav())
  await tick()
  f.decoding[f.decoding.length - 1].resolve(null)
  await tick()
  return done
}

describe("PhonePlayer", () => {
  it("unlock resumes a suspended context and plays one silent sample, and is safe to repeat", async () => {
    const f = fakeContext({ state: "suspended" })
    const player = new PhonePlayer(f.ctx)
    await player.unlock()
    expect(f.resumed()).toBe(1)
    expect(f.sources.length).toBe(1)
    expect(f.sources[0].started).toBe(1)
    expect((f.sources[0].buffer as { length: number }).length).toBe(1)
    await player.unlock()
    expect(f.resumed()).toBe(1)
    expect(f.sources.length).toBe(2)
  })

  it("unlock does not throw when the browser refuses", async () => {
    const f = fakeContext({ state: "suspended", resumeFails: true })
    await expect(new PhonePlayer(f.ctx).unlock()).resolves.toBeUndefined()
  })

  it("plays through an analyser to the destination and resolves when the audio ends", async () => {
    const f = fakeContext()
    const player = new PhonePlayer(f.ctx)
    let settled = false
    const done = started(f, player).then(() => (settled = true))
    await tick()
    await tick()
    expect(player.playing).toBe(true)
    expect(f.analysers[0].fftSize).toBe(1024)
    expect(f.sources[0].connected).toEqual([f.analysers[0]])
    expect(f.analysers[0].connected).toEqual([f.raw.destination])
    expect(f.sources[0].started).toBe(1)
    expect(settled).toBe(false)
    f.sources[0].end()
    await done
    expect(settled).toBe(true)
    expect(player.playing).toBe(false)
  })

  it("stop ends playback and resolves the pending play", async () => {
    const f = fakeContext()
    const player = new PhonePlayer(f.ctx)
    const done = started(f, player)
    await tick()
    player.stop()
    await done
    expect(f.sources[0].stopped).toBe(1)
    expect(player.playing).toBe(false)
    player.stop()
    player.stop()
    expect(f.sources[0].stopped).toBe(1)
  })

  it("stop before the decode finishes means nothing plays", async () => {
    const f = fakeContext()
    const player = new PhonePlayer(f.ctx)
    const done = player.play(wav())
    await tick()
    player.stop()
    f.decoding[0].resolve(null)
    await done
    expect(f.sources.length).toBe(0)
    expect(player.playing).toBe(false)
  })

  it("starting a new reply stops the one playing", async () => {
    const f = fakeContext()
    const player = new PhonePlayer(f.ctx)
    const first = started(f, player)
    await tick()
    const second = started(f, player)
    await first
    expect(f.sources[0].stopped).toBe(1)
    await tick()
    expect(player.playing).toBe(true)
    f.sources[1].end()
    await second
    expect(player.playing).toBe(false)
  })

  it("rejects with a clear error when the audio cannot be decoded", async () => {
    const f = fakeContext({ failDecode: true })
    const player = new PhonePlayer(f.ctx)
    await expect(player.play(wav())).rejects.toThrow(/could not be played/)
    expect(player.playing).toBe(false)
  })

  it("reads the level only while playing", async () => {
    const f = fakeContext()
    const player = new PhonePlayer(f.ctx)
    expect(player.level()).toBeNull()
    const done = started(f, player)
    await tick()
    f.analysers[0].fill = 0
    expect(player.level()).toBe(0)
    f.analysers[0].fill = 0.5
    const level = player.level()
    expect(level).not.toBeNull()
    // 0.5 is -6 dB: full scale on the orb's level scale.
    expect(level as number).toBeCloseTo(1, 1)
    f.analysers[0].fill = 0.01
    expect(player.level() as number).toBeGreaterThan(0.2)
    expect(player.level() as number).toBeLessThan(0.6)
    f.sources[0].end()
    await done
    expect(player.level()).toBeNull()
  })

  it("resumes a suspended context before decoding", async () => {
    const f = fakeContext({ state: "suspended" })
    const player = new PhonePlayer(f.ctx)
    const done = started(f, player)
    await tick()
    expect(f.resumed()).toBe(1)
    player.stop()
    await done
  })
})
