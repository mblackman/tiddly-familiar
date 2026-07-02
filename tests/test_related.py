"""ai.related: embedding-similarity neighbours of a target tiddler."""

import asyncio

from app.ai import related


class FakeEmbedder:
    """Vectors keyed by marker words in the text; default orthogonal."""

    VECS = {
        "APPLES": [1.0, 0.0, 0.0],
        "PEARS": [0.9, 0.1, 0.0],  # close to APPLES
        "BRICKS": [0.0, 1.0, 0.0],
    }

    def __init__(self, skip_marker=None):
        self.skip_marker = skip_marker

    async def embed_documents(self, texts, max_new=None):
        out = []
        for t in texts:
            if self.skip_marker and self.skip_marker in t:
                out.append(None)
                continue
            vec = [0.0, 0.0, 1.0]
            for marker, v in self.VECS.items():
                if marker in t:
                    vec = v
                    break
            out.append(vec)
        return out


def _run(target_title, tiddlers, k=5, embedder=None):
    target = next(t for t in tiddlers if t["title"] == target_title)
    return asyncio.run(
        related(target, tiddlers, embedder or FakeEmbedder(), top_k=k)
    )


TIDDLERS = [
    {"title": "Apples", "text": "APPLES notes"},
    {"title": "Pears", "text": "PEARS notes"},
    {"title": "Bricks", "text": "BRICKS notes"},
]


def test_orders_by_similarity_and_excludes_target():
    items, truncated = _run("Apples", TIDDLERS)
    titles = [i["title"] for i in items]
    assert titles[0] == "Pears"          # most similar first
    assert "Apples" not in titles        # never returns the target itself
    assert truncated is False
    assert all(0 < i["score"] <= 1 for i in items)


def test_top_k_limits_results():
    items, _ = _run("Apples", TIDDLERS, k=1)
    assert [i["title"] for i in items] == ["Pears"]


def test_zero_similarity_candidates_dropped():
    """Orthogonal notes aren't 'related' — better an empty list than junk."""
    tiddlers = [
        {"title": "Apples", "text": "APPLES notes"},
        {"title": "Unrelated", "text": "nothing special"},  # default vec ⊥ APPLES
    ]
    items, _ = _run("Apples", tiddlers)
    assert items == []


def test_empty_target_text():
    tiddlers = [{"title": "Empty", "text": "  "}] + TIDDLERS
    items, truncated = _run("Empty", tiddlers)
    assert items == [] and truncated is False


def test_unembedded_candidates_flag_truncated():
    tiddlers = TIDDLERS + [{"title": "Cold", "text": "SKIPME text"}]
    items, truncated = _run("Apples", tiddlers, embedder=FakeEmbedder("SKIPME"))
    assert truncated is True
    assert [i["title"] for i in items] == ["Pears"]  # cold note just missing
