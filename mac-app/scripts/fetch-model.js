// Downloads the openWakeWord ONNX models (~2MB total) into assets/:
//   melspectrogram.onnx   — audio → mel features
//   embedding_model.onnx  — mel features → speech embeddings
//   hey_jarvis_v0.1.onnx  — the official pretrained 'Hey Jarvis' wake model
// Runs automatically on `npm install`; safe to re-run; skips files already
// present.
const fs = require("fs");
const path = require("path");

const DEST_DIR = path.join(__dirname, "..", "assets");
const BASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1";
const MODELS = ["melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"];

async function fetchModel(name) {
  const dest = path.join(DEST_DIR, name);
  if (fs.existsSync(dest) && fs.statSync(dest).size > 10_000) {
    console.log(`${name} already present — skipping.`);
    return;
  }
  console.log(`Downloading ${name}…`);
  const res = await fetch(`${BASE}/${name}`, { redirect: "follow" });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(dest, buf);
  console.log(`  saved ${buf.length} bytes`);
}

async function main() {
  fs.mkdirSync(DEST_DIR, { recursive: true });
  for (const name of MODELS) await fetchModel(name);
  console.log("Wake-word models ready.");
}

main().catch((err) => {
  console.error("Model download failed:", err.message);
  console.error(
    "The wake word won't work until all three exist in assets/ — run " +
      "`npm run fetch-model` again, or download them manually from\n  " +
      BASE + "\n(files: " + MODELS.join(", ") + ")"
  );
  // Don't fail the whole npm install over it.
});
