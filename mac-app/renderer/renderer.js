// Jarvis desktop renderer — the whole client lives here.
//
// Privacy model: openWakeWord runs LOCALLY (ONNX models via onnxruntime-node,
// right in this process). Not one byte of audio leaves the Mac until either
// the wake word fires, the talk button is pressed, or something is typed.
// The status pill always tells the truth.
//
// Layout (v3, 7 Aug): three panes always visible — sidebar (nav + reference
// panes) · centre (live conversation) · right (Today's Focus / Next up /
// Portuguese / WhatsApp groups, always on, not a switchable "Hub" tab).
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const { ipcRenderer, shell, Notification } = require("electron");
const { OpenWakeWord } = require("../wakeword.js");

const MODEL_DIR = path.join(__dirname, "..", "assets");

// --- DOM ---
const feedEl = document.getElementById("feed");
const statusPillEl = document.getElementById("status-pill");
const dotEl = document.getElementById("status-dot");
const statusEl = document.getElementById("status-text");
const recBadgeEl = document.getElementById("rec-badge");
const mainColEl = document.getElementById("main-col");
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

// --- pre-buffered capture (§1 fix): the wake engine's mic stream feeds raw
// 16kHz PCM continuously (see startWakeWord). A short rolling ring of that
// PCM is always kept so the moment the wake word fires, capture can splice
// on the last ~0.9s instead of starting from silence. ---
const CAPTURE_SAMPLE_RATE = 16000;
const PREROLL_MS = 900;
const PREROLL_SAMPLES = Math.round((CAPTURE_SAMPLE_RATE * PREROLL_MS) / 1000);
let pcmRing = [];
let pcmRingSamples = 0;
let capturing = false;
let captureOnChunk = null;

// --- desktop notifications (§2) ---
const notifBadgeEl = document.getElementById("notif-badge");
const notifListEl = document.getElementById("notif-list");
const notifMuteBtn = document.getElementById("notif-mute-btn");
const NOTIF_POLL_MS = 20000;
let notifyMuted = localStorage.getItem("jarvis_notify_muted") === "1";
let notifSeenIds = new Set();
let notifFirstPoll = true;
let notifPollTimer = null;

function cfg() {
  return {
    url: (localStorage.getItem("jarvis_url") || "").replace(/\/+$/, ""),
    secret: localStorage.getItem("jarvis_secret") || "",
  };
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function setStatus(next, text) {
  state = next;
  const cls = muted && next === "idle" ? "muted" : (next === "idle" && wakeReady && !muted ? "listening" : next);
  statusPillEl.className = "pill " + cls;
  statusEl.textContent = text;
  recBadgeEl.classList.toggle("hidden", next !== "recording");
}

function idleStatus() {
  if (muted) return setStatus("idle", "Wake word muted — button and typing still work");
  if (wakeReady) return setStatus("idle", 'Listening for "Hey Jarvis"…');
  return setStatus("idle", "Wake word off — models missing (run `npm run fetch-model`)");
}

// --- the live conversation feed ---
let lastMsgDay = null;

function maybeDaySeparator() {
  const day = new Date().toDateString();
  if (day === lastMsgDay) return;
  lastMsgDay = day;
  const sep = document.createElement("div");
  sep.className = "daysep";
  sep.textContent = new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  feedEl.appendChild(sep);
}

function addMsg(kind, text) {
  maybeDaySeparator();
  const wrap = document.createElement("div");
  wrap.className = "msg" + (kind === "jarvis" ? "" : " " + kind);
  const bub = document.createElement("div");
  bub.className = "bub";
  bub.textContent = text;
  wrap.appendChild(bub);
  if (kind !== "system") {
    const meta = document.createElement("div");
    meta.className = "meta";
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    meta.innerHTML = `<span class="who">${kind === "you" ? "You" : "Jarvis"}</span> · ${time}`;
    wrap.appendChild(meta);
  }
  feedEl.appendChild(wrap);
  feedEl.scrollTop = feedEl.scrollHeight;
  return wrap;
}

function tone(ctx, freq, startAt, durationMs = 0.12) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.08, startAt);
  gain.gain.exponentialRampToValueAtTime(0.001, startAt + durationMs);
  osc.connect(gain).connect(ctx.destination);
  osc.start(startAt);
  osc.stop(startAt + durationMs);
  return osc;
}

function chime(freq = 880) {
  try {
    const ctx = new AudioContext();
    const osc = tone(ctx, freq, ctx.currentTime, 0.25);
    osc.onended = () => ctx.close();
  } catch (_) {}
}

// Recording feedback (§1): a rising two-note blip means "go ahead, I'm
// listening"; a single falling note means "got it, sending" — audibly
// distinct so Paul never has to check the screen to know which is which.
function chimeStart() {
  try {
    const ctx = new AudioContext();
    tone(ctx, 660, ctx.currentTime, 0.09);
    const last = tone(ctx, 990, ctx.currentTime + 0.1, 0.14);
    last.onended = () => ctx.close();
  } catch (_) {}
}

function chimeStop() {
  chime(440);
}

// --- the Card Script panel (6 Aug brief) ---
// A rendered view of Jarvis's ACTUAL card-parsing config, served by the
// backend — nothing here is hard-coded, so a renamed Trello list shows up
// on the next sync.
function renderCardScript(container, data, stale) {
  const colours = data.colours || {};
  const esc = escapeHtml;
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
    // cache at all the space stays empty rather than erroring.
  }
}

// --- Reference nav (§3): Trello card script (live) / Cheat-sheet (verbatim,
// static) / Commands (live, backend registry) / Filed documents / Notifications ---
function renderCheatSheet() {
  const container = document.getElementById("cheatsheet-panel");
  container.innerHTML = `
    <div class="ref-card">
      <p><b>What I need per card — in order of importance</b></p>
      <h4>Essential (I can't file it properly without these)</h4>
      <ol>
        <li><b>Title</b> — what the task actually is, in your words. Short and specific: "Email BMI full sales plan" not "BMI stuff".</li>
        <li><b>Domain</b> — which company it belongs to: Derma Direct UK, Derma Direct EU, Aesthetics Supply UK, Prodermis, or Personal. If it's a name I haven't got a ruling for (Revolax, AMS, BMI, LPG, JD Bio), I'll ask you once and remember your answer forever.</li>
        <li><b>List</b> — where it lives: Paul Today, This Week, Brain Dump, Blocked/Waiting On, or Inbox.</li>
      </ol>
      <h4>Strongly worth giving me</h4>
      <ol start="4">
        <li><b>Priority</b> — P1 to P5. P1 and P2 auto-flag as Urgent.</li>
        <li><b>Due date</b> — plain words are fine: "today", "friday", "next Tuesday".</li>
        <li><b>Owner</b> — you, Sarah, Adriana, Harry, Kiefer, whoever's actually holding it.</li>
      </ol>
      <h4>Optional but useful</h4>
      <ol start="7">
        <li><b>Description</b> — any context you'll want in three weeks when you've forgotten why. Links, amounts, names.</li>
        <li><b>Checklist</b> — the sub-steps. You can give each item its own owner and due date.</li>
        <li><b>Board</b> — Master Board by default; say so if it's the Paul x Harry board.</li>
      </ol>
    </div>`;
}

let commandsLoaded = false;
async function loadCommands(force = false) {
  const container = document.getElementById("commands-panel");
  if (commandsLoaded && !force) return;
  try {
    const data = await backendFetch("/commands");
    const esc = escapeHtml;
    const groups = Object.entries(data.categories || {})
      .map(([cat, cmds]) => `
        <div class="cmd-group">
          <h4>${esc(cat)}</h4>
          ${cmds.map((c) => `
            <div class="cmd-row">
              <div class="cmd-phrase">${esc(c.phrase)}</div>
              <div class="cmd-does">${esc(c.does)}</div>
            </div>`).join("")}
        </div>`)
      .join("");
    container.innerHTML = `<div class="ref-card">${groups}</div>`;
    commandsLoaded = true;
  } catch (_) {
    if (!container.innerHTML) {
      container.innerHTML = '<div class="ref-card">Connect to load the commands list.</div>';
    }
  }
}

const REF_PANES = ["script", "cheatsheet", "commands", "notifications", "files"];

function switchRefPane(name) {
  document.querySelectorAll(".nav[data-ref]").forEach((el) =>
    el.classList.toggle("on", el.dataset.ref === name));
  REF_PANES.forEach((n) =>
    document.getElementById(`ref-pane-${n}`).classList.toggle("hidden", n !== name));
  if (name === "commands") loadCommands();
  if (name === "files") loadRecentFiles();
}

document.querySelectorAll(".nav[data-ref]").forEach((el) =>
  el.addEventListener("click", () => switchRefPane(el.dataset.ref)));

// --- sidebar panel-nav: jump to + flash the matching card in the right column ---
function flashEl(el) {
  el.classList.remove("flash");
  void el.offsetWidth; // restart the animation
  el.classList.add("flash");
}

document.querySelectorAll(".nav[data-scroll]").forEach((el) => {
  el.addEventListener("click", () => {
    const card = document.getElementById(el.dataset.scroll);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      flashEl(card);
    }
  });
});

document.getElementById("nav-feed").addEventListener("click", () => {
  feedEl.scrollTop = feedEl.scrollHeight;
});

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

// --- desktop notifications + spoken announcements (§2) ---
// Telegram stays the always-on/away-from-desk channel (unchanged); this is
// the ADDITIONAL desktop surface for when Paul's actually at the Mac.
function renderNotifList(items) {
  notifBadgeEl.textContent = String(items.length);
  notifBadgeEl.classList.toggle("hidden", items.length === 0);
  if (!items.length) {
    notifListEl.innerHTML = '<div class="notif-empty">Nothing waiting.</div>';
    return;
  }
  notifListEl.innerHTML = "";
  for (const n of items.slice(0, 8)) {
    const row = document.createElement("div");
    row.className = "notif-item";
    const txt = document.createElement("div");
    txt.className = "txt";
    txt.textContent = (n.announce ? "🔊 " : "") + n.text;
    const dismiss = document.createElement("button");
    dismiss.className = "dismiss";
    dismiss.textContent = "✕";
    dismiss.title = "Dismiss";
    dismiss.addEventListener("click", () => dismissNotification(n.id));
    row.appendChild(txt);
    row.appendChild(dismiss);
    notifListEl.appendChild(row);
  }
}

async function dismissNotification(id) {
  try {
    await backendFetch(`/notifications/${id}/dismiss`, { method: "POST" });
  } catch (_) {}
  await pollNotifications();
}

async function speakAnnouncement(text) {
  chime(740);
  try {
    const data = await backendFetch("/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (data.audio_b64) {
      await new Promise((resolve) => {
        const audio = new Audio("data:audio/mpeg;base64," + data.audio_b64);
        audio.onended = resolve;
        audio.onerror = resolve;
        audio.play().catch(resolve);
      });
      return;
    }
  } catch (_) {}
  // Instant fallback: macOS `say`, no network needed.
  try { execFile("say", [text]); } catch (_) {}
}

async function presentNotification(n) {
  try {
    new Notification({ title: "Jarvis", body: n.text, silent: !n.announce }).show();
  } catch (_) {}
  // Guardrails: never speak over an in-progress turn (mid-call/meeting on
  // this Mac reads as "Jarvis is already mid-conversation"); the mute
  // control silences the spoken half only — the banner still lands.
  if (n.announce && !notifyMuted && state === "idle") {
    await speakAnnouncement(n.text);
  }
}

async function pollNotifications() {
  let data;
  try {
    data = await backendFetch("/notifications");
  } catch (_) {
    return; // offline — the queue just doesn't refresh this tick
  }
  const items = data.notifications || [];
  renderNotifList(items);
  const fresh = items.filter((n) => !notifSeenIds.has(n.id));
  items.forEach((n) => notifSeenIds.add(n.id));
  if (notifFirstPoll) {
    // Don't fire a barrage of banners/speech for a backlog that built up
    // while the app was closed — just populate the queue silently.
    notifFirstPoll = false;
    return;
  }
  for (const n of fresh.sort((a, b) => a.id - b.id)) {
    await presentNotification(n);
  }
}

function startNotifPolling() {
  if (notifPollTimer) return;
  pollNotifications();
  notifPollTimer = setInterval(pollNotifications, NOTIF_POLL_MS);
}

notifMuteBtn.addEventListener("click", () => {
  notifyMuted = !notifyMuted;
  localStorage.setItem("jarvis_notify_muted", notifyMuted ? "1" : "0");
  notifMuteBtn.textContent = notifyMuted ? "🔇" : "🔊";
  notifMuteBtn.classList.toggle("active", notifyMuted);
  notifMuteBtn.title = notifyMuted ? "Unmute spoken announcements" : "Mute spoken announcements";
});

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

// --- raw-PCM ring buffer + WAV encoding, backing the wake-word pre-buffer fix ---
function pushToRing(chunk) {
  pcmRing.push(chunk);
  pcmRingSamples += chunk.length;
  while (pcmRingSamples > PREROLL_SAMPLES && pcmRing.length > 1) {
    pcmRingSamples -= pcmRing[0].length;
    pcmRing.shift();
  }
}

function concatFloat32(chunks, totalLength) {
  const out = new Float32Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function encodeWav(pcmFloat32, sampleRate) {
  const numSamples = pcmFloat32.length;
  const buffer = new ArrayBuffer(44 + numSamples * 2);
  const view = new DataView(buffer);
  const writeString = (offset, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + numSamples * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate (16-bit mono)
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeString(36, "data");
  view.setUint32(40, numSamples * 2, true);
  let offset = 44;
  for (let i = 0; i < numSamples; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, pcmFloat32[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// Starts capturing IMMEDIATELY on the wake engine's already-open stream
// (no new getUserMedia round trip) — the pre-roll ring is spliced onto the
// front so the wake phrase's tail is never lost. Resolves with the raw PCM
// once ~1.2s of silence follows speech (or a hard 20s cap).
function beginPreBufferedCapture({ silenceMs = 1200, maxMs = 20000 } = {}) {
  return new Promise((resolve) => {
    const preRoll = pcmRing.slice();
    let chunks = preRoll;
    let samples = pcmRingSamples;
    const started = Date.now();
    let lastLoud = started; // the wake word itself counts as recent speech
    capturing = true;

    const finish = () => {
      if (!capturing) return;
      capturing = false;
      captureOnChunk = null;
      clearTimeout(hardStop);
      resolve(concatFloat32(chunks, samples));
    };
    // Wall-clock backstop: normally silence ends the capture (below), but if
    // the mic stream stops delivering chunks (mute mid-capture, teardown)
    // this guarantees the promise still resolves instead of wedging the app.
    const hardStop = setTimeout(finish, maxMs);

    captureOnChunk = (chunk) => {
      chunks.push(chunk);
      samples += chunk.length;
      let sum = 0;
      for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
      const rms = Math.sqrt(sum / chunk.length);
      const now = Date.now();
      if (rms > 0.02) lastLoud = now;
      if (now - lastLoud > silenceMs) return finish();
    };
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

// --- send a recorded blob → backend → feed → speak (shared by both capture paths) ---
async function sendAudioBlob(blob) {
  setStatus("thinking", "Jarvis is thinking…");
  let data;
  try {
    data = await backendFetch("/voice", {
      method: "POST",
      headers: { "Content-Type": blob.type || "audio/webm" },
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

// --- one full spoken turn (button / typed conversation loop): record → send ---
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
    chimeStop();
    addMsg("system", "Didn't hear anything.");
    return false;
  }
  chimeStop();
  return sendAudioBlob(blob);
}

function describeError(err) {
  if (err.message === "not-configured") return "Not connected — open Settings (⚙️) and fill in the backend URL + secret.";
  if (err.message === "bad-secret") return "The backend refused the desktop secret — re-check it in Settings (ask Jarvis on Telegram: “desktop setup”).";
  return "Couldn't reach Jarvis (" + err.message + ") — is the backend URL right?";
}

// --- mode 1: wake word one-shot ---
// Fix for "didn't hear anything" (§1): the wake engine's mic is already
// open and streaming raw PCM continuously (see startWakeWord's onPcm) — a
// rolling ~0.9s ring buffer of that PCM is kept at all times. The instant
// the wake word fires we splice that buffer onto the front of the capture,
// so "Hey Jarvis, what's on today" never loses its tail to a fresh
// getUserMedia()'s device-acquisition latency (the old failure mode: a
// brand new mic stream requested only AFTER detection).
async function onWakeWord() {
  if (state !== "idle" || inConversation || capturing) return; // busy — ignore
  chimeStart();
  addMsg("system", "— Hey Jarvis —");
  setStatus("recording", "Listening to you…");
  const pcm = await beginPreBufferedCapture();
  if (pcm.length < 16000 * 0.5) {
    chimeStop();
    addMsg("system", "Didn't hear anything.");
    return idleStatus();
  }
  chimeStop();
  await sendAudioBlob(encodeWav(pcm, CAPTURE_SAMPLE_RATE));
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
  talkBtn.textContent = "🎙 Start conversation";
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
  talkBtn.textContent = "🔴 End conversation";
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
      if (capturing) {
        if (captureOnChunk) captureOnChunk(pcm);
        return;
      }
      pushToRing(pcm);
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
  pcmRing = [];
  pcmRingSamples = 0;
  capturing = false;
  captureOnChunk = null;
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
    commandsLoaded = false;
    startNotifPolling();
    startPanelsRefresh();
    settingsPanel.classList.add("hidden");
  } catch (err) {
    cfgStatus.textContent = describeError(err);
    setConnected(false, "Offline — check Settings");
  }
  await stopWakeWord();
  if (!muted) await startWakeWord();
});

// =====================================================================
// The right-hand panels (§4): always-visible windows onto the existing
// backend — Today's Focus, Next up, Portuguese, WhatsApp groups. No new
// logic lives here — every panel reads an endpoint already built.
// =====================================================================
const PANELS = ["focus", "calendar", "portuguese", "whatsapp"];
let panelsRefreshTimer = null;

function applyPanelCollapsedState() {
  for (const name of PANELS) {
    const collapsed = localStorage.getItem(`jarvis_panel_${name}_collapsed`) === "1";
    const el = document.querySelector(`.card[data-panel="${name}"]`);
    if (el) el.classList.toggle("collapsed", collapsed);
  }
}

document.querySelectorAll(".panel-toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    const name = btn.dataset.toggle;
    const panel = document.querySelector(`.card[data-panel="${name}"]`);
    const collapsed = !panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed", collapsed);
    localStorage.setItem(`jarvis_panel_${name}_collapsed`, collapsed ? "1" : "0");
  });
});

function refreshPanels() {
  loadFocusPanel();
  loadCalendarPanel();
  loadPortuguesePanel();
  loadWhatsappPanel();
}

function startPanelsRefresh() {
  if (panelsRefreshTimer) return;
  refreshPanels();
  panelsRefreshTimer = setInterval(refreshPanels, 30000);
}

// --- 4a: Today's Focus (tickable) ---
async function loadFocusPanel() {
  const body = document.getElementById("focus-body");
  const countEl = document.getElementById("focus-count");
  const navBadge = document.getElementById("focus-nav-badge");
  try {
    const data = await backendFetch("/today-focus");
    if (!data.connected) {
      body.innerHTML = `<div class="panel-empty">${escapeHtml(data.reason || "Trello isn't connected yet.")}</div>`;
      countEl.textContent = "";
      navBadge.classList.add("hidden");
      return;
    }
    countEl.textContent = `${data.done} of ${data.total} done`;
    const remaining = data.total - data.done;
    navBadge.textContent = String(remaining);
    navBadge.classList.toggle("hidden", remaining <= 0);
    if (!data.total) {
      body.innerHTML = '<div class="panel-empty">Nothing on Today’s Focus yet.</div>';
      return;
    }
    body.innerHTML = "";
    for (const company of data.by_company) {
      if (!company.tasks.length) continue;
      const heading = document.createElement("div");
      heading.className = "cmp";
      heading.textContent = company.name;
      body.appendChild(heading);
      for (const task of company.tasks) {
        const row = document.createElement("div");
        row.className = "task" + (task.done ? " done" : "");
        const label = document.createElement("label");
        label.className = "box" + (task.done ? " done" : "");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = task.done;
        input.disabled = task.done;
        label.appendChild(input);
        const span = document.createElement("span");
        span.textContent = task.title;
        row.appendChild(label);
        row.appendChild(span);
        input.addEventListener("change", () => {
          if (input.checked) tickFocusTask(task.position, row, label, input);
        });
        body.appendChild(row);
      }
    }
  } catch (_) {
    body.innerHTML = '<div class="panel-empty">Couldn’t reach Jarvis.</div>';
  }
}

async function tickFocusTask(position, row, label, input) {
  row.classList.add("done"); // optimistic — instant feel
  label.classList.add("done");
  input.disabled = true;
  try {
    const data = await backendFetch(`/today-focus/${position}/done`, { method: "POST" });
    if (!data.ok) throw new Error(data.message || "failed");
    loadFocusPanel(); // reconcile counts + streak-driven header
  } catch (_) {
    row.classList.remove("done");
    label.classList.remove("done");
    input.checked = false;
    input.disabled = false;
    const toast = document.createElement("span");
    toast.className = "toast";
    toast.textContent = "Couldn’t save — try again";
    row.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }
}

// --- 4b: Next up (Google Calendar) ---
function fmtCountdown(mins) {
  if (mins <= 0) return "now";
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return `in ${h}h${m ? " " + m + "m" : ""}`;
}

async function loadCalendarPanel() {
  const body = document.getElementById("calendar-body");
  try {
    const [nextData, todayData] = await Promise.all([
      backendFetch("/calendar?range=next_up"),
      backendFetch("/calendar?range=today"),
    ]);
    if (!nextData.connected) {
      body.innerHTML = `<div class="panel-empty">${escapeHtml(nextData.reason || "Calendar not connected.")}</div>`;
      return;
    }
    const next = nextData.next_up;
    let html = "";
    if (next) {
      html += `<div class="next">
          <div class="when"><b>${escapeHtml(next.time)}</b><span>today</span></div>
          <div style="flex:1"><div class="next-t">${escapeHtml(next.title)}</div>
            <div class="next-s">${fmtCountdown(next.minutes_until)}</div></div>
          ${next.join_url ? `<button class="join" data-url="${escapeHtml(next.join_url)}">Join</button>` : ""}
        </div>`;
    } else {
      html += '<div class="panel-empty">Nothing else in the diary today.</div>';
    }
    const rest = (todayData.today || [])
      .filter((e) => !next || e.title !== next.title || e.time !== next.time)
      .slice(0, 6);
    if (rest.length) {
      html += '<div class="later">' + rest.map((e) => `<div><i>${escapeHtml(e.time)}</i>${escapeHtml(e.title)}</div>`).join("") + "</div>";
    }
    body.innerHTML = html;
    const joinBtn = body.querySelector(".join");
    if (joinBtn) joinBtn.addEventListener("click", () => shell.openExternal(joinBtn.dataset.url));
  } catch (_) {
    body.innerHTML = '<div class="panel-empty">Couldn’t reach Jarvis.</div>';
  }
}

// --- 4c: Portuguese lessons ---
async function loadPortuguesePanel() {
  const body = document.getElementById("portuguese-body");
  try {
    const data = await backendFetch("/portuguese");
    const r = data.readiness;
    const countdown = r.days_left > 0 ? `${r.days_left}<span>days to Brazil</span>` : "<span>Brazil trip is here!</span>";
    body.innerHTML = `
      <div class="pt-top">
        <div class="cd">${countdown}</div>
        <div class="gauge">
          Speech <div class="bar"><i style="width:${r.speech_pct}%"></i></div>
          <div style="margin-top:6px">Basics <div class="bar g"><i style="width:${r.survival_pct}%"></i></div></div>
        </div>
      </div>
      <button class="lesson" id="pt-start-btn">▶︎ Start today’s lesson</button>
      <div class="streak">${data.done_today ? "✅ Done today" : "Not done today"} · streak ${data.streak.current}</div>`;
    document.getElementById("pt-start-btn").addEventListener("click", startPortugueseLesson);
  } catch (_) {
    body.innerHTML = '<div class="panel-empty">Couldn’t reach Jarvis.</div>';
  }
}

// One click, no phone: types the trigger phrase, then drops straight into
// a spoken conversation loop so Paul talks and Jarvis corrects — no typing.
async function startPortugueseLesson() {
  feedEl.scrollTop = feedEl.scrollHeight;
  const text = "Start my Portuguese lesson";
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
    idleStatus();
    return;
  }
  if (!inConversation) {
    inConversation = true;
    talkBtn.textContent = "🔴 End conversation";
    talkBtn.classList.add("live");
    conversationLoop();
  }
}

// --- 4d: drag a file onto the app → into the second brain ---
const ROOM_LABELS = { you: "You", companies: "Companies", health: "Health", finances: "Finances", people: "People", private: "Private" };
function roomLabel(room) { return ROOM_LABELS[room] || room || "Companies"; }

async function uploadDroppedFile(file) {
  switchRefPane("files");
  const listEl = document.getElementById("recent-files");
  const row = document.createElement("div");
  row.className = "file-row";
  row.innerHTML = `<div class="fn">${escapeHtml(file.name)}</div><div class="fmeta">Filing…</div>`;
  listEl.prepend(row);
  try {
    const buf = await file.arrayBuffer();
    const { url, secret } = cfg();
    const res = await fetch(
      `${url}/desktop/${secret}/documents/upload?filename=${encodeURIComponent(file.name)}&mime=${encodeURIComponent(file.type || "")}`,
      { method: "POST", body: buf }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderFiledRow(row, data.id, file.name, data.suggestion || {});
  } catch (err) {
    row.innerHTML = `<div class="fn">${escapeHtml(file.name)}</div><div class="fmeta" style="color:var(--red)">Upload failed — ${escapeHtml(err.message)}</div>`;
  }
}

function renderFiledRow(row, docId, filename, s) {
  const tags = s.tags && s.tags.length ? " · " + s.tags.join(", ") : "";
  row.innerHTML = `
    <div class="fn">${escapeHtml(filename)}</div>
    <div class="fmeta">Filed under ${escapeHtml(roomLabel(s.room))}${escapeHtml(tags)}</div>
    <div class="file-confirm">Wrong?<button class="correct-room">change it</button></div>
    ${s.actionable ? `<div class="file-actionable">Looks like ${escapeHtml(s.action_kind || "something actionable")} — <button class="mk-card">create a Trello card?</button></div>` : ""}`;
  row.querySelector(".correct-room").addEventListener("click", () => correctDocRoom(docId, row, filename, s));
  const mkBtn = row.querySelector(".mk-card");
  if (mkBtn) mkBtn.addEventListener("click", () => createCardFromDoc(docId, filename, mkBtn));
}

async function correctDocRoom(docId, row, filename, s) {
  const input = prompt("File under which room? (you / companies / health / finances / people / private)", s.room || "companies");
  if (!input) return;
  const room = input.trim().toLowerCase();
  try {
    await backendFetch(`/documents/${docId}/room`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room }),
    });
    renderFiledRow(row, docId, filename, { ...s, room });
  } catch (_) {
    alert("Couldn't change that — try again.");
  }
}

async function createCardFromDoc(docId, filename, btn) {
  btn.disabled = true;
  btn.textContent = "Creating…";
  try {
    await backendFetch(`/documents/${docId}/trello`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: `Review ${filename}` }),
    });
    btn.textContent = "Created ✓";
  } catch (_) {
    btn.textContent = "Failed — retry";
    btn.disabled = false;
  }
}

async function loadRecentFiles() {
  const listEl = document.getElementById("recent-files");
  try {
    const data = await backendFetch("/documents/recent");
    if (!data.connected) { listEl.innerHTML = ""; return; }
    listEl.innerHTML = "";
    for (const doc of data.documents) {
      const row = document.createElement("div");
      row.className = "file-row";
      const tags = doc.tags && doc.tags.length ? " · " + doc.tags.join(", ") : "";
      row.innerHTML = `<div class="fn">${escapeHtml(doc.filename)}</div><div class="fmeta">Filed under ${escapeHtml(roomLabel(doc.room))}${escapeHtml(tags)}</div>`;
      listEl.appendChild(row);
    }
  } catch (_) {}
}

// Drag anywhere onto the centre pane (matches the brief's "drag onto the app
// window" — a small hint pill in the composer shows the affordance visually).
["dragenter", "dragover"].forEach((evt) =>
  mainColEl.addEventListener(evt, (e) => { e.preventDefault(); feedEl.classList.add("dragover"); }));
["dragleave", "drop"].forEach((evt) =>
  mainColEl.addEventListener(evt, (e) => { e.preventDefault(); feedEl.classList.remove("dragover"); }));
mainColEl.addEventListener("drop", (e) => {
  Array.from(e.dataTransfer.files || []).forEach(uploadDroppedFile);
});

// --- 4e: WhatsApp groups (display only — the intelligence is backend-side) ---
function buildWhatsappHtml(groups, actions, missedText, live) {
  const sorted = actions.slice().sort((a, b) => Number(b.tagged) - Number(a.tagged));
  if (!groups.length && !sorted.length && !missedText) {
    return '<div class="panel-empty">You’re caught up — nothing needs you.</div>';
  }
  let html = "";
  for (const g of groups) {
    html += `<div class="grp"><span class="gn">${escapeHtml(g.chat_title)}</span><span class="gs">· ${escapeHtml(g.gist || "no gist yet")} (${g.message_count})</span></div>`;
  }
  for (const a of sorted) {
    html += `<div class="tag">
      ${a.tagged ? '<span class="lbl">⚑ You were tagged</span>' : ""}
      “${escapeHtml(a.ask)}”
      <span class="who">${escapeHtml(a.asked_by)}</span>
      <div class="acts">
        <div class="btn-s" data-action="trello" data-id="${a.id}" data-live="${live ? "1" : "0"}">+ Add to Trello</div>
        <div class="btn-s ghost" data-action="ignore" data-id="${a.id}" data-live="${live ? "1" : "0"}">Ignore</div>
      </div></div>`;
  }
  if (missedText) {
    html += `<div class="dismiss" id="wa-dismiss" data-live="${live ? "1" : "0"}">${escapeHtml(missedText)}</div>`;
  }
  return html;
}

function wireWhatsappButtons(body, reload) {
  body.querySelectorAll(".btn-s").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.live !== "1") return alert("Sample data — connect the SIM to make this real.");
      actOnGroupAction(btn.dataset.id, btn.dataset.action, reload);
    });
  });
  const dismissBtn = document.getElementById("wa-dismiss");
  if (dismissBtn) dismissBtn.addEventListener("click", async () => {
    if (dismissBtn.dataset.live !== "1") return alert("Sample data — connect the SIM to make this real.");
    try { await backendFetch("/groups/dismiss-summary", { method: "POST" }); } catch (_) {}
    reload();
  });
}

async function actOnGroupAction(id, kind, reload) {
  try {
    await backendFetch(`/groups/actions/${id}/${kind}`, { method: "POST" });
  } catch (_) {}
  reload();
}

async function loadWhatsappPanel() {
  const body = document.getElementById("whatsapp-body");
  const badge = document.getElementById("whatsapp-badge");
  const navBadge = document.getElementById("whatsapp-nav-badge");
  try {
    const [summaries, actions, missed, count] = await Promise.all([
      backendFetch("/groups/summaries"),
      backendFetch("/groups/actions"),
      backendFetch("/groups/missed-summary"),
      backendFetch("/groups/uncleared-count"),
    ]);
    const n = count.count || 0;
    badge.textContent = n ? `${n} need you` : "";
    navBadge.textContent = String(n);
    navBadge.classList.toggle("hidden", !n);
    if (!summaries.connected) {
      body.innerHTML =
        `<div class="panel-empty">${escapeHtml(summaries.reason || "WhatsApp group reading isn’t connected yet.")} Set up the second number and this fills in automatically.</div>` +
        '<button class="sample-btn" id="wa-sample-btn">Preview with sample data</button>';
      document.getElementById("wa-sample-btn").addEventListener("click", async () => {
        try {
          const fx = await backendFetch("/groups/fixtures");
          body.innerHTML = buildWhatsappHtml(fx.group_summaries, fx.open_actions, fx.missed_summary.text, false);
          wireWhatsappButtons(body, loadWhatsappPanel);
        } catch (_) {}
      });
      return;
    }
    body.innerHTML = buildWhatsappHtml(summaries.groups || [], actions.actions || [], (missed.text || ""), true);
    wireWhatsappButtons(body, loadWhatsappPanel);
  } catch (_) {
    body.innerHTML = '<div class="panel-empty">Couldn’t reach Jarvis.</div>';
  }
}

// --- boot ---
(async function boot() {
  addMsg("system", "Jarvis desktop — say “Hey Jarvis”, press the button, or type.");
// --- Bia, the live EN⇄PT interpreter (8 Aug): Gemini native speech in the
// Aoede voice, bridged through the backend so no keys live on this Mac.
// Mic up as 16kHz PCM, Bia's voice back at 24kHz; she opens the session
// herself with the scripted pt-BR intro. NO barge-in by design — Bia asks
// not to be interrupted, so playback always runs to the end of her turn.
let biaLive = null;
const biaBtn = document.getElementById("bia-btn");
biaBtn.addEventListener("click", async () => {
  if (biaLive) { biaLive.stop(); return; }
  const { url, secret } = cfg();
  if (!url || !secret) {
    addMsg("system", "Connect first — open Settings (⚙️) and fill in the backend URL + secret.");
    return;
  }
  biaBtn.textContent = "Connecting…";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    const wsUrl = url.replace(/^http/, "ws") + `/desktop/${secret}/bia-live`;
    const ws = new WebSocket(wsUrl);
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const playCtx = new AudioContext({ sampleRate: 24000 });
    let nextAt = 0;

    proc.onaudioprocess = (e) => {
      if (ws.readyState !== 1) return;
      const inp = e.inputBuffer.getChannelData(0);
      const ratio = ctx.sampleRate / 16000;
      const out = new Int16Array(Math.floor(inp.length / ratio));
      for (let i = 0; i < out.length; i++) {
        const s = Math.max(-1, Math.min(1, inp[Math.floor(i * ratio)]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      let bin = "";
      const bytes = new Uint8Array(out.buffer);
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      ws.send(JSON.stringify({ audio: btoa(bin) }));
    };

    const playChunk = (b64) => {
      const raw = atob(b64);
      const pcm = new Int16Array(new Uint8Array([...raw].map((c) => c.charCodeAt(0))).buffer);
      const buf = playCtx.createBuffer(1, pcm.length, 24000);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
      const s = playCtx.createBufferSource();
      s.buffer = buf;
      s.connect(playCtx.destination);
      const at = Math.max(playCtx.currentTime, nextAt);
      s.start(at);
      nextAt = at + buf.duration;
    };

    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.error) { addMsg("system", m.error); biaLive.stop(); return; }
      if (m.ready) {
        biaBtn.textContent = "🔴 Bia live — tap to end";
        src.connect(proc);
        proc.connect(ctx.destination);
        addMsg("system", "Bia is on — she'll open in Portuguese, then translate both ways. One voice at a time, don't talk over her.");
      }
      if (m.audio) playChunk(m.audio);
    };
    ws.onclose = () => { if (biaLive) biaLive.stop(); };

    biaLive = {
      stop: () => {
        biaLive = null;
        try { ws.close(); } catch (e) {}
        try { proc.disconnect(); src.disconnect(); } catch (e) {}
        stream.getTracks().forEach((t) => t.stop());
        try { ctx.close(); playCtx.close(); } catch (e) {}
        biaBtn.textContent = "🌐 Bia · translator";
      },
    };
  } catch (err) {
    biaBtn.textContent = "🌐 Bia · translator";
    addMsg("system", "Couldn't start Bia — check the mic permission and try again.");
  }
});

  notifMuteBtn.textContent = notifyMuted ? "🔇" : "🔊";
  notifMuteBtn.classList.toggle("active", notifyMuted);
  renderNotifList([]);
  renderCheatSheet();
  applyPanelCollapsedState();
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
    startNotifPolling();
    startPanelsRefresh();
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
