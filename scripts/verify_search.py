"""Headless check of the sidebar semantic-search flow (run via
scripts/run_headless.sh, like verify_plugin_local_mode.py).

Seeds a few notes, drives the panel in Search mode (mode toggle → tm-familiar-
search), and asserts the ranked results render as .fam-result blocks with the
most relevant note on top. Also exercises the $tw.Familiar.search JS API
directly for structured scores. Restores config and cleans up.
"""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG_PREFIX = "$:/config/mblackman/familiar/"
IN_NET_URL = "http://gateway:8787"

SEEDS = {
    "Wireguard VPN": "Wireguard tunnel runs on the wg0 interface. Peers exchange "
                     "public keys and complete a handshake before traffic flows.",
    "Sourdough Bread": "Feed the starter with flour and water; bake in a preheated "
                       "dutch oven for good oven spring.",
    "Caddy Proxy": "Caddy is a reverse proxy that provisions automatic HTTPS "
                   "certificates for each site block.",
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


async def get_text(page, title):
    return await page.evaluate("(t) => $tw.wiki.getTiddlerText(t, '')", title)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.text.startswith("[familiar]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        original_url = await get_text(page, CFG_PREFIX + "GatewayURL")
        await set_tiddler(page, CFG_PREFIX + "GatewayURL", IN_NET_URL)
        for title, text in SEEDS.items():
            await set_tiddler(page, title, text)
        await asyncio.sleep(4)  # let the syncer push config + seeds

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        await asyncio.sleep(2)

        # --- structured JS API: scores, best-first ---
        api = await page.evaluate(
            "(q) => $tw.Familiar.search(q, null, 5)",
            "how do I configure the wireguard tunnel",
        )
        print("api results:", api["results"])
        titles = [r["title"] for r in api["results"]]
        assert titles and titles[0] == "Wireguard VPN", f"wrong top hit: {titles}"
        assert api["results"][0]["score"] >= api["results"][-1]["score"], "not sorted"
        assert api["results"][0]["snippet"], "missing snippet"

        # --- UI flow: mode toggle → composer → tm-familiar-search → results ---
        await set_tiddler(page, "$:/state/familiar/mode", "search")
        await set_tiddler(page, "$:/temp/volatile/familiar/question", "reverse proxy https certificates")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-familiar-search'})")
        results = []
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/familiar/searching") != "yes":
                results = await page.evaluate(
                    "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/search/result/]sort[title]]')"
                    ".map(t => { var f = $tw.wiki.getTiddler(t).fields;"
                    " return {caption: f.caption, score: f.score, snippet: f.snippet || ''}; })"
                )
                if results:
                    break
        print("result tiddlers:", [(r["caption"], r["score"]) for r in results])
        assert results and results[0]["caption"] == "Caddy Proxy", \
            f"expected Caddy Proxy on top: {[r['caption'] for r in results]}"
        assert all(r["snippet"] for r in results), "a result has no snippet"

        # --- the panel renders every result block in the DOM (the earlier
        # transcluded-HTML approach rendered only the first two) ---
        await page.click("text=Familiar")
        await asyncio.sleep(1)
        expected = len(results)
        rendered = await page.locator(".fam-search-results .fam-result").count()
        print(f"rendered .fam-result count: {rendered} (expected {expected})")
        assert expected >= 3, f"too few results to be a real test: {expected}"
        assert rendered == expected, f"only {rendered}/{expected} result blocks rendered"
        await page.locator(".fam-panel").screenshot(path="/tmp/search.png")

        # --- restore config + clean up ---
        if original_url:
            await set_tiddler(page, CFG_PREFIX + "GatewayURL", original_url)
        else:
            await page.evaluate("(t) => $tw.wiki.deleteTiddler(t)", CFG_PREFIX + "GatewayURL")
        await page.evaluate("(ts) => ts.forEach(t => $tw.wiki.deleteTiddler(t))", list(SEEDS))
        await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/]] [prefix[$:/state/familiar/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await asyncio.sleep(4)
        await browser.close()
        print("SEARCH VERIFICATION PASSED")


asyncio.run(main())
