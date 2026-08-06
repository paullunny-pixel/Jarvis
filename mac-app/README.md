# Jarvis Mac app

An always-open desktop window for Jarvis: say **"Hey Jarvis"** for a quick
hands-free voice note, press **Start conversation** for a turn-based
back-and-forth, or just type. Everything hits the real Jarvis backend — same
brain, memory and tools as Telegram and the phone line.

Privacy: the wake word runs **locally** on the Mac (Picovoice Porcupine, no
cloud). No audio leaves the machine until "Hey Jarvis" fires, the talk button
is pressed, or you type. The mute button (🎙) switches the wake word off
entirely; the status dot always shows what the app is doing.

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

(This also downloads the small wake-word model automatically. If that step
ever fails — e.g. no internet — run `npm run fetch-model` later.)

### 4. Get your two keys

- **Desktop secret + backend URL** — message Jarvis on Telegram:
  **"desktop setup"**. He replies with the exact two values to paste in.
- **Picovoice AccessKey** (free, powers the wake word) — sign up at
  https://console.picovoice.ai , copy the AccessKey shown on the dashboard.
  Without it the wake word stays off, but the talk button and typing still
  work fine.

### 5. Run it

```
npm start
```

The window opens and shows Settings on first launch. Paste in:

- **Backend URL** (from Jarvis's "desktop setup" reply)
- **Desktop secret** (same reply)
- **Picovoice AccessKey**

Click **Save & connect** — you should see "Connected ✓" in the feed.
macOS will ask for **microphone access** the first time — click Allow.
(If you miss it: System Settings → Privacy & Security → Microphone →
enable Electron.)

---

## Daily use

Just run `npm start` from `Jarvis/mac-app` (or keep the window open — it's
meant to stay on the desktop).

- **"Hey Jarvis"** → chime → speak your message → he answers out loud and in
  the feed, then goes back to idle listening. (The built-in keyword is
  "Jarvis", so plain "Jarvis" works too.)
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
- **Wake word never fires** — is the Picovoice key pasted in ⚙️? Did
  `assets/porcupine_params.pv` download (run `npm run fetch-model`)? Is 🎙
  unmuted (dot should be green)?
- **He hears nothing / empty recordings** — microphone permission (System
  Settings → Privacy & Security → Microphone → Electron), and if you're on
  AirPods try the built-in mic; Bluetooth mics can be flaky.
- **Wants a true "Hey Jarvis" phrase** — train a custom keyword at
  https://console.picovoice.ai (Porcupine → "Hey Jarvis" → macOS platform),
  download the `.ppn`, save it as `mac-app/assets/hey-jarvis.ppn`, restart
  the app. It's picked up automatically.

## What's next (already planned)

The talk button currently runs a turn-based loop on the existing pipeline
(Deepgram → Claude → ElevenLabs). When the shared realtime voice engine
lands, the button swaps to a fully live, interruptible conversation — a
contained change, not a rebuild.
