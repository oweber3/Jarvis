// The phone's own microphone: an open stream whose samples arrive as Float32Array chunks from an AudioWorklet.
// The audio goes nowhere on its own; whoever sets `onChunk` decides what happens to it.

export type MicFailure = "denied" | "missing" | "insecure" | "unsupported"

const MESSAGES: Record<MicFailure, string> = {
  denied: "Microphone access was denied. Allow it for this page in the browser's settings.",
  missing: "No microphone could be opened on this device.",
  insecure: "The microphone needs a secure (HTTPS) connection to the PC.",
  unsupported: "This browser cannot record from the microphone.",
}

export class MicError extends Error {
  constructor(readonly reason: MicFailure) {
    super(MESSAGES[reason])
    this.name = "MicError"
  }
}

export const WORKLET_URL = "/pcm-worklet.js"
export const WORKLET_NAME = "pcm-capture"

export type MicOptions = {
  /** Whether the page is a secure context (default: the browser's answer). */
  isSecureContext?: boolean
  /** Builds the worklet node (default: `new AudioWorkletNode`). */
  makeNode?: (ctx: AudioContext) => AudioWorkletNode
  workletUrl?: string
}

const loaded = new WeakMap<object, Promise<void>>()

function loadWorklet(ctx: AudioContext, url: string): Promise<void> {
  const known = loaded.get(ctx)
  if (known) return known
  if (!ctx.audioWorklet) return Promise.reject(new MicError("unsupported"))
  const pending = ctx.audioWorklet.addModule(url).catch(() => {
    loaded.delete(ctx)
    throw new MicError("unsupported")
  })
  loaded.set(ctx, pending)
  return pending
}

function failureFor(error: unknown): MicError {
  if (error instanceof MicError) return error
  const name = typeof error === "object" && error !== null ? (error as { name?: unknown }).name : undefined
  if (name === "NotAllowedError" || name === "SecurityError") return new MicError("denied")
  if (name === "NotFoundError" || name === "OverconstrainedError" || name === "NotReadableError") {
    return new MicError("missing")
  }
  if (name === "TypeError") return new MicError("unsupported")
  return new MicError("missing")
}

function defaultNode(ctx: AudioContext): AudioWorkletNode {
  return new AudioWorkletNode(ctx, WORKLET_NAME, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
}

export class MicStream {
  private handler: ((chunk: Float32Array) => void) | null = null
  private closed = false

  constructor(
    /** The sample rate of the chunks (the audio context's). */
    readonly rate: number,
    private readonly stream: MediaStream,
    private readonly nodes: AudioNode[],
    private readonly port: MessagePort,
  ) {
    port.onmessage = (event: MessageEvent) => {
      if (!this.closed && this.handler) this.handler(event.data as Float32Array)
    }
  }

  set onChunk(handler: ((chunk: Float32Array) => void) | null) {
    this.handler = handler
  }

  get onChunk(): ((chunk: Float32Array) => void) | null {
    return this.handler
  }

  /** Still open and the browser is still giving audio (a revoked permission or an unplugged microphone ends it). */
  isLive(): boolean {
    if (this.closed) return false
    const tracks = this.stream.getAudioTracks()
    return tracks.length > 0 && tracks.every((track) => track.readyState === "live")
  }

  /** Release the microphone. Safe to call again. */
  close(): void {
    if (this.closed) return
    this.closed = true
    this.handler = null
    this.port.onmessage = null
    for (const node of this.nodes) {
      try {
        node.disconnect()
      } catch {
        // Already disconnected.
      }
    }
    for (const track of this.stream.getTracks()) track.stop()
  }
}

/** Ask for the microphone and start delivering its samples. Rejects with a `MicError`. */
export async function openMic(
  ctx: AudioContext,
  mediaDevices: MediaDevices | undefined = globalThis.navigator?.mediaDevices,
  options: MicOptions = {},
): Promise<MicStream> {
  const secure = options.isSecureContext ?? (globalThis as { isSecureContext?: boolean }).isSecureContext
  if (secure === false) throw new MicError("insecure")
  if (!mediaDevices || typeof mediaDevices.getUserMedia !== "function") throw new MicError("unsupported")

  let stream: MediaStream
  try {
    stream = await mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
  } catch (error) {
    throw failureFor(error)
  }

  try {
    await loadWorklet(ctx, options.workletUrl ?? WORKLET_URL)
    const source = ctx.createMediaStreamSource(stream)
    const node = (options.makeNode ?? defaultNode)(ctx)
    // The graph must reach the destination for the browser to run it, but it must stay silent.
    const mute = ctx.createGain()
    mute.gain.value = 0
    source.connect(node)
    node.connect(mute)
    mute.connect(ctx.destination)
    return new MicStream(ctx.sampleRate, stream, [source, node, mute], node.port)
  } catch (error) {
    for (const track of stream.getTracks()) track.stop()
    throw error instanceof MicError ? error : new MicError("unsupported")
  }
}
