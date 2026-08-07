"""The document library: upload → store safely → extract text → chunk → embed →
recallable by meaning ("show me the BMI contract")."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.store import utc_now_iso
from app.db.base import Database
from app.documents.extract import chunk_document_text, extract_text
from app.documents.storage import ObjectStore
from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)

FETCH_PATTERN = re.compile(
    r"\b(show me|send me|pull up|find|get me|where'?s|share)\b.{0,60}?"
    r"\b(contract|document|doc|file|pdf|agreement|invoice|statement|soa|report|spreadsheet|terms)\b",
    re.IGNORECASE,
)

FILING_SYSTEM = """\
You are filing a document Paul just dropped into his second brain. Read \
the extracted text and return ONLY a JSON object:
{"room": "<one of: you, companies, health, finances, people, private>", \
"tags": ["..."], "actionable": <true/false>, "action_kind": "<short label \
like invoice/contract/demand notice, or empty>", "reason": "<one short \
clause, e.g. 'mentions Prodermis and an invoice number'>"}

Rules:
- "room" MUST be one of the six listed — pick the closest fit, default to \
"companies" if genuinely unclear.
- "tags" are 1-4 short lowercase words (company names, doc type) — no \
sentences.
- "actionable" is true only for things that plainly need a response: an \
invoice, a signed contract, a demand/legal notice. Reports and reference \
material are false.
- Never invent facts not in the text. If the text is too short/garbled to \
judge, return room "companies", empty tags, actionable false.\
"""


def looks_like_document_request(text: str) -> bool:
    return bool(FETCH_PATTERN.search(text))


class DocumentLibrary:
    def __init__(
        self,
        db: Database,
        memory: MemoryStore,
        object_store: ObjectStore | None = None,
        claude: Any | None = None,
    ) -> None:
        self._db = db
        self._memory = memory
        self._objects = object_store
        self._claude = claude

    async def ingest(
        self,
        data: bytes,
        filename: str,
        *,
        mime: str = "",
        room: str = "companies",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store the file, extract + embed its text. Returns a summary dict."""
        text = extract_text(data, filename, mime)
        if self._objects is not None:
            key = f"documents/{utc_now_iso()[:10]}/{filename}"
            await self._objects.put(key, data, mime)
            storage, storage_ref, blob = self._objects.name, key, None
        else:
            storage, storage_ref, blob = "db", "", data

        doc_id = await self._db.insert_returning_id(
            "INSERT INTO documents (filename, mime, storage, storage_ref, content, room, tags,"
            " uploaded_at, extracted_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                filename,
                mime,
                storage,
                storage_ref,
                blob,
                room,
                json.dumps(tags or []),
                utc_now_iso(),
                len(text),
            ),
        )

        chunks = chunk_document_text(text)
        for chunk in chunks:
            await self._memory.add_chunk(
                f"(from document '{filename}') {chunk}",
                room=room,
                type_="STABLE",
                source="document",
                tags=(tags or []) + [filename],
                document_id=doc_id,
            )
        logger.info("Ingested %s: %d chars, %d chunks", filename, len(text), len(chunks))
        return {"id": doc_id, "filename": filename, "chars": len(text), "chunks": len(chunks)}

    async def suggest_filing(self, text: str, filename: str) -> dict[str, Any]:
        """Drag-a-file-to-brain (Mac app v2, 4d): a best-guess room + tags +
        actionable flag Paul can correct in one click. Falls back to a safe
        default when there's no brain model wired or the model misbehaves —
        this must never block the upload itself."""
        from app.memory.store import ROOMS

        default = {"room": "companies", "tags": [], "actionable": False, "action_kind": "", "reason": ""}
        if self._claude is None or not text.strip():
            return default
        try:
            raw = await self._claude.quick(
                f"Filename: {filename}\n\nExtracted text (may be truncated):\n{text[:6000]}",
                system=FILING_SYSTEM,
                max_tokens=300,
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
        except Exception:
            logger.exception("Filing suggestion failed for %s — defaulting", filename)
            return default
        room = data.get("room") if data.get("room") in ROOMS else "companies"
        tags = [str(t)[:40] for t in (data.get("tags") or [])][:4]
        return {
            "room": room,
            "tags": tags,
            "actionable": bool(data.get("actionable")),
            "action_kind": str(data.get("action_kind") or "")[:60],
            "reason": str(data.get("reason") or "")[:200],
        }

    async def set_room(self, doc_id: int, room: str) -> bool:
        """The 'Wrong? change it' one-click correction (4d)."""
        from app.memory.store import ROOMS

        if room not in ROOMS:
            return False
        await self._db.execute("UPDATE documents SET room = ? WHERE id = ?", (room, doc_id))
        row = await self._db.fetch_one("SELECT id FROM documents WHERE id = ? AND room = ?", (doc_id, room))
        return row is not None

    async def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        """The 'recently filed' list (4d) — last N uploads, newest first."""
        rows = await self._db.fetch_all(
            "SELECT id, filename, room, tags, uploaded_at FROM documents ORDER BY uploaded_at DESC LIMIT ?",
            (limit,),
        )
        return [{**r, "tags": json.loads(r["tags"] or "[]")} for r in rows]

    async def find(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        """Semantic search → the documents most relevant to the query."""
        hits = await self._memory.search(query, k=12)
        seen: dict[int, float] = {}
        for hit in hits:
            doc_id = hit.get("document_id") or 0
            if doc_id and doc_id not in seen:
                seen[doc_id] = hit["score"]
        results = []
        for doc_id in list(seen)[:k]:
            row = await self._db.fetch_one(
                "SELECT id, filename, mime, storage, storage_ref, uploaded_at FROM documents WHERE id = ?",
                (doc_id,),
            )
            if row:
                results.append({**row, "score": seen[doc_id]})
        # Fall back to filename match if semantics found nothing
        if not results:
            for word in re.findall(r"[a-zA-Z0-9]{3,}", query):
                rows = await self._db.fetch_all(
                    "SELECT id, filename, mime, storage, storage_ref, uploaded_at FROM documents"
                    " WHERE LOWER(filename) LIKE ? LIMIT ?",
                    (f"%{word.lower()}%", k),
                )
                for row in rows:
                    if all(r["id"] != row["id"] for r in results):
                        results.append({**row, "score": 0.0})
        return results[:k]

    async def fetch_bytes(self, doc: dict[str, Any]) -> bytes:
        if doc["storage"] == "r2" and self._objects is not None:
            return await self._objects.get(doc["storage_ref"])
        row = await self._db.fetch_one("SELECT content FROM documents WHERE id = ?", (doc["id"],))
        return bytes(row["content"]) if row and row["content"] is not None else b""
