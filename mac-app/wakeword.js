// openWakeWord in Node (6 Aug): the official pretrained 'Hey Jarvis' model,
// run in-process via onnxruntime-node — no Python sidecar, no cloud, no key.
//
// The openWakeWord pipeline, faithfully ported from the reference library
// (github.com/dscripka/openWakeWord):
//   16 kHz mono PCM → 1280-sample (80 ms) chunks
//   → melspectrogram.onnx  ([1, 1280] → 5 frames × 32 mels, then x/10 + 2)
//   → embedding_model.onnx (sliding window of 76 mel frames, step 8 → [1, 96])
//   → hey_jarvis_v0.1.onnx (last 16 embeddings [1, 16, 96] → score 0..1)
const path = require("path");
const fs = require("fs");

const CHUNK = 1280;            // 80 ms at 16 kHz — openWakeWord's native step
const MEL_WINDOW = 76;         // mel frames per embedding
const MEL_STEP = 8;            // new mel frames between embeddings
const EMB_WINDOW = 16;         // embeddings per wake-model inference
const MEL_BUFFER_MAX = 160;    // keep ~10s of mel history, plenty

class OpenWakeWord {
  constructor(melSession, embSession, wakeSession, { threshold = 0.5, refractoryMs = 2000 } = {}) {
    this.mel = melSession;
    this.emb = embSession;
    this.wake = wakeSession;
    this.threshold = threshold;
    this.refractoryMs = refractoryMs;
    this.melBuffer = [];       // array of Float32Array(32)
    this.embBuffer = [];       // array of Float32Array(96)
    this.framesSinceEmb = 0;
    this.lastDetection = 0;
    this._pcmRemainder = new Float32Array(0);
    this._queue = Promise.resolve();   // serialize session.run calls
  }

  static modelPaths(dir) {
    return {
      mel: path.join(dir, "melspectrogram.onnx"),
      emb: path.join(dir, "embedding_model.onnx"),
      wake: path.join(dir, "hey_jarvis_v0.1.onnx"),
    };
  }

  static modelsPresent(dir) {
    const p = OpenWakeWord.modelPaths(dir);
    return [p.mel, p.emb, p.wake].every((f) => fs.existsSync(f));
  }

  static async load(dir, options = {}) {
    const ort = require("onnxruntime-node");
    const p = OpenWakeWord.modelPaths(dir);
    const opts = { logSeverityLevel: 3 };
    const [mel, emb, wake] = await Promise.all([
      ort.InferenceSession.create(p.mel, opts),
      ort.InferenceSession.create(p.emb, opts),
      ort.InferenceSession.create(p.wake, opts),
    ]);
    const engine = new OpenWakeWord(mel, emb, wake, options);
    engine._ort = ort;
    return engine;
  }

  // Feed any amount of 16 kHz mono float32 PCM. onDetect(score) fires on a
  // wake-word hit (threshold + refractory applied). Calls are serialized so
  // overlapping audio callbacks can't interleave the ONNX sessions.
  feed(pcm, onDetect) {
    const merged = new Float32Array(this._pcmRemainder.length + pcm.length);
    merged.set(this._pcmRemainder, 0);
    merged.set(pcm, this._pcmRemainder.length);
    let offset = 0;
    while (merged.length - offset >= CHUNK) {
      const chunk = merged.slice(offset, offset + CHUNK);
      offset += CHUNK;
      this._queue = this._queue
        .then(() => this._processChunk(chunk))
        .then((score) => {
          if (score !== null && score >= this.threshold) {
            const now = Date.now();
            if (now - this.lastDetection > this.refractoryMs) {
              this.lastDetection = now;
              onDetect(score);
            }
          }
        })
        .catch(() => {});   // one bad frame must never kill the listener
    }
    this._pcmRemainder = merged.slice(offset);
  }

  async _processChunk(chunk) {
    const ort = this._ort;
    // 1. melspectrogram: [1, 1280] → frames × 32 mels (openWakeWord applies
    //    x/10 + 2 before the embedding model — utils.py, melspec_transform)
    const melOut = await this.mel.run({
      [this.mel.inputNames[0]]: new ort.Tensor("float32", chunk, [1, CHUNK]),
    });
    const melData = melOut[this.mel.outputNames[0]].data;
    const frames = melData.length / 32;
    for (let f = 0; f < frames; f++) {
      const frame = new Float32Array(32);
      for (let m = 0; m < 32; m++) frame[m] = melData[f * 32 + m] / 10 + 2;
      this.melBuffer.push(frame);
      this.framesSinceEmb += 1;
    }
    if (this.melBuffer.length > MEL_BUFFER_MAX) {
      this.melBuffer.splice(0, this.melBuffer.length - MEL_BUFFER_MAX);
    }

    // 2. embeddings: window of 76 mel frames, stepping 8
    let newEmbedding = false;
    while (this.melBuffer.length >= MEL_WINDOW && this.framesSinceEmb >= MEL_STEP) {
      this.framesSinceEmb -= MEL_STEP;
      const window = this.melBuffer.slice(-MEL_WINDOW);
      const flat = new Float32Array(MEL_WINDOW * 32);
      window.forEach((fr, i) => flat.set(fr, i * 32));
      const embOut = await this.emb.run({
        [this.emb.inputNames[0]]: new ort.Tensor("float32", flat, [1, MEL_WINDOW, 32, 1]),
      });
      this.embBuffer.push(Float32Array.from(embOut[this.emb.outputNames[0]].data));
      if (this.embBuffer.length > EMB_WINDOW) this.embBuffer.shift();
      newEmbedding = true;
    }
    if (!newEmbedding || this.embBuffer.length < EMB_WINDOW) return null;

    // 3. wake model: [1, 16, 96] → sigmoid score
    const flat = new Float32Array(EMB_WINDOW * 96);
    this.embBuffer.forEach((e, i) => flat.set(e.subarray(0, 96), i * 96));
    const wakeOut = await this.wake.run({
      [this.wake.inputNames[0]]: new ort.Tensor("float32", flat, [1, EMB_WINDOW, 96]),
    });
    return Number(wakeOut[this.wake.outputNames[0]].data[0]);
  }

  reset() {
    this.melBuffer = [];
    this.embBuffer = [];
    this.framesSinceEmb = 0;
    this._pcmRemainder = new Float32Array(0);
  }
}

module.exports = { OpenWakeWord, CHUNK };
