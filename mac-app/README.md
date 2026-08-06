# Jarvis Mac app

An always-open desktop window for Jarvis: say **"Hey Jarvis"** for a quick
hands-free voice note, press **Start conversation** for a turn-based
back-and-forth, or just type. Everything hits the real Jarvis backend — same
brain, memory and tools as Telegram and the phone line.

Privacy: the wake word runs **locally** on the Mac — the open-source
openWakeWord "Hey Jarvis" model, executed on-device via ONNX. No account, no
key, no cloud. No audio leaves the machine until "Hey Jarvis" fires, the talk
button is pressed, or you type. The mute button (🎙) switches the wake word
off entirely; the status dot always shows what the app is doing.

---

## One-time setup (about 10 minutes)

### 1. Install Node.js (if you don't have it)

Open **Terminal** (Cmd+Space, type "Terminal") and paste:

```
node --version
```

If that prints a version like `v20.x`, skip ahead. If it says "command not
found", install it from https://nodejs.org (download the **LTS** installer,
open it, click through) — then close and reopen Terminal.

### 2. Get the app onto the Mac

If the Jarvis repo isn't on this Mac yet:

```
git clone https://github.com/paullunny-pixel/Jarvis.git
cd Jarvis/mac-app
```

If it is already cloned:

```
cd Jarvis/mac-app
git pull
```

### 3. Install the app's dependencies

```
npm install
```

(This also downloads the three small wake-word models automatically — about
2MB total. If that step ever fails — e.g. no internet — run
`npm run fetch-model` later.)

### 4. Get your pairing values

Message Jarvis on Telegram: **"desktop setup"**. He replies with the exact
two values to paste in (backend URL + desktop secret). The wake word needs
no key at all — it runs entirely on your Mac.

### 5. Run it

```
npm start
```

The window opens and shows Settings on first launch. Paste in:

- **Backend URL** (from Jarvis's "desktop setup" reply)
- **Desktop secret** (same reply)

Click **Save & connect** — you should see "Connected ✓" in the feed.
macOS will ask for **microphone access** the first time — click Allow.
(If you miss it: System Settings → Privacy & Security → Microphone →
enable Electron.)

---

## Daily use

Just run `npm start` from `Jarvis/mac-app` (or keep the window open — it's
meant to stay on the desktop).

- **"Hey Jarvis"** → chime → speak your message → he answers out loud and in
  the feed, then goes back to idle listening. (The model is trained on the
  full phrase "Hey Jarvis" — say both words.)
- **Start conversation** → turn-based chat: speak, he replies, speak again —
  press **End conversation** to stop (it also closes itself after two silent
  turns).
- **Type** in the box for a silent exchange.
- 📌 keeps the window on top · 🎙 mutes the wake word · ⚙️ settings.

Status dot: **green** = listening for the wake word · **red pulse** =
recording you · **amber pulse** = thinking · **blue** = speaking · grey =
idle/muted.

## Troubleshooting

- **"Not connected"** — re-check the backend URL and desktop secret in ⚙️
  (ask Jarvis "desktop setup" again on Telegram; the values must match
  exactly).
- **Wake word never fires** — did the three models download? Check
  `mac-app/assets/` for `melspectrogram.onnx`, `embedding_model.onnx` and
  `hey_jarvis_v0.1.onnx` (run `npm run fetch-model` if missing). Is 🎙
  unmuted (dot should be green)? Say the full phrase, "Hey Jarvis".
- **Fires too easily / not easily enough** — the sensitivity threshold lives
  in `mac-app/wakeword.js` (`threshold: 0.5`); higher = stricter. Tell the
  engineer and it's a one-line tweak.
- **He hears nothing / empty recordings** — microphone permission (System
  Settings → Privacy & Security → Microphone → Electron), and if you're on
  AirPods try the built-in mic; Bluetooth mics can be flaky.
- **macOS says "Electron.app contains malware" and bins it** — a known
  Gatekeeper false positive on Electron's development build; nothing was
  actually wrong. Fix: reinstall and clear the quarantine flag, then start:

  ```
  rm -rf node_modules
  npm install
  xattr -cr node_modules/electron/dist/Electron.app
  npm start
  ```

  If npm prints an "install scripts not yet covered by allowScripts"
  warning naming electron, run the `npm approve-scripts` command it
  suggests, then `npm install` again.
- **`git pull` says "Please move or remove them before you merge"** — a
  local leftover file is in the way. From the `Jarvis` folder:
  `rm -f mac-app/package-lock.json && git pull` (the file is re-created
  by the update).

## What's next (already planned)

The talk button currently runs a turn-based loop on the existing pipeline
(Deepgram → Claude → ElevenLabs). When the shared realtime voice engine
lands, the button swaps to a fully live, interruptible conversation — a
contained change, not a rebuild.
