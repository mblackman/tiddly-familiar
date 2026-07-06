"""Content-addressed note cache: hash parity with the plugin's JS sha256,
store/check/resolve round-trips, and TTL pruning."""

import time

from app.note_cache import NoteCache, canonical_hash

# Parity vectors shared with the JS implementation in
# plugins/mblackman/familiar/src/startup.js — if either side changes its
# canonical form, both this test and the plugin's vector comment must move
# together, otherwise local-mode clients silently lose all cache hits.
PARITY_VECTORS = [
    (
        ("Zebra", "A zebra is a striped horse.", ""),
        "13c0a20134166a0c73da78321b8d6588431cb8cd11a5022f2276dd31b726985f",
    ),
    (
        ("Note", "", ""),
        "d68e7ae2c3de5f151a1f463b4c33cd852817f0a8c3405cd02312fee9d280e94a",
    ),
    (
        ("Ünïcødé ✨", "tëxt 🦓", "TagA [[Tag B]]"),
        "9ca48d25d446f1e5724955761c10b8cd75d3351c96908032adfcae5286baca6b",
    ),
]


def test_canonical_hash_parity_vectors():
    for (title, text, tags), expected in PARITY_VECTORS:
        assert canonical_hash(title, text, tags) == expected


def test_hash_changes_with_any_component():
    base = canonical_hash("T", "x", "a")
    assert canonical_hash("T2", "x", "a") != base
    assert canonical_hash("T", "x2", "a") != base
    assert canonical_hash("T", "x", "a2") != base


def test_put_check_get_roundtrip(tmp_path):
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    tid = {"title": "Zebra", "text": "A zebra is a striped horse.", "fields": {}}
    h = canonical_hash("Zebra", "A zebra is a striped horse.", "")
    assert cache.check([h]) == set()
    cache.put_many([tid])
    assert cache.check([h]) == {h}
    assert cache.get_many([h]) == {h: tid}
    assert cache.get_many(["deadbeef"]) == {}
    cache.close()


def test_hash_recomputed_server_side(tmp_path):
    """A wrong client hash can only cause a miss, never a wrong lookup: the
    store key is recomputed from content, ignoring whatever the client sent."""
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    cache.put_many([{"title": "T", "text": "x", "fields": {"tags": "a"}}])
    assert cache.check(["not-the-real-hash"]) == set()
    real = canonical_hash("T", "x", "a")
    assert cache.check([real]) == {real}
    cache.close()


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "notes.sqlite3")
    h = canonical_hash("T", "x", "")
    cache = NoteCache(path)
    cache.put_many([{"title": "T", "text": "x", "fields": {}}])
    cache.close()
    reopened = NoteCache(path)
    assert reopened.check([h]) == {h}
    reopened.close()


def test_prune_drops_stale_keeps_fresh(tmp_path):
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    cache.put_many([{"title": "Old", "text": "o", "fields": {}}])
    old_cutoff = time.time() - 40 * 86400
    cache._db.execute("UPDATE notes SET last_seen = ?", (old_cutoff,))
    cache._db.commit()
    cache.put_many([{"title": "Fresh", "text": "f", "fields": {}}])
    assert cache.prune(ttl_days=30) == 1
    assert cache.check([canonical_hash("Fresh", "f", "")]) == {
        canonical_hash("Fresh", "f", "")
    }
    assert cache.check([canonical_hash("Old", "o", "")]) == set()
    cache.close()


def test_check_and_get_many_span_query_chunks(tmp_path):
    """Lookups over more hashes than one IN clause holds still resolve every
    entry (queries are chunked under SQLite's bound-variable limit)."""
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    tiddlers = [
        {"title": f"Note {i}", "text": f"body {i}", "fields": {}}
        for i in range(1200)
    ]
    cache.put_many(tiddlers)
    hashes = [canonical_hash(t["title"], t["text"], "") for t in tiddlers]
    unknown = ["0" * 64, "f" * 64]
    assert cache.check(hashes + unknown) == set(hashes)
    resolved = cache.get_many(hashes + unknown)
    assert len(resolved) == len(tiddlers)
    assert resolved[hashes[1199]]["title"] == "Note 1199"
    cache.close()


def test_check_bumps_last_seen_across_chunks(tmp_path):
    """The last_seen bump covers every found hash, not just the first chunk."""
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    tiddlers = [
        {"title": f"N{i}", "text": "x", "fields": {}} for i in range(600)
    ]
    cache.put_many(tiddlers)
    cache._db.execute("UPDATE notes SET last_seen = ?", (time.time() - 40 * 86400,))
    cache._db.commit()
    cache.check([canonical_hash(t["title"], "x", "") for t in tiddlers])
    assert cache.prune(ttl_days=30) == 0
    cache.close()


def test_check_bumps_last_seen(tmp_path):
    """A note referenced only by hash must not age out while still in use."""
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    cache.put_many([{"title": "T", "text": "x", "fields": {}}])
    h = canonical_hash("T", "x", "")
    cache._db.execute("UPDATE notes SET last_seen = ?", (time.time() - 40 * 86400,))
    cache._db.commit()
    cache.check([h])  # bump
    assert cache.prune(ttl_days=30) == 0
    cache.close()


def test_get_many_bumps_last_seen(tmp_path):
    """Resolving a ref counts as use: a warm client sends confirmed notes as
    bare refs and skips /notes/check, so get_many is the only place an active
    note is 'seen' — it must refresh the retention window or the note ages out
    from under the tab and forces a needless 409 resend."""
    cache = NoteCache(str(tmp_path / "notes.sqlite3"))
    cache.put_many([{"title": "T", "text": "x", "fields": {}}])
    h = canonical_hash("T", "x", "")
    cache._db.execute("UPDATE notes SET last_seen = ?", (time.time() - 40 * 86400,))
    cache._db.commit()
    assert cache.get_many([h]) == {h: {"title": "T", "text": "x", "fields": {}}}
    assert cache.prune(ttl_days=30) == 0
    cache.close()
