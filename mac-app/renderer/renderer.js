// Jarvis desktop renderer — the whole client lives here.
//
// Privacy model: Porcupine runs LOCALLY (WASM in this window). Not one byte
// of audio leaves the Mac until either the wake word fires, the talk button
// is pressed, or something is typed. The status dot always tells the truth.
const fs = require("fs");
const path = require("path");
const { ipcRenderer } = require("electron");

// --- Picovoice globals (IIFE builds expose namespaces on window) ---
const Porcupine = window.PorcupineWeb;
const WVP = window.WebVoiceProcessor && window.WebVoiceProcessor.WebVoiceProcessor;

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
const cfgPicovoice = document.getElementById("cfg-picovoice");
const cfgStatus = document.getElementById("cfg-status");
const typeForm = document.getElementById("type-form");
const typeInput = document.getElementById("type-input");

// --- state ---
let state = "idle"; // idle | listening | recording | thinking | speaking
let muted = false;
let pinned = false;
let inConversation = false;
let porcupine = null;
let wakeReady = false;

function cfg() {
  return {
    url: (localStorage.getItem("jarvis_url") || "").replace(/\/+$/, ""),
    secret: localStorage.getItem("jarvis_secret") || "",
    picovoice: localStorage.getItem("jarvis_picovoice") || "",
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
  return setStatus("idle", "Wake word off — set a Picovoice key in Settings");
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

// --- wake word engine ---
async function startWakeWord() {
  const { picovoice } = cfg();
  if (!picovoice) {
    wakeReady = false;
    return idleStatus();
  }
  if (!Porcupine || !WVP) {
    wakeReady = false;
    addMsg("system", "Wake-word libraries didn't load — run `npm install` in mac-app/.");
    return idleStatus();
  }
  try {
    const modelPath = path.join(__dirname, "..", "assets", "porcupine_params.pv");
    if (!fs.existsSync(modelPath)) {
      wakeReady = false;
      addMsg("system", "Wake-word model missing — run `npm run fetch-model` in mac-app/.");
      return idleStatus();
    }
    const modelB64 = fs.readFileSync(modelPath).toString("base64");
    // A custom 'Hey Jarvis' keyword file (from console.picovoice.ai) is used
    // automatically if you drop it in as assets/hey-jarvis.ppn; otherwise the
    // built-in 'Jarvis' keyword — saying 'Hey Jarvis' fires it just the same.
    const customPath = path.join(__dirname, "..", "assets", "hey-jarvis.ppn");
    const keyword = fs.existsSync(customPath)
      ? { label: "Hey Jarvis", base64: fs.readFileSync(customPath).toString("base64"), sensitivity: 0.65 }
      : { builtin: "Jarvis", sensitivity: 0.65 };
    porcupine = await Porcupine.PorcupineWorker.create(
      picovoice,
      [keyword],
      () => onWakeWord(),
      { base64: modelB64 }
    );
    await WVP.subscribe(porcupine);
    wakeReady = true;
    idleStatus();
  } catch (err) {
    wakeReady = false;
    addMsg("system", "Wake word failed to start: " + (err.message || err));
    idleStatus();
  }
}

async function stopWakeWord() {
  try {
    if (porcupine) {
      await WVP.unsubscribe(porcupine);
      porcupine.release && porcupine.release();
      porcupine.terminate && porcupine.terminate();
    }
  } catch (_) {}
  porcupine = null;
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

settingsBtn.addEventListener("click", () => {
  const c = cfg();
  cfgUrl.value = c.url;
  cfgSecret.value = c.secret;
  cfgPicovoice.value = c.picovoice;
  cfgStatus.textContent = "";
  settingsPanel.classList.remove("hidden");
});

document.getElementById("cfg-close").addEventListener("click", () => {
  settingsPanel.classList.add("hidden");
});

document.getElementById("cfg-save").addEventListener("click", async () => {
  localStorage.setItem("jarvis_url", cfgUrl.value.trim().replace(/\/+$/, ""));
  localStorage.setItem("jarvis_secret", cfgSecret.value.trim());
  localStorage.setItem("jarvis_picovoice", cfgPicovoice.value.trim());
  cfgStatus.textContent = "Checking connection…";
  try {
    await pingBackend();
    cfgStatus.textContent = "Connected ✓";
    addMsg("system", "Connected to Jarvis ✓");
    settingsPanel.classList.add("hidden");
  } catch (err) {
    cfgStatus.textContent = describeError(err);
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
  } catch (err) {
    addMsg("system", describeError(err));
  }
  await startWakeWord();
})();
