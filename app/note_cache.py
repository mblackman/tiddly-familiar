"""
Content-addressed note cache backing the client-supplied-content routes.

Local-mode clients (the browser plugin) hash each note, ask which hashes the
server is missing (/notes/check), and send full content only for those — the
rest arrive as bare {hash} references resolved here. Content addressing means
no invalidation logic: an edited note has a new hash and simply misses.

The cache is a pure optimization — an unknown reference is answered with a
409 so the client resends the content, never with wrong or missing context.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

# Drop entries not referenced by any request for this long. Content-addressed
# entries never go stale, so this only bounds disk growth from deleted notes.
TTL_DAYS = 30

# IN-clause chunk size; SQLite caps bound variables at 999 by default.
_QUERY_CHUNK = 500


def _chunks(items: list, size: int = _QUERY_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def canonical_hash(title: str, text: str, tags: str = "") -> str:
    """The note hash both sides compute: sha256 over the UTF-8 bytes of
    title NUL text NUL tags. The plugin's JS implementation must match this
    byte-for-byte (parity vectors in tests/test_note_cache.py)."""
    payload = f"{title}\0{text}\0{tags}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class NoteCache:
    """SQLite-backed store of {hash: tiddler} shared by all local-mode clients.

    Same persistence pattern as the embedding cache: lives on the profiles
    volume so container restarts keep it warm. Hashes are recomputed here on
    write — a buggy client hash can only cause cache misses, never a lookup
    that returns someone else's content.
    """

    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: created in the lifespan but used from
        # request handlers (and the TestClient portal thread in tests); the
        # sqlite3 module serializes access internally.
        self._db = sqlite3.connect(path, check_same_thread=False)
        # WAL lets ask-path readers (ref resolution) proceed while a background
        # /notes/sync writes, instead of every access serializing on one lock —
        # this store is read-mostly and hit concurrently by several tabs.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            " hash TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL,"
            " fields TEXT NOT NULL, last_seen REAL NOT NULL)"
        )
        self._db.commit()
        count = self._db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        if count:
            logger.info("NoteCache: %d cached note(s) at %s", count, path)

    def close(self) -> None:
        self._db.close()

    def count(self) -> int:
        """Total notes currently held (across all clients/hashes). A rough
        server-side coverage gauge for the plugin's sync-status panel — it
        counts distinct content hashes, so an edited note briefly double-counts
        (old + new) until the old hash ages out via the TTL."""
        return self._db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def check(self, hashes: list[str]) -> set[str]:
        """Return the subset of `hashes` present, bumping their last_seen."""
        present: set[str] = set()
        now = time.time()
        dirty = False
        for chunk in _chunks(hashes):
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT hash FROM notes WHERE hash IN ({marks})", chunk
            ).fetchall()
            found = [r[0] for r in rows]
            present.update(found)
            if found:
                self._db.execute(
                    f"UPDATE notes SET last_seen = ? WHERE hash IN"
                    f" ({','.join('?' * len(found))})",
                    [now, *found],
                )
                dirty = True
        if dirty:  # an all-miss preflight (steady state) touches nothing to commit
            self._db.commit()
        return present

    def get_many(self, hashes: list[str]) -> dict[str, dict]:
        """Resolve hashes to {title, text, fields} dicts; absent keys omitted.

        Bumps last_seen on every resolved hash: a note referenced on every ask
        but never edited is only ever seen here (a warm client sends it as a
        bare ref and skips /notes/check), so without this it would age out from
        under an active tab and force a needless 409 resend."""
        out: dict[str, dict] = {}
        found: list[str] = []
        for chunk in _chunks(hashes):
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT hash, title, text, fields FROM notes"
                f" WHERE hash IN ({marks})",
                chunk,
            ).fetchall()
            for h, title, text, fields in rows:
                out[h] = {"title": title, "text": text, "fields": json.loads(fields)}
                found.append(h)
        if found:
            now = time.time()
            for chunk in _chunks(found):
                self._db.execute(
                    f"UPDATE notes SET last_seen = ?"
                    f" WHERE hash IN ({','.join('?' * len(chunk))})",
                    [now, *chunk],
                )
            self._db.commit()
        return out

    def put_many(self, tiddlers: list[dict]) -> None:
        """Store full tiddlers ({title, text, fields}) keyed by recomputed hash."""
        now = time.time()
        self._db.executemany(
            "INSERT OR REPLACE INTO notes (hash, title, text, fields, last_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    canonical_hash(
                        t["title"],
                        t.get("text", ""),
                        (t.get("fields") or {}).get("tags", ""),
                    ),
                    t["title"],
                    t.get("text", ""),
                    json.dumps(t.get("fields") or {}),
                    now,
                )
                for t in tiddlers
            ],
        )
        self._db.commit()

    def prune(self, ttl_days: float = TTL_DAYS) -> int:
        """Delete entries unseen for `ttl_days`. Returns the number removed."""
        cutoff = time.time() - ttl_days * 86400
        cur = self._db.execute("DELETE FROM notes WHERE last_seen < ?", (cutoff,))
        self._db.commit()
        if cur.rowcount:
            logger.info("NoteCache: pruned %d stale note(s)", cur.rowcount)
        return cur.rowcount
