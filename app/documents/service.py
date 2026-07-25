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


def looks_like_document_request(text: str) -> bool:
    return bool(FETCH_PATTERN.search(text))


class DocumentLibrary:
    def __init__(
        self,
        db: Database,
        memory: MemoryStore,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._db = db
        self._memory = memory
        self._objects = object_store

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
