"""Headless end-to-end check of the (local-only) plugin against the dev wiki.

Runs INSIDE the gateway container (docker cp + docker exec), like
verify_plugin_headless.py. The plugin sends note content with every request;
a network tripwire records any request touching /notebooks/ and fails the
run (the plugin must never use the notebook routes). Exercises the streaming
ask over locally-collected notes, the content-addressed cache (second ask
must be all hash refs → cache.hits > 0), history follow-up, local
related-notes, and browser-rendered Summarize. Restores config and cleans up.
"""

import asyncio
import json

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG_PREFIX = "$:/config/mblackman/ai-gateway/"
IN_NET_URL = "http://gateway:8787"

SEEDS = {
    "Zebra Facts": "A zebra is a striped horse native to Africa. "
                   "Each zebra's stripe pattern is unique, like a fingerprint.",
    "Okapi Notes": "The okapi is a forest giraffe with striped legs, "
                   "found in the Congo rainforest.",
}


async def wait_ready(page):
    for _ in range(60):
        ok = await page.evaluate(
            "() => typeof $tw !== 'undefined' && !!$tw.wiki && !!$tw.rootWidget"
        )
        if ok:
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


async def wait_done(page, state="$:/state/ai-gateway/asking"):
    growth = []
    for _ in range(480):
        await asyncio.sleep(0.125)
        partial = await get_text(page, "$:/state/ai-gateway/answer")
        if len(partial) and (not growth or len(partial) != growth[-1]):
            growth.append(len(partial))
        if await get_text(page, state) == "no":
            return growth
    raise TimeoutError("ask did not finish")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        page.on("console", lambda m: m.text.startswith("[ai-gateway]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))
        # Tripwire: the local-only plugin must never touch the notebook routes.
        # (The wiki's own syncer talks to tw-dev, not the gateway, so any
        # /notebooks/ request can only come from the plugin.)
        notebook_requests = []
        page.on(
            "request",
            lambda r: "/notebooks/" in r.url and notebook_requests.append(r.url),
        )

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        original_url = await get_text(page, CFG_PREFIX + "GatewayURL")
        print("original GatewayURL:", original_url)

        await set_tiddler(page, CFG_PREFIX + "GatewayURL", IN_NET_URL)
        for title, text in SEEDS.items():
            await set_tiddler(page, title, text)
        await asyncio.sleep(4)  # let the syncer push config + seeds

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        await asyncio.sleep(2)
        debug = await get_text(page, "$:/temp/ai-gateway/debug")
        print("debug:", debug)
        assert "(local mode)" in debug, f"unexpected startup banner: {debug}"

        # --- streaming ask over locally-collected notes ---
        await set_tiddler(page, "$:/state/ai-gateway/question",
                          "Using Zebra Facts, explain in detail (several sentences) "
                          "what is unique about each zebra and where zebras live.")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        await wait_done(page)
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; "
            "return {role: f.role, sources: f.sources || '', len: (f.text||'').length}; })"
        )
        print("turns:", json.dumps(turns))
        assert len(turns) == 2 and turns[1]["role"] == "assistant", "expected 2 turns"
        assert turns[1]["len"] > 0, "empty answer"
        assert "Zebra Facts" in turns[1]["sources"], \
            f"seeded note not cited: {turns[1]['sources']!r}"

        # --- streaming + cache round-trip in one probe: the second ask must
        # deliver deltas AND be all hash refs (cache.hits > 0, misses == 0
        # proves the JS sha256 matches the server's canonical_hash) ---
        probe = await page.evaluate(
            "() => new Promise((resolve, reject) => {"
            "  var deltas = 0;"
            "  $tw.TiddlyPWAGateway.askStream("
            "    'What animal has striped legs and where does it live?', null, null, {"
            "    onDelta: function() { deltas += 1; },"
            "    onDone: function(data) { resolve({deltas: deltas, cache: data.cache || null,"
            "      answered: !!(data.answer || '').length}); }"
            "  }).catch(reject);"
            "})"
        )
        print("askStream probe:", probe)
        assert probe["answered"], "streamed ask produced no answer"
        assert probe["deltas"] >= 1, "no delta events arrived over the stream"
        assert probe["cache"] and probe["cache"]["hits"] > 0, \
            f"no cache hits on second ask — JS/Python hash mismatch? {probe['cache']}"
        assert probe["cache"]["misses"] == 0, \
            f"unexpected re-sends on unchanged wiki: {probe['cache']}"

        # --- follow-up uses history ---
        await set_tiddler(page, "$:/state/ai-gateway/question",
                          "Where does that second animal live?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        await wait_done(page)
        n_turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]]').length"
        )
        assert n_turns == 4, f"expected 4 turns after follow-up, got {n_turns}"
        print("follow-up OK")

        # --- related notes, resolved locally ---
        await page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tm-related-notes', param: 'Zebra Facts'})"
        )
        related = ""
        for _ in range(120):
            await asyncio.sleep(0.5)
            related = await get_text(page, "$:/temp/ai-gateway/related/Zebra Facts")
            if related:
                break
        print("related:", related)
        assert "Okapi Notes" in related, f"related missed the striped neighbour: {related!r}"

        # --- summarize: browser-rendered text through the client /generate ---
        await page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tm-summarize-tiddler', param: 'Zebra Facts'})"
        )
        summary = ""
        for _ in range(120):
            await asyncio.sleep(0.5)
            summary = await page.evaluate(
                "() => { var t = $tw.wiki.getTiddler('Zebra Facts');"
                " return (t && t.fields.summary) || ''; }"
            )
            if summary:
                break
        print("summary:", summary[:120])
        assert summary, "summarize wrote no summary field"

        # --- the tripwire must not have fired ---
        assert not notebook_requests, \
            f"plugin touched notebook routes: {notebook_requests}"

        # --- restore config, remove seeds and session state ---
        if original_url:
            await set_tiddler(page, CFG_PREFIX + "GatewayURL", original_url)
        else:
            await page.evaluate(
                "(t) => $tw.wiki.deleteTiddler(t)", CFG_PREFIX + "GatewayURL"
            )
        await page.evaluate(
            "(ts) => ts.forEach(t => $tw.wiki.deleteTiddler(t))", list(SEEDS)
        )
        await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/]] [prefix[$:/state/ai-gateway/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await asyncio.sleep(4)
        await browser.close()
        print("LOCAL MODE VERIFICATION PASSED")


asyncio.run(main())
