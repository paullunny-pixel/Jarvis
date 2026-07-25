"""Embeddings for the second brain.

Production: Voyage AI (Anthropic's recommended embeddings partner — one small
extra key, generous free tier). Dev/tests: a deterministic local embedder so
the whole memory system runs with zero external calls.
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        """kind is 'document' (stored content) or 'query' (search input)."""
        ...

    async def close(self) -> None: ...


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3.5-lite",
        dim: int = 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.dim = dim
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.post(
            VOYAGE_URL,
            json={
                "input": texts,
                "model": self.model,
                "input_type": kind,
                "output_dimension": self.dim,
            },
        )
        if response.status_code != 200:
            raise EmbeddingError(f"Voyage {response.status_code}: {response.text[:300]}")
        data = response.json()
        vectors = [item["embedding"] for item in data.get("data", [])]
        if len(vectors) != len(texts):
            raise EmbeddingError("Voyage returned wrong number of embeddings")
        return vectors


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder. No network, stable across
    runs — real semantic overlap (shared words → similar vectors) which is
    exactly enough to exercise storage, retrieval and ranking in dev/tests."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def close(self) -> None:
        return None

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            token = token.strip(".,!?;:()[]\"'")
            if not token:
                continue
            digest = hashlib.md5(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        return [self._vector(t) for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (norm_a * norm_b)
