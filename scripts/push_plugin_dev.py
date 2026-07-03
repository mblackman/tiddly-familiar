"""Push the loose plugin tiddlers to the `dev` notebook via the gateway REST
API — the plugin-dev inner loop (edit under plugins/ → push → reload wiki).

Field mapping mirrors plugins/mblackman/ai-gateway/tiddlywiki.files. The REST
route only honours title/text plus the NESTED `fields` object (top-level extra
keys are dropped silently), so every field goes through `fields`.
"""

import json
import os
import sys
from pathlib import Path

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://claude-docker:8787")
NOTEBOOK = os.environ.get("NOTEBOOK", "dev")
ROOT = Path(__file__).resolve().parent.parent / "plugins" / "mblackman" / "ai-gateway"


def main() -> int:
    api_key = os.environ.get("GATEWAY_API_KEY", "")
    if not api_key:
        print("GATEWAY_API_KEY not set", file=sys.stderr)
        return 1

    manifest = json.loads((ROOT / "tiddlywiki.files").read_text())
    with httpx.Client(
        base_url=GATEWAY, headers={"X-API-Key": api_key}, timeout=30
    ) as client:
        for entry in manifest["tiddlers"]:
            fields = dict(entry["fields"])
            title = fields.pop("title")
            if title.startswith("$:/config/"):
                continue  # never clobber the wiki's live gateway config
            text = (ROOT / entry["file"]).read_text()
            resp = client.put(
                f"/notebooks/{NOTEBOOK}/tiddler",
                json={"title": title, "text": text, "fields": fields},
            )
            resp.raise_for_status()
            print(f"pushed {title} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
