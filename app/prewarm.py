"""
Background embedding pre-warm for ingested notes.

Notes reach the note cache from the plugin's background sync or as full sends
on the ask path; this worker embeds their chunks off the request path so
ask-time retrieval is all cache hits and answers stop reporting a warming
index (`truncated`). The vector cache is the deliverable — a dropped or failed
note simply gets embedded inline by the next ask, exactly as before this
worker existed, so nothing here may ever fail a request.
"""

import asyncio
import logging

from .ai import _chunk_text, _embed_text
from .embeddings import Embedder
from .note_cache import canonical_hash

logger = logging.getLogger(__name__)

# Bounds memory if Ollama stalls while syncs keep arriving; drops are safe.
_QUEUE_MAX = 10_000

# Notes per worker cycle. The embedder batches chunks to Ollama itself; this
# only coalesces queue items so one cycle isn't a single tiny request.
_NOTES_PER_BATCH = 16


class EmbedPrewarmer:
    """Single-worker queue that warms the embedding cache for note dicts
    ({title, text, fields}).

    Cache keys must match ask-time retrieval exactly, so notes are chunked
    with ai._chunk_text and keyed via ai._embed_text before going through
    the same Embedder.embed_documents the ask path uses.
    """

    def __init__(self, embedder: Embedder, queue_max: int = _QUEUE_MAX):
        self._embedder = embedder
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_max)
        # Hashes queued but not yet embedded: repeat syncs of the same content
        # (e.g. several tabs warming at once) don't bloat the queue.
        self._pending: set[str] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def aclose(self) -> None:
        """Cancel the worker. Progress is kept: the embedder writes each
        completed chunk batch through to SQLite as it lands."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def enqueue(self, tiddlers: list[dict]) -> None:
        """Queue notes for background embedding. Never raises: on overflow the
        rest of the batch is dropped with a log line."""
        for t in tiddlers:
            text = t.get("text", "")
            if not text.strip():
                continue  # retrieval skips empty notes; no vector needed
            key = canonical_hash(
                t.get("title", ""), text, (t.get("fields") or {}).get("tags", "")
            )
            if key in self._pending:
                continue
            try:
                self._queue.put_nowait(t)
            except asyncio.QueueFull:
                logger.warning(
                    "EmbedPrewarmer: queue full, dropping %d note(s) —"
                    " they will embed inline on the next ask",
                    len(tiddlers) - tiddlers.index(t),
                )
                return
            self._pending.add(key)

    async def drain(self) -> None:
        """Wait until every queued note has been processed (test helper)."""
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < _NOTES_PER_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                texts = [
                    _embed_text(t.get("title", ""), chunk)
                    for t in batch
                    for _offset, chunk in _chunk_text(t.get("text", ""))
                ]
                await self._embedder.embed_documents(texts)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Cache stays cold for these notes; the ask path embeds inline.
                logger.warning(
                    "EmbedPrewarmer: batch of %d failed (%s)", len(batch), e
                )
            finally:
                for t in batch:
                    self._pending.discard(
                        canonical_hash(
                            t.get("title", ""),
                            t.get("text", ""),
                            (t.get("fields") or {}).get("tags", ""),
                        )
                    )
                    self._queue.task_done()
