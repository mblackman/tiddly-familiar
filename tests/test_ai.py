"""Pure helpers in app.ai: context assembly and embeddable text."""

from app.ai import _build_context, _tiddler_text


def test_build_context_formats_and_lists_sources():
    tiddlers = [
        {"title": "Note A", "text": "Alpha content."},
        {"title": "Note B", "text": "  Beta content.  "},
    ]
    context, sources = _build_context(tiddlers)
    assert context == "## Note A\nAlpha content.\n\n## Note B\nBeta content."
    assert sources == ["Note A", "Note B"]


def test_build_context_skips_empty_text():
    tiddlers = [
        {"title": "Empty", "text": "   "},
        {"title": "Textless"},
        {"title": "Real", "text": "content"},
    ]
    context, sources = _build_context(tiddlers)
    assert sources == ["Real"]
    assert "Empty" not in context and "Textless" not in context


def test_build_context_title_from_nested_fields():
    tiddlers = [{"fields": {"title": "Nested"}, "text": "body"}]
    context, sources = _build_context(tiddlers)
    assert context == "## Nested\nbody"
    assert sources == ["Nested"]


def test_tiddler_text_prepends_title():
    assert _tiddler_text({"title": "T", "text": "body"}) == "T\nbody"


def test_tiddler_text_handles_missing_parts():
    # Title carries signal even with no text; no stray whitespace either way.
    assert _tiddler_text({"title": "Only Title"}) == "Only Title"
    assert _tiddler_text({"fields": {"title": "Nested"}, "text": ""}) == "Nested"
