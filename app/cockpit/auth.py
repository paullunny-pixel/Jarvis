"""Cockpit protection — the link alone is no longer the key.

Paul sets a password by telling Jarvis in Telegram ('set cockpit password
…'); it is stored only as a salted PBKDF2 hash. A correct login mints a
signed, expiring cookie so each device asks once (~30 days). HTTPS (Render)
encrypts the wire; this layer covers the LINK leaking — screenshots,
forwarded chats, shoulder surfing. Secure by default: until a password
exists, the dashboard serves a setup notice and NO data.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

PASSWORD_KEY = "cockpit_password_hash"
SESSION_KEY = "cockpit_session_key"
COOKIE_NAME = "jarvis_cockpit"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def mint_session(session_key: str, now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(session_key.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_session(token: str, session_key: str, now: float | None = None) -> bool:
    if not token or not session_key:
        return False
    try:
        expiry_text, sig = token.split(".", 1)
        expiry = int(expiry_text)
    except Exception:
        return False
    if (now if now is not None else time.time()) > expiry:
        return False
    expected = hmac.new(session_key.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# Brute-force lockout: N wrong guesses inside the window locks the login
# for LOCKOUT_MINUTES. State lives in the settings table so it survives
# restarts and Render's multiple workers see the same count.
ATTEMPTS_KEY = "cockpit_login_attempts"
MAX_ATTEMPTS = 10
LOCKOUT_MINUTES = 15


async def note_failed_login(store, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    try:
        raw = await store.get(ATTEMPTS_KEY, "")
        window_start, count = (raw.split(":") + ["0"])[:2] if raw else ("0", "0")
        if now - float(window_start) > LOCKOUT_MINUTES * 60:
            window_start, count = str(now), "0"
        await store.set(ATTEMPTS_KEY, f"{window_start}:{int(count) + 1}")
    except Exception:
        await store.set(ATTEMPTS_KEY, f"{now}:1")


async def login_locked(store, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    try:
        raw = await store.get(ATTEMPTS_KEY, "")
        if not raw:
            return False
        window_start, count = raw.split(":")
        if now - float(window_start) > LOCKOUT_MINUTES * 60:
            return False
        return int(count) >= MAX_ATTEMPTS
    except Exception:
        return False


async def clear_login_failures(store) -> None:
    await store.set(ATTEMPTS_KEY, "")


async def gate(store, cookie: str) -> str:
    """The one decision the endpoints share: 'setup' (no password yet — show
    instructions, serve NO data), 'login' (password set, no valid session),
    or 'ok' (valid signed cookie)."""
    if not await store.get(PASSWORD_KEY, ""):
        return "setup"
    key = await store.get(SESSION_KEY, "")
    return "ok" if verify_session(cookie, key) else "login"


_PAGE_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Jarvis</title><style>
body{{background:#0b0f14;color:#e8eef5;font-family:-apple-system,system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
main{{background:#121820;padding:32px;border-radius:16px;width:min(340px,88vw)}}
h1{{font-size:18px;margin:0 0 6px}} p{{color:#8fa0b3;font-size:13px;line-height:1.5;margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #2a3644;
background:#0b0f14;color:#e8eef5;font-size:16px;margin-bottom:12px}}
button{{width:100%;padding:12px;border-radius:10px;border:none;background:#2f81f7;color:#fff;
font-size:15px;cursor:pointer}}
.err{{color:#ff6b6b;font-size:13px;margin:0 0 12px}}
code{{background:#0b0f14;padding:2px 6px;border-radius:6px}}
</style></head><body><main>{body}</main></body></html>"""

LOGIN_FORM = """<h1>Jarvis — Progress Cockpit</h1>
<p>Private. Enter the cockpit password.</p>{error}
<form method="post" action="{action}">
<input type="password" name="password" placeholder="Password" autofocus
 autocomplete="current-password">
<button type="submit">Unlock</button></form>"""

SETUP_NOTICE = """<h1>Jarvis — Progress Cockpit</h1>
<p>Locked until a password exists — no data is served without one.</p>
<p>Message Jarvis on Telegram:<br>
<code>set cockpit password &lt;your password&gt;</code><br>
then reload this page and log in.</p>"""


def login_page(action: str, error: str = "") -> str:
    err_html = f'<p class="err">{error}</p>' if error else ""
    return _PAGE_SHELL.format(body=LOGIN_FORM.format(action=action, error=err_html))


def setup_page() -> str:
    return _PAGE_SHELL.format(body=SETUP_NOTICE)
