/**
 * AudioWorklet processor for AES67 monitor browser playback.
 *
 * Receives binary float32 interleaved stereo frames via port messages
 * and writes them into a power-of-two ring buffer.  The process()
 * callback drains the ring buffer into the AudioContext output.
 *
 * Starts in "buffering" mode and waits for TARGET_FRAMES samples before
 * playing, to absorb network jitter without dropouts.
 */

const RING_SIZE    = 32768;   // must be power of 2 (~682 ms at 48 kHz)
const RING_MASK    = RING_SIZE - 1;
const TARGET_FRAMES = 4096;   // ~85 ms pre-roll before playback starts

class AES67AudioReceiver extends AudioWorkletProcessor {
  constructor() {
    super();

    this._left  = new Float32Array(RING_SIZE);
    this._right = new Float32Array(RING_SIZE);
    this._writeHead = 0;
    this._readHead  = 0;
    this._buffering = true;

    this.port.onmessage = ({ data }) => {
      // data: ArrayBuffer containing interleaved float32 stereo
      const pcm    = new Float32Array(data);
      const frames = pcm.length >>> 1;  // divide by 2

      for (let i = 0; i < frames; i++) {
        const pos = (this._writeHead + i) & RING_MASK;
        this._left[pos]  = pcm[i * 2];
        this._right[pos] = pcm[i * 2 + 1];
      }
      this._writeHead += frames;

      // Report buffer level to main thread periodically
      this.port.postMessage({ type: 'level', available: this._available });
    };
  }

  get _available() {
    return this._writeHead - this._readHead;
  }

  process(_inputs, outputs) {
    const out    = outputs[0];
    const L      = out[0];
    const R      = out[1] ?? out[0];
    const frames = L ? L.length : 128;

    if (this._buffering) {
      if (this._available >= TARGET_FRAMES) {
        this._buffering = false;
      } else {
        L?.fill(0);
        R?.fill(0);
        return true;
      }
    }

    if (this._available < frames) {
      // Buffer underrun — go back to buffering mode
      this._buffering = true;
      L?.fill(0);
      R?.fill(0);
      return true;
    }

    for (let i = 0; i < frames; i++) {
      const pos = (this._readHead + i) & RING_MASK;
      if (L) L[i] = this._left[pos];
      if (R) R[i] = this._right[pos];
    }
    this._readHead += frames;

    return true;
  }
}

registerProcessor('aes67-audio-receiver', AES67AudioReceiver);
