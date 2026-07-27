/**
 * Jarvis WhatsApp bridge — Master Update §13. STRICTLY READ-ONLY.
 *
 * Links as a companion device to the DEDICATED WhatsApp number (never Paul's
 * real one — unofficial access carries a ban risk, which this isolates) and
 * streams work-group messages to the Jarvis backend, which tags them by
 * company and makes them summarisable ("catch me up on Derma EU").
 *
 * It never sends, reacts, or marks anything read. If WhatsApp logs the
 * device out, it prints a fresh QR in the service logs for re-linking.
 *
 * Env:
 *   JARVIS_WEBHOOK_URL  https://<jarvis>/webhook/whatsapp/<secret>
 *   AUTH_DIR            persistent dir for the session (default /data/auth)
 */
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } =
  require("@whiskeysockets/baileys");
const qrcode = require("qrcode-terminal");
const pino = require("pino");

const WEBHOOK = process.env.JARVIS_WEBHOOK_URL || "";
const AUTH_DIR = process.env.AUTH_DIR || "/data/auth";

if (!WEBHOOK) {
  console.error("JARVIS_WEBHOOK_URL is not set — nowhere to deliver messages. Exiting.");
  process.exit(1);
}

const groupNames = new Map(); // jid -> subject cache

function textOf(msg) {
  const m = msg.message || {};
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.videoMessage && m.videoMessage.caption) ||
    ""
  );
}

async function deliver(payload) {
  try {
    const res = await fetch(WEBHOOK, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) console.error("Webhook rejected message:", res.status);
  } catch (err) {
    console.error("Webhook delivery failed:", err.message);
  }
}

async function run() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: "warn" }),
    markOnlineOnConnect: false, // stay invisible — read-only observer
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log("\n=== SCAN THIS QR WITH THE DEDICATED WHATSAPP PHONE ===");
      console.log("(WhatsApp -> Settings -> Linked devices -> Link a device)\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") console.log("Bridge linked and listening (read-only).");
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.log("Logged out by WhatsApp — restart the service to show a fresh QR.");
        process.exit(0);
      }
      console.log("Connection dropped — reconnecting…");
      setTimeout(run, 3000);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      const jid = msg.key?.remoteJid || "";
      if (!jid.endsWith("@g.us")) continue; // work GROUPS only
      if (msg.key?.fromMe) continue;
      const text = textOf(msg);
      if (!text) continue;
      if (!groupNames.has(jid)) {
        try {
          const meta = await sock.groupMetadata(jid);
          groupNames.set(jid, meta.subject || jid);
        } catch {
          groupNames.set(jid, jid);
        }
      }
      await deliver({
        group_id: jid,
        group_name: groupNames.get(jid),
        sender: msg.pushName || (msg.key?.participant || "").split("@")[0],
        text: text,
        ts: new Date((Number(msg.messageTimestamp) || Date.now() / 1000) * 1000).toISOString(),
      });
    }
  });
}

run();
