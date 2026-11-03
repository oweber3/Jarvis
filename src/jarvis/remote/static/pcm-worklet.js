// Runs in the audio thread: hands the microphone's first channel to the page in blocks of about 2048 samples.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.block = new Float32Array(2048)
    this.filled = 0
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel) return true
    let at = 0
    while (at < channel.length) {
      const take = Math.min(channel.length - at, this.block.length - this.filled)
      this.block.set(channel.subarray(at, at + take), this.filled)
      this.filled += take
      at += take
      if (this.filled === this.block.length) {
        const out = this.block
        this.block = new Float32Array(2048)
        this.filled = 0
        this.port.postMessage(out, [out.buffer])
      }
    }
    return true
  }
}

registerProcessor("pcm-capture", PcmCapture)
