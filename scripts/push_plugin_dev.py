"""Push the loose plugin tiddlers straight to the tw-dev wiki's native
TiddlyWiki server API — the plugin-dev inner loop (edit under plugins/ →
push → reload wiki). The gateway is not involved: it no longer has notebook
routes or wiki sessions.

Field mapping mirrors plugins/mblackman/ai-gateway/tiddlywiki.files. The
TW5 put-tiddler route pulls a nested `fields` object up into top-level
tiddler fields, so every extra field goes through `fields`.
"""

import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import os

WIKI = os.environ.get("WIKI_URL", "http://claude-docker:8080")
ROOT = Path(__file__).resolve().parent.parent / "plugins" / "mblackman" / "ai-gateway"


def main() -> int:
    manifest = json.loads((ROOT / "tiddlywiki.files").read_text())
    with httpx.Client(
        base_url=WIKI,
        headers={"X-Requested-With": "TiddlyWiki"},
        timeout=30,
    ) as client:
        for entry in manifest["tiddlers"]:
            fields = dict(entry["fields"])
            title = fields.pop("title")
            if title.startswith("$:/config/"):
                continue  # never clobber the wiki's live gateway config
            text = (ROOT / entry["file"]).read_text()
            resp = client.put(
                f"/recipes/default/tiddlers/{quote(title, safe='')}",
                json={"title": title, "text": text, "fields": fields},
            )
            resp.raise_for_status()
            print(f"pushed {title} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
