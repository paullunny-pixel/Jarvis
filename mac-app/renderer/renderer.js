// Jarvis desktop renderer — the whole client lives here.
//
// Privacy model: openWakeWord runs LOCALLY (ONNX models via onnxruntime-node,
// right in this process). Not one byte of audio leaves the Mac until either
// the wake word fires, the talk button is pressed, or something is typed.
// The status dot always tells the truth.
const fs = require("fs");
const path = require("path");
const { ipcRenderer, shell } = require("electron");
const { OpenWakeWord } = require("../wakeword.js");

const MODEL_DIR = path.join(__dirname, "..", "assets");

// --- DOM ---
const feedEl = document.getElementById("feed");
const dotEl = document.getElementById("status-dot");
const statusEl = document.getElementById("status-text");
const talkBtn = document.getElementById("talk-btn");
const muteBtn = document.getElementById("mute-btn");
const pinBtn = document.getElementById("pin-btn");
const settingsBtn = document.getElementById("settings-btn");
const settingsPanel = document.getElementById("settings-panel");
const cfgUrl = document.getElementById("cfg-url");
const cfgSecret = document.getElementById("cfg-secret");
const cfgStatus = document.getElementById("cfg-status");
const typeForm = document.getElementById("type-form");
const typeInput = document.getElementById("type-input");

// --- state ---
let state = "idle"; // idle | listening | recording | thinking | speaking
let muted = false;
let pinned = false;
let inConversation = false;
let wakeEngine = null;
let wakeStream = null;
let wakeAudioCtx = null;
let wakeReady = false;

function cfg() {
  return {
    url: (localStorage.getItem("jarvis_url") || "").replace(/\/+$/, ""),
    secret: localStorage.getItem("jarvis_secret") || "",
  };
}

function setStatus(next, text) {
  state = next;
  dotEl.className = "dot " + (muted && next === "idle" ? "muted" : next === "idle" && wakeReady && !muted ? "listening" : next);
  statusEl.textContent = text;
}

function idleStatus() {
  if (muted) return setStatus("idle", "Wake word muted — button and typing still work");
  if (wakeReady) return setStatus("idle", 'Listening for "Hey Jarvis"…');
  return setStatus("idle", "Wake word off — models missing (run `npm run fetch-model`)");
}

function addMsg(kind, text) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;
  feedEl.appendChild(div);
  feedEl.scrollTop = feedEl.scrollHeight;
  return div;
}

function chime(freq = 880) {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.25);
    osc.onended = () => ctx.close();
  } catch (_) {}
}

// --- the Card Script panel (6 Aug brief) ---
// A rendered view of Jarvis's ACTUAL card-parsing config, served by the
// backend — nothing here is hard-coded, so a renamed Trello list shows up
// on the next sync. Standalone component: needs only a container + data,
// so the War Room can reuse it verbatim later.
function renderCardScript(container, data, stale) {
  const colours = data.colours || {};
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const sentence = (data.example_parts || [])
    .map((p) => `<span style="color:${esc(colours[p.colour] || "#9db0c6")}${p.colour === "slate" ? ";font-weight:400" : ""}">${esc(p.text)}</span>`)
    .join("");
  const legend = (data.fields || [])
    .map((f) => `
      <div class="lrow" data-key="${esc(f.key)}">
        <i class="sw" style="background:${esc(colours[f.colour] || "#4a5a70")}"></i>
        <div class="lk">${esc(f.label)}</div>
        <div class="lv">${esc(f.value)}</div>
      </div>`)
    .join("");
  container.innerHTML = `
    <div class="script${stale ? " stale" : ""}">
      <div class="script-head"><span>📇</span><b>Card script</b><i>${stale ? "reconnecting…" : "always here"}</i></div>
      <div class="script-body">
        <div class="say">“${sentence}”</div>
        <div class="legend">${legend}</div>
        <div class="script-note">${esc(data.defaults_note || "")
          .replace("Say nothing?", "<b>Say nothing?</b>")}</div>
      </div>
    </div>`;
}

const GRAMMAR_CACHE_KEY = "jarvis_card_grammar";
const GRAMMAR_STALE_MS = 24 * 60 * 60 * 1000;

async function loadCardScript() {
  const container = document.getElementById("card-script");
  if (!container) return;
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(GRAMMAR_CACHE_KEY) || "null"); } catch (_) {}
  if (cached) {
    renderCardScript(container, cached.data, Date.now() - cached.ts > GRAMMAR_STALE_MS);
  }
  try {
    const data = await backendFetch("/card-grammar");
    localStorage.setItem(GRAMMAR_CACHE_KEY, JSON.stringify({ ts: Date.now(), data }));
    renderCardScript(container, data, false);
  } catch (_) {
    // Backend unreachable: the cached render above stands; if there was no
    // cache at all the space stays empty rather than erroring (brief §7.3).
  }
}

function setConnected(ok, text) {
  const dot = document.getElementById("side-dot");
  const label = document.getElementById("side-conn");
  if (dot) dot.classList.toggle("ok", ok);
  if (label) label.textContent = text;
}

// --- backend ---
async function backendFetch(pathname, options = {}) {
  const { url, secret } = cfg();
  if (!url || !secret) throw new Error("not-configured");
  const res = await fetch(`${url}/desktop/${secret}${pathname}`, options);
  if (res.status === 403) throw new Error("bad-secret");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function pingBackend() {
  const data = await backendFetch("/ping");
  return data && data.ok;
}

// --- audio out ---
function playReply(audioB64) {
  return new Promise((resolve) => {
    if (!audioB64) return resolve();
    const audio = new Audio("data:audio/mpeg;base64," + audioB64);
    setStatus("speaking", "Jarvis is speaking…");
    audio.onended = resolve;
    audio.onerror = resolve;
    audio.play().catch(resolve);
  });
}

// --- recording with silence detection ---
// Records mic audio (webm/opus) until ~1.5s of silence after speech started.
// Gives up politely if nothing is said within 7s. Hard cap 90s.
function recordUtterance({ waitForSpeechMs = 7000, silenceMs = 1500, maxMs = 90000 } = {}) {
  return new Promise(async (resolve, reject) => {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      return reject(new Error("mic-denied"));
    }
    const chunks = [];
    const recorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);

    const started = Date.now();
    let lastLoud = 0;
    let spoke = false;
    let done = false;

    const finish = (cancelled) => {
      if (done) return;
      done = true;
      clearInterval(timer);
      try { recorder.state !== "inactive" && recorder.stop(); } catch (_) {}
      setTimeout(() => {
        stream.getTracks().forEach((t) => t.stop());
        ctx.close().catch(() => {});
        if (cancelled) return resolve(null);
        resolve(new Blob(chunks, { type: "audio/webm" }));
      }, 150); // let the last chunk flush
    };

    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.start(250);

    const timer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const now = Date.now();
      if (rms > 0.02) {
        spoke = true;
        lastLoud = now;
      }
      if (!spoke && now - started > waitForSpeechMs) return finish(true);
      if (spoke && now - lastLoud > silenceMs) return finish(false);
      if (now - started > maxMs) return finish(false);
    }, 100);
  });
}

// --- one full spoken turn: record → backend → feed → speak ---
async function voiceTurn() {
  setStatus("recording", "Listening to you…");
  let blob;
  try {
    blob = await recordUtterance();
  } catch (err) {
    addMsg("system", err.message === "mic-denied" ? "Microphone access denied — check System Settings → Privacy." : "Recording failed.");
    return false;
  }
  if (!blob || blob.size < 2000) {
    addMsg("system", "Didn't hear anything.");
    return false;
  }
  setStatus("thinking", "Jarvis is thinking…");
  let data;
  try {
    data = await backendFetch("/voice", {
      method: "POST",
      headers: { "Content-Type": "audio/webm" },
      body: blob,
    });
  } catch (err) {
    addMsg("system", describeError(err));
    return false;
  }
  if (data.transcript) addMsg("you", data.transcript);
  addMsg("jarvis", data.reply);
  await playReply(data.audio_b64);
  return Boolean(data.transcript);
}

function describeError(err) {
  if (err.message === "not-configured") return "Not connected — open Settings (⚙️) and fill in the backend URL + secret.";
  if (err.message === "bad-secret") return "The backend refused the desktop secret — re-check it in Settings (ask Jarvis on Telegram: “desktop setup”).";
  return "Couldn't reach Jarvis (" + err.message + ") — is the backend URL right?";
}

// --- mode 1: wake word one-shot ---
async function onWakeWord() {
  if (state !== "idle" || inConversation) return; // busy — ignore
  chime(880);
  addMsg("system", "— Hey Jarvis —");
  await voiceTurn();
  idleStatus();
}

// --- mode 2: turn-based conversation loop ---
async function conversationLoop() {
  let misses = 0;
  addMsg("system", "— conversation started —");
  while (inConversation) {
    const heard = await voiceTurn();
    if (!inConversation) break;
    misses = heard ? 0 : misses + 1;
    if (misses >= 2) {
      addMsg("system", "— conversation closed (all quiet) —");
      break;
    }
  }
  inConversation = false;
  talkBtn.textContent = "Start conversation";
  talkBtn.classList.remove("live");
  idleStatus();
}

talkBtn.addEventListener("click", () => {
  if (inConversation) {
    inConversation = false; // loop notices after the current turn
    talkBtn.textContent = "Ending…";
    return;
  }
  inConversation = true;
  talkBtn.textContent = "End conversation";
  talkBtn.classList.add("live");
  conversationLoop();
});

// --- typed input ---
typeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = typeInput.value.trim();
  if (!text) return;
  typeInput.value = "";
  addMsg("you", text);
  setStatus("thinking", "Jarvis is thinking…");
  try {
    const data = await backendFetch("/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    addMsg("jarvis", data.reply);
    await playReply(data.audio_b64);
  } catch (err) {
    addMsg("system", describeError(err));
  }
  idleStatus();
});

// --- wake word engine (openWakeWord via onnxruntime-node, fully local) ---
async function startWakeWord() {
  if (!OpenWakeWord.modelsPresent(MODEL_DIR)) {
    wakeReady = false;
    addMsg("system", "Wake-word models missing — run `npm run fetch-model` in mac-app/.");
    return idleStatus();
  }
  try {
    wakeEngine = await OpenWakeWord.load(MODEL_DIR, { threshold: 0.5, refractoryMs: 2000 });
    // Dedicated 16 kHz capture path — Chromium resamples the mic for us.
    wakeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    wakeAudioCtx = new AudioContext({ sampleRate: 16000 });
    const source = wakeAudioCtx.createMediaStreamSource(wakeStream);
    const onPcm = (pcm) => {
      if (wakeEngine && !muted) wakeEngine.feed(pcm, () => onWakeWord());
    };
    // AudioWorklet runs on the REAL-TIME AUDIO THREAD — immune to the
    // background throttling that froze the wake word whenever the window
    // lost focus (6 Aug). ScriptProcessor (main thread) stays as fallback.
    try {
      const workletSrc = `
        class WakeCapture extends AudioWorkletProcessor {
          process(inputs) {
            const ch = inputs[0] && inputs[0][0];
            if (ch && ch.length) this.port.postMessage(ch.slice(0));
            return true;
          }
        }
        registerProcessor("wake-capture", WakeCapture);
      `;
      const url = URL.createObjectURL(new Blob([workletSrc], { type: "application/javascript" }));
      await wakeAudioCtx.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(wakeAudioCtx, "wake-capture", {
        numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
      });
      node.port.onmessage = (e) => onPcm(e.data);
      source.connect(node);
      node.connect(wakeAudioCtx.destination);   // keeps the graph pulling; silent
    } catch (err) {
      const processor = wakeAudioCtx.createScriptProcessor(2048, 1, 1);
      processor.onaudioprocess = (e) => onPcm(Float32Array.from(e.inputBuffer.getChannelData(0)));
      source.connect(processor);
      processor.connect(wakeAudioCtx.destination);
    }
    wakeReady = true;
    idleStatus();
  } catch (err) {
    wakeReady = false;
    addMsg(
      "system",
      err.name === "NotAllowedError"
        ? "Microphone access denied — the wake word can't listen (System Settings → Privacy → Microphone)."
        : "Wake word failed to start: " + (err.message || err)
    );
    idleStatus();
  }
}

async function stopWakeWord() {
  try {
    if (wakeStream) wakeStream.getTracks().forEach((t) => t.stop());
    if (wakeAudioCtx) await wakeAudioCtx.close();
  } catch (_) {}
  wakeStream = null;
  wakeAudioCtx = null;
  wakeEngine = null;
  wakeReady = false;
}

// --- header buttons ---
muteBtn.addEventListener("click", async () => {
  muted = !muted;
  muteBtn.classList.toggle("active", muted);
  muteBtn.title = muted ? "Unmute the wake word" : "Mute the wake word";
  if (muted) await stopWakeWord();
  else await startWakeWord();
  idleStatus();
});

pinBtn.addEventListener("click", () => {
  pinned = !pinned;
  pinBtn.classList.toggle("active", pinned);
  ipcRenderer.send("set-always-on-top", pinned);
});

// The dashboard — one click, no URL to remember. The backend redirects
// /desktop/{secret}/dashboard to the real cockpit address.
function dashboardUrl() {
  const { url, secret } = cfg();
  return url && secret ? `${url}/desktop/${secret}/dashboard` : null;
}

document.getElementById("dashboard-btn").addEventListener("click", () => {
  const target = dashboardUrl();
  if (target) shell.openExternal(target);
  else addMsg("system", "Connect first — open Settings (⚙️) and fill in the backend URL + secret.");
});

settingsBtn.addEventListener("click", () => {
  const c = cfg();
  cfgUrl.value = c.url;
  cfgSecret.value = c.secret;
  document.getElementById("cfg-dash-boot").checked =
    localStorage.getItem("jarvis_dash_boot") !== "off";
  cfgStatus.textContent = "";
  settingsPanel.classList.remove("hidden");
});

document.getElementById("cfg-close").addEventListener("click", () => {
  settingsPanel.classList.add("hidden");
});

document.getElementById("cfg-save").addEventListener("click", async () => {
  localStorage.setItem("jarvis_url", cfgUrl.value.trim().replace(/\/+$/, ""));
  localStorage.setItem("jarvis_secret", cfgSecret.value.trim());
  localStorage.setItem(
    "jarvis_dash_boot",
    document.getElementById("cfg-dash-boot").checked ? "on" : "off"
  );
  cfgStatus.textContent = "Checking connection…";
  try {
    await pingBackend();
    cfgStatus.textContent = "Connected ✓";
    addMsg("system", "Connected to Jarvis ✓");
    setConnected(true, "Connected · brain online");
    loadCardScript();
    settingsPanel.classList.add("hidden");
  } catch (err) {
    cfgStatus.textContent = describeError(err);
    setConnected(false, "Offline — check Settings");
  }
  await stopWakeWord();
  if (!muted) await startWakeWord();
});

// --- boot ---
(async function boot() {
  addMsg("system", "Jarvis desktop — say “Hey Jarvis”, press the button, or type.");
  const c = cfg();
  if (!c.url || !c.secret) {
    settingsPanel.classList.remove("hidden");
    setStatus("idle", "Open Settings to connect");
    return;
  }
  try {
    await pingBackend();
    addMsg("system", "Connected to Jarvis ✓");
    setConnected(true, "Connected · brain online");
    // Dashboard on boot (Paul, 6 Aug: no URLs to remember, ever) —
    // opt-out lives in Settings.
    if (localStorage.getItem("jarvis_dash_boot") !== "off") {
      const target = dashboardUrl();
      if (target) shell.openExternal(target);
    }
  } catch (err) {
    addMsg("system", describeError(err));
    setConnected(false, "Offline — check Settings");
  }
  loadCardScript();
  await startWakeWord();
})();
