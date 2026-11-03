import { describe, expect, it } from "vitest"
import { MicError, openMic } from "@/phone/micCapture"

// Hand-made stand-ins for the browser's audio objects: they record what was connected and stopped.

class FakeNode {
  connected: FakeNode[] = []
  disconnected = false
  gain = { value: 1 }
  port: { onmessage: ((event: { data: unknown }) => void) | null } = { onmessage: null }
  connect(to: FakeNode) {
    this.connected.push(to)
    return to
  }
  disconnect() {
    this.disconnected = true
  }
}

class FakeTrack {
  readyState = "live"
  stop() {
    this.readyState = "ended"
  }
}

class FakeStream {
  tracks = [new FakeTrack()]
  getTracks() {
    return this.tracks
  }
  getAudioTracks() {
    return this.tracks
  }
}

function fakeContext(options: { failModule?: boolean; noWorklet?: boolean } = {}) {
  const source = new FakeNode()
  const gain = new FakeNode()
  const modules: string[] = []
  const ctx = {
    sampleRate: 48000,
    destination: new FakeNode(),
    audioWorklet: options.noWorklet
      ? undefined
      : {
          addModule: async (url: string) => {
            modules.push(url)
            if (options.failModule) throw new Error("load failed")
          },
        },
    createMediaStreamSource: () => source,
    createGain: () => gain,
  }
  return { ctx: ctx as unknown as AudioContext, source, gain, modules, raw: ctx }
}

function devices(result: FakeStream | Error) {
  const asked: unknown[] = []
  const media = {
    getUserMedia: async (constraints: unknown) => {
      asked.push(constraints)
      if (result instanceof Error) throw result
      return result
    },
  }
  return { media: media as unknown as MediaDevices, asked }
}

function named(name: string): Error {
  const error = new Error(name)
  error.name = name
  return error
}

const secure = { isSecureContext: true }

describe("openMic", () => {
  it("asks for a clean mono microphone and connects it silently to the destination", async () => {
    const { ctx, source, gain, modules, raw } = fakeContext()
    const { media, asked } = devices(new FakeStream())
    const node = new FakeNode()
    const mic = await openMic(ctx, media, { ...secure, makeNode: () => node as unknown as AudioWorkletNode })
    expect(asked).toEqual([
      { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } },
    ])
    expect(modules).toEqual(["/pcm-worklet.js"])
    expect(source.connected).toEqual([node])
    expect(node.connected).toEqual([gain])
    expect(gain.connected).toEqual([raw.destination])
    expect(gain.gain.value).toBe(0)
    expect(mic.rate).toBe(48000)
    expect(mic.isLive()).toBe(true)
  })

  it("delivers the worklet's chunks to onChunk until it is cleared or closed", async () => {
    const { ctx } = fakeContext()
    const { media } = devices(new FakeStream())
    const node = new FakeNode()
    const mic = await openMic(ctx, media, { ...secure, makeNode: () => node as unknown as AudioWorkletNode })
    const seen: Float32Array[] = []
    node.port.onmessage?.({ data: new Float32Array(2) })
    mic.onChunk = (chunk) => seen.push(chunk)
    node.port.onmessage?.({ data: new Float32Array([1, 2]) })
    expect(seen.length).toBe(1)
    expect(Array.from(seen[0])).toEqual([1, 2])
    mic.onChunk = null
    node.port.onmessage?.({ data: new Float32Array(2) })
    expect(seen.length).toBe(1)
  })

  it("releases the microphone when closed, and closing twice is harmless", async () => {
    const { ctx, source, gain } = fakeContext()
    const stream = new FakeStream()
    const { media } = devices(stream)
    const node = new FakeNode()
    const mic = await openMic(ctx, media, { ...secure, makeNode: () => node as unknown as AudioWorkletNode })
    mic.onChunk = () => {
      throw new Error("must not be called after close")
    }
    mic.close()
    mic.close()
    expect(stream.tracks[0].readyState).toBe("ended")
    expect([source, node, gain].every((n) => n.disconnected)).toBe(true)
    expect(node.port.onmessage).toBeNull()
    expect(mic.isLive()).toBe(false)
  })

  it("is no longer live when the browser ends the track", async () => {
    const { ctx } = fakeContext()
    const stream = new FakeStream()
    const { media } = devices(stream)
    const mic = await openMic(ctx, media, { ...secure, makeNode: () => new FakeNode() as unknown as AudioWorkletNode })
    stream.tracks[0].readyState = "ended"
    expect(mic.isLive()).toBe(false)
  })

  it("loads the worklet once per context", async () => {
    const { ctx, modules } = fakeContext()
    const make = () => new FakeNode() as unknown as AudioWorkletNode
    await openMic(ctx, devices(new FakeStream()).media, { ...secure, makeNode: make })
    await openMic(ctx, devices(new FakeStream()).media, { ...secure, makeNode: make })
    expect(modules.length).toBe(1)
    const other = fakeContext()
    await openMic(other.ctx, devices(new FakeStream()).media, { ...secure, makeNode: make })
    expect(other.modules.length).toBe(1)
  })

  it("refuses on an insecure page before asking for anything", async () => {
    const { ctx } = fakeContext()
    const { media, asked } = devices(new FakeStream())
    await expect(openMic(ctx, media, { isSecureContext: false })).rejects.toMatchObject({ reason: "insecure" })
    expect(asked.length).toBe(0)
  })

  it("says unsupported when there is no getUserMedia", async () => {
    const { ctx } = fakeContext()
    await expect(openMic(ctx, undefined, secure)).rejects.toMatchObject({ reason: "unsupported" })
    await expect(openMic(ctx, {} as MediaDevices, secure)).rejects.toMatchObject({ reason: "unsupported" })
  })

  it("maps the browser's refusals", async () => {
    const cases: [string, string][] = [
      ["NotAllowedError", "denied"],
      ["SecurityError", "denied"],
      ["NotFoundError", "missing"],
      ["OverconstrainedError", "missing"],
      ["NotReadableError", "missing"],
      ["TypeError", "unsupported"],
    ]
    for (const [name, reason] of cases) {
      const { ctx } = fakeContext()
      const { media } = devices(named(name))
      const failure = await openMic(ctx, media, secure).catch((e) => e)
      expect(failure).toBeInstanceOf(MicError)
      expect(failure.reason, name).toBe(reason)
      expect(failure.message.length).toBeGreaterThan(10)
    }
  })

  it("stops the microphone and says unsupported when the worklet cannot load", async () => {
    for (const options of [{ failModule: true }, { noWorklet: true }]) {
      const { ctx } = fakeContext(options)
      const stream = new FakeStream()
      const { media } = devices(stream)
      await expect(openMic(ctx, media, secure)).rejects.toMatchObject({ reason: "unsupported" })
      expect(stream.tracks[0].readyState).toBe("ended")
    }
  })

  it("tries the worklet again after a failed load", async () => {
    const first = fakeContext({ failModule: true })
    await openMic(first.ctx, devices(new FakeStream()).media, secure).catch(() => undefined)
    first.raw.audioWorklet = { addModule: async (url: string) => void first.modules.push(url) }
    const mic = await openMic(first.ctx, devices(new FakeStream()).media, {
      ...secure,
      makeNode: () => new FakeNode() as unknown as AudioWorkletNode,
    })
    expect(mic.isLive()).toBe(true)
    expect(first.modules.length).toBe(2)
  })
})
