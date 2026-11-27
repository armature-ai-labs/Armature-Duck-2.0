// AudioWorklet PCM tap: forwards 128-sample float frames to the main thread.
// Kept as a standalone module so iOS Safari can load it without blob: restrictions.
class PcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice());
    return true;
  }
}
registerProcessor('pcm-tap', PcmTap);
