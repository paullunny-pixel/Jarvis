"""Send Jarvis a link and he reads the page: fetch → clean text → in front of
the brain the same turn, and filed in the document library for later recall.

Safety: private/internal addresses are refused (the bot must never be a proxy
into its own network), hard size and time caps apply, and the router marks
fetched content as reading material — words inside a page are never commands.
"""
from __future__ import annotations

import html as _html
import ipaddress
import re
from typing import Any

import httpx

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)

MAX_BYTES = 4_000_000        # per page — enough for any article or exported doc
FETCH_TIMEOUT = 15.0
MAX_LINKS = 3                # per message; the rest are named but not fetched
CONTEXT_CHARS = 12_000       # per page, in the brain's prompt

_GOOGLE_DOC = re.compile(r"https?://docs\.google\.com/document/d/([\w-]+)")
_GOOGLE_SHEET = re.compile(r"https?://docs\.google\.com/spreadsheets/d/([\w-]+)")
_GOOGLE_DRIVE = re.compile(r"https?://drive\.google\.com/file/d/([\w-]+)")
CHATGPT_SHARE = re.compile(r"https?://(?:chat\.openai\.com|chatgpt\.com)/share/[\w-]+")

LOGIN_WALL_ERROR = (
    "the page needs a login — a Google Doc must be shared as "
    "'anyone with the link' for me to read it (or wait for the Workspace hookup)"
)


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?…")
        if url not in urls:
            urls.append(url)
    return urls


def rewrite_google_link(url: str) -> str:
    """Link-shared Google Docs/Sheets/Drive files have plain export endpoints —
    fetch those so Jarvis reads the document, not the web app's login shell."""
    m = _GOOGLE_DOC.match(url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
    m = _GOOGLE_SHEET.match(url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    m = _GOOGLE_DRIVE.match(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def blocked_host(host: str) -> bool:
    lowered = (host or "").strip("[]").lower()
    if lowered in ("", "localhost") or lowered.endswith((".local", ".internal")):
        return True
    try:
        return not ipaddress.ip_address(lowered).is_global
    except ValueError:
        return False  # an ordinary hostname


def html_to_text(page: str) -> tuple[str, str]:
    """(title, readable text) out of an HTML page."""
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    title = " ".join(_html.unescape(title_match.group(1)).split()) if title_match else ""
    text = re.sub(r"(?is)<(script|style|head|nav|footer|noscript|svg)[^>]*>.*?</\1>", " ", page)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = (" ".join(line.split()) for line in _html.unescape(text).splitlines())
    return title, "\n".join(line for line in lines if line)


def extract_chatgpt_share(page: str) -> str:
    """The conversation out of a ChatGPT share page. The visible HTML is an
    empty JS-app shell — the transcript hides inside streamed script data
    (modern: self.__next_f.push chunks; legacy: a __NEXT_DATA__ JSON blob),
    which is exactly what the normal HTML stripper throws away."""
    import json as _json

    stream = ""
    # Modern RSC stream — decode each pushed JS string and concatenate, so a
    # message split across two chunks knits back together.
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', page):
        try:
            stream += _json.loads(f'"{m.group(1)}"')
        except Exception:
            continue
    legacy = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.DOTALL)
    if legacy:
        stream += legacy.group(1)
    if not stream:
        stream = page
    roles = [
        (m.start(), m.group(1))
        for m in re.finditer(r'"author"\s*:\s*\{\s*"role"\s*:\s*"(\w+)"', stream)
    ]
    lines: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'"parts"\s*:\s*\[\s*"((?:[^"\\]|\\.)*)"', stream):
        try:
            text = _json.loads(f'"{m.group(1)}"').strip()
        except Exception:
            continue
        if not text or text in seen:
            continue
        seen.add(text)
        role = next((r for pos, r in reversed(roles) if pos < m.start()), "")
        speaker = {"user": "PAUL", "assistant": "CHATGPT"}.get(role, role.upper() or "MESSAGE")
        lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


async def fetch_page(
    url: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """One link → {'ok', 'url', 'title', 'text', 'pdf', 'error'}. Never raises."""
    fetch_url = rewrite_google_link(url)
    try:
        if blocked_host(httpx.URL(fetch_url).host):
            return {"ok": False, "url": url, "error": "that address is off-limits"}
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
            # A real browser UA — chatgpt.com and friends turn away obvious bots.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            response = await client.get(fetch_url)
        # A private Google file bounces the export to the account login page.
        if "accounts.google.com" in str(response.url) or response.status_code in (401, 403):
            return {"ok": False, "url": url, "error": LOGIN_WALL_ERROR}
        if response.status_code >= 400:
            return {"ok": False, "url": url, "error": f"the site answered {response.status_code}"}
        body = response.content[:MAX_BYTES]
        ctype = response.headers.get("content-type", "").lower()
        if "pdf" in ctype or fetch_url.split("?")[0].lower().endswith(".pdf"):
            return {"ok": True, "url": url, "title": "", "text": "", "pdf": bytes(body)}
        decoded = body.decode(response.encoding or "utf-8", "replace")
        if CHATGPT_SHARE.match(url):
            convo = extract_chatgpt_share(decoded)
            if not convo:
                return {
                    "ok": False, "url": url,
                    "error": (
                        "that ChatGPT share page came back without the conversation in it — "
                        "open the chat, Select All, and paste the text instead"
                    ),
                }
            title, _ = html_to_text(decoded)
            return {"ok": True, "url": url, "title": title or "ChatGPT conversation",
                    "text": convo, "pdf": None}
        if "html" in ctype or decoded.lstrip()[:1] == "<":
            title, text = html_to_text(decoded)
        elif ctype.startswith("text/") or "csv" in ctype or "json" in ctype or not ctype:
            title, text = "", decoded
        else:
            return {"ok": False, "url": url, "error": f"I can't read that file type ({ctype.split(';')[0]})"}
        if not text.strip():
            return {"ok": False, "url": url, "error": "the page had no readable text"}
        return {"ok": True, "url": url, "title": title, "text": text, "pdf": None}
    except httpx.TimeoutException:
        return {"ok": False, "url": url, "error": "the site was too slow to answer"}
    except Exception as exc:  # DNS failure, TLS, malformed URL — stay graceful
        return {"ok": False, "url": url, "error": f"couldn't reach it ({type(exc).__name__})"}


def filename_for(url: str, title: str, is_pdf: bool) -> str:
    """A library filename for a fetched page — title first, URL as fallback."""
    stem = re.sub(r"[^\w\s-]", "", title).strip()[:60]
    if not stem:
        u = httpx.URL(url)
        stem = re.sub(r"[^\w-]+", "-", (u.host or "page") + u.path).strip("-")[:60]
    return f"{stem or 'page'}{'.pdf' if is_pdf else '.txt'}"
