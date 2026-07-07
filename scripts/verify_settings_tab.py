"""Headless check of the Control Panel settings tab (plugin 0.8.0).

Run via scripts/run_headless.sh. Verifies:
1. The Settings tiddler is installed and tagged $:/tags/ControlPanel/SettingsTab.
2. Control Panel -> Settings -> Familiar renders the three config inputs.
3. GatewayURL is read per request (no reload): flipping the config tiddler
   mid-session routes the next ask at the new host.
Restores config and cleans up.
"""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG_URL = "$:/config/mblackman/familiar/GatewayURL"
SETTINGS_TITLE = "$:/plugins/mblackman/familiar/ui/Settings"
BOGUS_HOST = "bogus-gateway.invalid"


async def wait_ready(page):
    for _ in range(60):
        ok = await page.evaluate(
            "() => typeof $tw !== 'undefined' && !!$tw.wiki && !!$tw.rootWidget"
        )
        if ok:
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("wiki did not boot")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        await page.goto(WIKI)
        await wait_ready(page)

        # 1. Settings tiddler installed with the control-panel tag.
        tags = await page.evaluate(
            "(t) => { var td = $tw.wiki.getTiddler(t); return td ? td.fields.tags : null; }",
            SETTINGS_TITLE,
        )
        assert tags and "$:/tags/ControlPanel/SettingsTab" in tags, (
            f"settings tiddler missing or untagged: {tags!r}"
        )
        print("PASS settings tiddler installed + tagged")

        # 2. It renders in the control panel with the three inputs.
        await page.evaluate(
            "() => { $tw.wiki.addToStory('$:/ControlPanel');"
            "$tw.wiki.addToHistory('$:/ControlPanel'); }"
        )
        await page.wait_for_selector(".tc-control-panel", timeout=5000)
        await page.click(".tc-control-panel .tc-tab-buttons button:has-text('Settings')")
        await page.click(".tc-control-panel button:has-text('Familiar')")
        await page.wait_for_selector("input.fam-settings-input", timeout=5000)
        # 3 core inputs (GatewayURL/APIKey/ChatNoteTemplate) + 4 advanced
        # number inputs (RagTopK/MaxTiddlers/SearchResultCount/RelatedCount);
        # QueryRewrite renders as a <select>, counted separately.
        n = await page.locator("input.fam-settings-input").count()
        assert n == 7, f"expected 7 settings inputs, got {n}"
        sel = await page.locator("select.fam-settings-input").count()
        assert sel == 1, f"expected 1 settings select, got {sel}"
        print("PASS control panel tab renders 7 inputs + query-rewrite select")

        # 3. Typing a new GatewayURL into the tab takes effect on the very
        # next request, no reload: the ask must hit the bogus host.
        orig = await page.evaluate("(t) => $tw.wiki.getTiddlerText(t, '')", CFG_URL)
        url_input = page.locator("input.fam-settings-input").first
        await url_input.fill(f"http://{BOGUS_HOST}:9/")
        got = await page.evaluate("(t) => $tw.wiki.getTiddlerText(t, '')", CFG_URL)
        assert BOGUS_HOST in got, f"input did not write config tiddler: {got!r}"

        requests.clear()
        await page.evaluate(
            "() => { $tw.wiki.addTiddler(new $tw.Tiddler("
            "{title: '$:/temp/volatile/familiar/question', text: 'ping?'}));"
            "$tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'}); }"
        )
        for _ in range(20):
            if any(BOGUS_HOST in u for u in requests):
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(
                f"no request reached {BOGUS_HOST}; saw: {requests}"
            )
        print("PASS GatewayURL change applied without reload")

        # Restore config (empty means "was absent"; keep tiddler consistent)
        # and take the control panel back out of the story.
        await url_input.fill(orig)
        await page.evaluate(
            "() => { var story = $tw.wiki.getTiddlerList('$:/StoryList');"
            "$tw.wiki.addTiddler(new $tw.Tiddler({title: '$:/StoryList',"
            "list: story.filter(function(t) { return t !== '$:/ControlPanel'; })}));"
            "}"
        )
        await asyncio.sleep(2)  # let the syncer flush the restore to tw-dev
        await browser.close()
        print("ALL PASS")


asyncio.run(main())
