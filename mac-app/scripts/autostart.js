// Start Jarvis automatically when the Mac logs in (6 Aug: 'I want the
// dashboard to load on boot'). Writes a macOS LaunchAgent that runs
// `npm start` in this folder via a login shell (so nvm/homebrew PATHs work).
//   npm run autostart       — install + load
//   npm run autostart-off   — unload + remove
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execSync } = require("child_process");

const LABEL = "com.jarvis.desktop";
const APP_DIR = path.join(__dirname, "..");
const AGENT_DIR = path.join(os.homedir(), "Library", "LaunchAgents");
const PLIST = path.join(AGENT_DIR, `${LABEL}.plist`);

const plistContent = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd ${APP_DIR.replace(/&/g, "&amp;")} &amp;&amp; npm start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/jarvis-desktop.log</string>
  <key>StandardErrorPath</key><string>/tmp/jarvis-desktop.log</string>
</dict>
</plist>
`;

const mode = process.argv[2] || "on";

if (process.platform !== "darwin") {
  console.log("Autostart is macOS-only — nothing to do here.");
  process.exit(0);
}

if (mode === "off") {
  try { execSync(`launchctl unload ${JSON.stringify(PLIST)} 2>/dev/null`); } catch (_) {}
  if (fs.existsSync(PLIST)) fs.unlinkSync(PLIST);
  console.log("Autostart removed — Jarvis no longer launches at login.");
} else {
  fs.mkdirSync(AGENT_DIR, { recursive: true });
  fs.writeFileSync(PLIST, plistContent);
  try { execSync(`launchctl unload ${JSON.stringify(PLIST)} 2>/dev/null`); } catch (_) {}
  try {
    execSync(`launchctl load -w ${JSON.stringify(PLIST)}`);
  } catch (_) {
    // load can be picky across macOS versions — the plist alone still
    // fires at next login, which is the behaviour Paul asked for.
  }
  console.log(
    "Autostart installed — Jarvis (and the dashboard) will open every time " +
      "this Mac logs in. Undo with: npm run autostart-off"
  );
}
