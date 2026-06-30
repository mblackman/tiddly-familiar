"""
Discovery tool: navigate to a notebook and dump unlock inputs + $tw diagnostics.

Usage:
    python -m scripts.probe <notebook-name> [--headful]

Run this first on a new wiki to confirm the password selector and verify
that $tw is accessible after unlock.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# allow running from the project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

from app.config import load_config

_DRIVER_JS = (Path(__file__).parent.parent / "app" / "driver.js").read_text()


async def run_probe(name: str, headful: bool) -> None:
    cfg = load_config()

    if name not in cfg.notebooks:
        print(f"Unknown notebook: {name!r}")
        print(f"Available: {', '.join(cfg.notebooks.keys()) or '(none)'}")
        sys.exit(1)

    nb_cfg = cfg.notebooks[name]
    print(f"\n=== Probe: {name} ===")
    print(f"URL: {nb_cfg.app_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        page = await browser.new_page()
        try:
            await page.add_init_script(_DRIVER_JS)
            await page.goto(nb_cfg.app_url, wait_until="load")
            # Give the PWA time to boot, sync, and render any unlock dialog
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            result = await page.evaluate("() => window.__gw.probe()")

            print("\n--- INPUTS (pick the password selector) ---")
            for inp in result.get("inputs", []):
                vis = "visible" if inp.get("visible") else "hidden"
                print(f"  [{inp['index']}] selector: {inp['selector']}   ({vis})")
                print(
                    f"       type={inp['type']}  name={inp['name']!r}  id={inp['id']!r}"
                    f"  class={inp['class']!r}"
                )
                if inp.get("placeholder") or inp.get("ariaLabel") or inp.get("label"):
                    print(
                        f"       placeholder={inp['placeholder']!r}"
                        f"  aria-label={inp['ariaLabel']!r}  label={inp['label']!r}"
                    )
                print(f"       parent: {inp.get('parent', '')}")
                if inp.get("context"):
                    print(f"       nearby text: {inp['context']!r}")
                print(f"       html: {inp.get('html', '')}")

            print("\n--- DIAG ---")
            print(f"  $tw present:              {result.get('twPresent')}")
            print(f"  Tiddler count (all):      {result.get('tiddlerCount')}")
            print(f"  System tiddlers:          {result.get('systemCount')}")
            print(f"  [tiddlypwa[]] operator:   {result.get('hasTiddlyPwaFilter')}")

            methods = result.get("syncerMethods", [])
            if methods:
                print(f"  Syncer methods:  {', '.join(methods)}")
            else:
                print("  Syncer methods:  (none — wiki may still be locked)")

        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a TiddlyPWA notebook for selector and $tw diagnostics"
    )
    parser.add_argument("notebook", help="Notebook name as defined in config.yaml")
    parser.add_argument(
        "--headful", action="store_true", help="Show the browser window"
    )
    args = parser.parse_args()
    asyncio.run(run_probe(args.notebook, args.headful))


if __name__ == "__main__":
    main()
