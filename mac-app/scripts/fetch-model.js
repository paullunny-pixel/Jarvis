// Downloads the Porcupine wake-word model (porcupine_params.pv, ~2MB) into
// assets/. Runs automatically on `npm install`; safe to re-run; skips if the
// file is already there.
const fs = require("fs");
const path = require("path");

const DEST_DIR = path.join(__dirname, "..", "assets");
const DEST = path.join(DEST_DIR, "porcupine_params.pv");
const URL =
  "https://raw.githubusercontent.com/Picovoice/porcupine/master/lib/common/porcupine_params.pv";

async function main() {
  if (fs.existsSync(DEST) && fs.statSync(DEST).size > 100_000) {
    console.log("porcupine_params.pv already present — nothing to do.");
    return;
  }
  fs.mkdirSync(DEST_DIR, { recursive: true });
  console.log("Downloading Porcupine model (~2MB)…");
  const res = await fetch(URL);
  if (!res.ok) throw new Error(`Download failed: HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(DEST, buf);
  console.log(`Saved ${buf.length} bytes to assets/porcupine_params.pv`);
}

main().catch((err) => {
  console.error("Model download failed:", err.message);
  console.error(
    "Wake word won't work until it exists — run `npm run fetch-model` again, " +
      "or download it manually from\n  " + URL + "\nand save it as assets/porcupine_params.pv"
  );
  // Don't fail the whole npm install over it.
});
