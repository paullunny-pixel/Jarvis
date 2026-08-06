// Jarvis Mac app — Electron main process. Small always-open window; all the
// interesting work (wake word, recording, backend calls) lives in the
// renderer. Personal-use app: nodeIntegration is on so the renderer can read
// the local Porcupine model file without a bundler.
const { app, BrowserWindow, ipcMain, systemPreferences } = require("electron");
const path = require("path");

let win = null;

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
    width: 420,
    height: 680,
    minWidth: 340,
    minHeight: 480,
    title: "Jarvis",
    backgroundColor: "#0d1117",
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
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
