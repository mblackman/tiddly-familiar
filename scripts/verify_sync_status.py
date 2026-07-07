"""Headless check of the manual-sync + sync-status feature (0.15.0), run via
scripts/run_headless.sh (like verify_search.py).

Points the plugin at the in-network gateway, seeds a couple of notes, fires the
`tm-familiar-sync` message (the settings "Sync now" button), and asserts the
`$:/temp/familiar/sync-status` tiddler ends up "ready" with populated
total / synced / server counts — and that the Control Panel settings tab renders
the readout. Exercises /notes/stats end-to-end against the live gateway.
"""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG_PREFIX = "$:/config/mblackman/familiar/"
IN_NET_URL = "http://gateway:8787"
STATUS = "$:/temp/familiar/sync-status"

SEEDS = {
    "ECS Engine Design": "Entity component system: entities are ids, components "
    "are data, systems iterate over component sets. Archetype storage.",
    "Sync Status Probe Note": "A throwaway note so the corpus is non-empty.",
}


async def wait_ready(page):
    for _ in range(60):
        if await page.evaluate(
            "() => typeof $tw !== 'undefined' && !!$tw.wiki && !!$tw.rootWidget"
        ):
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("wiki did not boot")


async def set_tiddler(page, title, text):
    await page.evaluate(
        "([t, x]) => $tw.wiki.addTiddler(new $tw.Tiddler({title: t, text: x}))",
        [title, text],
    )


async def field(page, title, name):
    return await page.evaluate(
        "([t, f]) => { var d = $tw.wiki.getTiddler(t); return d ? (d.fields[f] || '') : ''; }",
        [title, name],
    )


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.text.startswith("[familiar]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        original_url = await page.evaluate(
            "(t) => $tw.wiki.getTiddlerText(t, '')", CFG_PREFIX + "GatewayURL"
        )
        await set_tiddler(page, CFG_PREFIX + "GatewayURL", IN_NET_URL)
        for title, text in SEEDS.items():
            await set_tiddler(page, title, text)
        await asyncio.sleep(1)

        # --- fire the manual sync (the "Sync now" button's message) ---
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-familiar-sync'})")
        state = ""
        for _ in range(120):
            await asyncio.sleep(0.5)
            state = await page.evaluate("(t) => $tw.wiki.getTiddlerText(t, '')", STATUS)
            if state in ("ready", "degraded"):
                break
        print("sync state:", state)
        assert state == "ready", f"sync did not reach ready: {state!r}"

        total = int(await field(page, STATUS, "total") or 0)
        synced = int(await field(page, STATUS, "synced") or 0)
        server = await field(page, STATUS, "server")
        print(f"tracked={total} synced={synced} server={server!r}")
        assert total >= len(SEEDS), f"too few tracked notes: {total}"
        assert synced == total, f"not all tracked notes confirmed: {synced}/{total}"
        assert server != "" and int(server) >= synced, f"bad server count: {server!r}"

        # --- the settings tab renders the readout ---
        panel = await page.evaluate(
            "() => $tw.wiki.renderTiddler('text/plain',"
            " '$:/plugins/mblackman/familiar/ui/Settings')"
        )
        assert "Sync status" in panel, "settings tab missing the Sync status section"
        assert "Sync now" in panel, "settings tab missing the Sync now button"
        assert str(total) in panel, "settings tab did not render the tracked count"
        print("settings readout:", "OK")

        # --- restore config + clean up seeds ---
        if original_url:
            await set_tiddler(page, CFG_PREFIX + "GatewayURL", original_url)
        else:
            await page.evaluate("(t) => $tw.wiki.deleteTiddler(t)", CFG_PREFIX + "GatewayURL")
        await page.evaluate("(ts) => ts.forEach(t => $tw.wiki.deleteTiddler(t))", list(SEEDS))
        await asyncio.sleep(1)

        print("\nALL CHECKS PASSED")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
