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

    def check(self, hashes: list[str]) -> set[str]:
        """Return the subset of `hashes` present, bumping their last_seen."""
        present: set[str] = set()
        now = time.time()
        for h in hashes:
            row = self._db.execute(
                "SELECT 1 FROM notes WHERE hash = ?", (h,)
            ).fetchone()
            if row:
                present.add(h)
                self._db.execute(
                    "UPDATE notes SET last_seen = ? WHERE hash = ?", (now, h)
                )
        self._db.commit()
        return present

    def get_many(self, hashes: list[str]) -> dict[str, dict]:
        """Resolve hashes to {title, text, fields} dicts; absent keys omitted."""
        out: dict[str, dict] = {}
        for h in hashes:
            row = self._db.execute(
                "SELECT title, text, fields FROM notes WHERE hash = ?", (h,)
            ).fetchone()
            if row:
                out[h] = {
                    "title": row[0],
                    "text": row[1],
                    "fields": json.loads(row[2]),
                }
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
