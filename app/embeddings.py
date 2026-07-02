"""
Local embedding-based retrieval for the `ask` path.

Wraps an Ollama server (`/api/embed`) to turn tiddler text and questions into
vectors, caches per-tiddler embeddings by content hash so unchanged notes are
never re-embedded, and cosine-ranks candidates so only the most relevant
tiddlers reach the generation model.
"""

import hashlib
import logging
import os
import sqlite3

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# Ollama embedding models have their own context window; keep well under it so a
# single long tiddler doesn't get silently dropped/errored by the server. This is
# a coarse char budget (v1 — per-tiddler chunking is a future enhancement).
_MAX_EMBED_CHARS = 8000

# Cache misses are embedded in chunks of this size so each chunk lands in the
# cache as it completes — a timeout/crash mid-corpus keeps the progress made,
# and no single request approaches the client timeout.
_EMBED_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """Embedding backend failure, with a message safe to show to the caller."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rank(query_vec: list[float], candidate_vecs: list[list[float]]) -> list[tuple[int, float]]:
    """Cosine-rank candidate vectors against the query. Returns [(index, score), ...]
    sorted by score descending. Empty candidates → empty list."""
    if not candidate_vecs:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    m = np.asarray(candidate_vecs, dtype=np.float32)
    qn = np.linalg.norm(q)
    mn = np.linalg.norm(m, axis=1)
    # Guard against zero-vectors (empty text) producing NaNs.
    denom = mn * qn
    safe = denom > 0
    scores = np.zeros(m.shape[0], dtype=np.float32)
    scores[safe] = (m[safe] @ q) / denom[safe]
    order = np.argsort(-scores)
    return [(int(i), float(scores[i])) for i in order]


class Embedder:
    """Ollama-backed embedder with a content-hash vector cache.

    The cache is keyed by the text hash alone (not title), so identical text under
    different titles reuses one vector and a retitled-but-unchanged tiddler stays
    cached. Editing a tiddler's text changes its hash and transparently re-embeds.

    With `cache_path` set, vectors also persist to a SQLite file (keyed by
    model + hash, so switching embed models never serves stale vectors) and are
    loaded back on startup — a container restart doesn't re-embed the corpus.
    Content-hash keying means no invalidation logic is needed.
    """

    def __init__(self, ollama_url: str, model: str, cache_path: str | None = None):
        self._url = ollama_url.rstrip("/")
        self._model = model
        self._cache: dict[str, list[float]] = {}
        self._client = httpx.AsyncClient(timeout=60.0)
        self._db: sqlite3.Connection | None = None
        if cache_path:
            parent = os.path.dirname(cache_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._db = sqlite3.connect(cache_path)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                " model TEXT NOT NULL, hash TEXT NOT NULL, vec BLOB NOT NULL,"
                " PRIMARY KEY (model, hash))"
            )
            self._db.commit()
            rows = self._db.execute(
                "SELECT hash, vec FROM embeddings WHERE model = ?", (model,)
            )
            self._cache = {
                h: np.frombuffer(blob, dtype=np.float32).tolist() for h, blob in rows
            }
            if self._cache:
                logger.info(
                    "Embedder: loaded %d cached vector(s) from %s (model=%s)",
                    len(self._cache), cache_path, model,
                )

    async def aclose(self):
        await self._client.aclose()
        if self._db is not None:
            self._db.close()

    def _persist(self, items: list[tuple[str, list[float]]]) -> None:
        """Write freshly embedded (hash, vector) pairs through to disk."""
        if self._db is None:
            return
        self._db.executemany(
            "INSERT OR REPLACE INTO embeddings (model, hash, vec) VALUES (?, ?, ?)",
            [
                (self._model, key, np.asarray(vec, dtype=np.float32).tobytes())
                for key, vec in items
            ],
        )
        self._db.commit()

    async def _embed_raw(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama for a batch of texts. No caching/truncation here.
        Transport/status failures surface as EmbeddingError so callers can
        distinguish embedding problems from generation-model problems."""
        if not texts:
            return []
        try:
            resp = await self._client.post(
                f"{self._url}/api/embed",
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
        except httpx.ConnectError as e:
            raise EmbeddingError(
                "Cannot reach the embedding service — Ollama may still be starting up."
            ) from e
        except httpx.TimeoutException as e:
            raise EmbeddingError(
                "The embedding service timed out — it may be busy. Please try again."
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise EmbeddingError(
                    "Embedding model not ready — it may still be downloading. "
                    "Please wait and try again."
                ) from e
            raise EmbeddingError(
                f"Embedding service returned {e.response.status_code}."
            ) from e
        data = resp.json()
        embeddings = data.get("embeddings")
        if embeddings is None or len(embeddings) != len(texts):
            raise RuntimeError(
                f"Ollama returned {len(embeddings) if embeddings else 0} embeddings "
                f"for {len(texts)} inputs (model={self._model})"
            )
        return embeddings

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string (not cached — queries rarely repeat)."""
        vecs = await self._embed_raw([text[:_MAX_EMBED_CHARS]])
        return vecs[0]

    async def embed_documents(
        self, texts: list[str], max_new: int | None = None
    ) -> list[list[float] | None]:
        """Embed documents, serving cache hits and only calling Ollama for misses.
        Returns vectors in the same order as `texts`.

        `max_new` bounds how many cache misses are embedded in this call; misses
        beyond it come back as None (skipped this request). Because embedded
        vectors are cached, repeated calls over the same corpus make progress
        until everything is covered."""
        truncated = [t[:_MAX_EMBED_CHARS] for t in texts]
        keys = [_hash(t) for t in truncated]

        missing_idx = [i for i, k in enumerate(keys) if k not in self._cache]
        skipped = 0
        if max_new is not None and len(missing_idx) > max_new:
            skipped = len(missing_idx) - max_new
            missing_idx = missing_idx[:max_new]
        if missing_idx:
            # Chunked so each completed chunk is cached even if a later one fails.
            for start in range(0, len(missing_idx), _EMBED_BATCH_SIZE):
                chunk = missing_idx[start : start + _EMBED_BATCH_SIZE]
                fresh = await self._embed_raw([truncated[i] for i in chunk])
                for i, vec in zip(chunk, fresh):
                    self._cache[keys[i]] = vec
                # Per-chunk write-through: progress survives a mid-corpus failure
                # and a restart.
                self._persist([(keys[i], vec) for i, vec in zip(chunk, fresh)])
            logger.info(
                "Embedder: %d cache hit(s), %d embedded, %d skipped (model=%s)",
                len(texts) - len(missing_idx) - skipped,
                len(missing_idx),
                skipped,
                self._model,
            )

        return [self._cache.get(k) for k in keys]
