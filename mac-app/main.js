// Jarvis Mac app — Electron main process. Small always-open window; all the
// interesting work (wake word, recording, backend calls) lives in the
// renderer. Personal-use app: nodeIntegration is on so the renderer can load
// the local openWakeWord models (onnxruntime-node) without a bundler.
const { app, BrowserWindow, ipcMain, powerSaveBlocker, systemPreferences } = require("electron");
const path = require("path");

// ALWAYS-ON means always on (6 Aug: the wake word only worked while the
// window had focus — Chromium backgrounds unfocused renderers and macOS App
// Nap suspends idle apps, both starving the mic pipeline). All three
// throttles are disabled; the mic is the whole point of this app.
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("disable-background-timer-throttling");

let win = null;
let powerBlockerId = null;

ipcMain.on("set-always-on-top", (_event, on) => {
  if (win !== null) win.setAlwaysOnTop(Boolean(on));
});

async function createWindow() {
  // Ask macOS for the microphone up front — the wake word needs it from boot.
  try {
    if (systemPreferences.getMediaAccessStatus("microphone") !== "granted") {
      await systemPreferences.askForMediaAccess("microphone");
    }
  } catch (_) {
    // Non-mac platforms don't have this — fine.
  }

  win = new BrowserWindow({
    width: 1360,           // v3 hub: 250px sidebar + centre feed + 372px panels
    height: 900,
    minWidth: 640,          // below this the sidebar/panels hide (styles.css)
    minHeight: 480,
    title: "Jarvis",
    backgroundColor: "#0b0e13",
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      backgroundThrottling: false,   // keep listening when unfocused/hidden
    },
  });
  // Stop macOS App Nap from suspending the listener while Paul works
  // elsewhere. Released automatically when the app quits.
  if (powerBlockerId === null) {
    powerBlockerId = powerSaveBlocker.start("prevent-app-suspension");
  }
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  win.on("closed", () => {
    win = null;
  });
}

app.whenReady().then(createWindow);

app.on("activate", () => {
  if (win === null) createWindow();
});

app.on("window-all-closed", () => {
  // Keep the norm for small utility apps: closing the window quits (the
  // always-listening mic should never run headless without the indicator).
  app.quit();
});
