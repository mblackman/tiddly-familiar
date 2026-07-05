"""Headless end-to-end check of the (local-only) plugin against the dev wiki.

Run via scripts/run_headless.sh, like verify_plugin_headless.py.
The plugin background-syncs note content (idle-time scan + change-event
pushes); a network tripwire records any request touching /notebooks/ and
fails the run (those routes no longer exist server-side — the plugin must
never call them). Verifies the background warm (the FIRST ask is already all
hash refs with no /notes/check preflight), the streaming ask, history
follow-up, the debounced change push, local related-notes, and
browser-rendered Summarize. Restores config and cleans up.
"""

import asyncio
import json

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG_PREFIX = "$:/config/mblackman/familiar/"
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


async def wait_done(page, state="$:/state/familiar/asking"):
    growth = []
    for _ in range(480):
        await asyncio.sleep(0.125)
        partial = await get_text(page, "$:/state/familiar/answer")
        if len(partial) and (not growth or len(partial) != growth[-1]):
            growth.append(len(partial))
        if await get_text(page, state) == "no":
            return growth
    raise TimeoutError("ask did not finish")


async def ask_stream_probe(page, question):
    """askStream via the JS API, returning {deltas, cache, answered}."""
    return await page.evaluate(
        "(q) => new Promise((resolve, reject) => {"
        "  var deltas = 0;"
        "  $tw.Familiar.askStream(q, null, null, {"
        "    onDelta: function() { deltas += 1; },"
        "    onDone: function(data) { resolve({deltas: deltas, cache: data.cache || null,"
        "      answered: !!(data.answer || '').length}); }"
        "  }).catch(reject);"
        "})",
        question,
    )


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        page.on("console", lambda m: m.text.startswith("[familiar]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))
        # Tripwire: the local-only plugin must never touch the notebook routes.
        # (The wiki's own syncer talks to tw-dev, not the gateway, so any
        # /notebooks/ request can only come from the plugin.)
        notebook_requests = []
        page.on(
            "request",
            lambda r: "/notebooks/" in r.url and notebook_requests.append(r.url),
        )
        # Counts /notes/check preflights: allowed during the background warm,
        # forbidden on steady-state asks (payloads must be pure hash refs).
        check_requests = []
        page.on(
            "request",
            lambda r: "/notes/check" in r.url and check_requests.append(r.url),
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
        debug = await get_text(page, "$:/temp/familiar/debug")
        print("debug:", debug)
        # The warm-done message may have already overwritten the ready banner
        # (dbg is a single tiddler); either one proves the local-mode startup.
        assert "(local mode)" in debug or "sync warm" in debug, \
            f"unexpected startup banner: {debug}"

        # --- background warm: idle scan + /notes/check + /notes/sync ---
        status = ""
        for _ in range(180):
            status = await get_text(page, "$:/temp/familiar/sync-status")
            if status in ("ready", "degraded"):
                break
            await asyncio.sleep(0.5)
        print("sync-status:", status)
        assert status == "ready", f"background warm did not finish: {status!r}"
        warm_checks = len(check_requests)
        assert warm_checks >= 1, "warm never called /notes/check"

        # --- the FIRST ask is already all hash refs over a warm server:
        # hits > 0 with misses == 0 proves the background sync (and the JS
        # sha256 parity), and no new /notes/check proves there's no preflight
        # left on the ask path ---
        probe = await ask_stream_probe(
            page, "What animal has striped legs and where does it live?"
        )
        print("first-ask probe:", probe)
        assert probe["answered"], "streamed ask produced no answer"
        assert probe["deltas"] >= 1, "no delta events arrived over the stream"
        assert probe["cache"] and probe["cache"]["hits"] > 0, \
            f"no cache hits on first ask — background sync broken? {probe['cache']}"
        assert probe["cache"]["misses"] == 0, \
            f"unexpected full sends after warm: {probe['cache']}"
        assert len(check_requests) == warm_checks, \
            f"steady-state ask still preflights /notes/check: {check_requests}"

        # --- streaming ask over locally-collected notes ---
        await set_tiddler(page, "$:/state/familiar/question",
                          "Using Zebra Facts, explain in detail (several sentences) "
                          "what is unique about each zebra and where zebras live.")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        await wait_done(page)
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; "
            "return {role: f.role, sources: f.sources || '', len: (f.text||'').length}; })"
        )
        print("turns:", json.dumps(turns))
        assert len(turns) == 2 and turns[1]["role"] == "assistant", "expected 2 turns"
        assert turns[1]["len"] > 0, "empty answer"
        assert "Zebra Facts" in turns[1]["sources"], \
            f"seeded note not cited: {turns[1]['sources']!r}"

        # --- change-event sync: edit a note, wait out the 5s debounce, and
        # the next ask must still be all refs with no preflight (the edit was
        # pushed in the background via /notes/sync) ---
        checks_before_edit = len(check_requests)
        await set_tiddler(page, "Zebra Facts", SEEDS["Zebra Facts"] +
                          " Zebras sleep standing up.")
        await asyncio.sleep(10)  # debounce (5s) + push
        probe = await ask_stream_probe(
            page, "How do zebras sleep and what makes their stripes special?"
        )
        print("post-edit probe:", probe)
        assert probe["cache"] and probe["cache"]["misses"] == 0, \
            f"edited note was not background-pushed: {probe['cache']}"
        assert probe["cache"]["hits"] > 0, f"no refs after edit: {probe['cache']}"
        assert len(check_requests) == checks_before_edit, \
            f"ask after edit still preflights /notes/check: {check_requests}"

        # --- follow-up uses history ---
        await set_tiddler(page, "$:/state/familiar/question",
                          "Where does that second animal live?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        await wait_done(page)
        n_turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]]').length"
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
            related = await get_text(page, "$:/temp/familiar/related/Zebra Facts")
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
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/]] [prefix[$:/state/familiar/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await asyncio.sleep(4)
        await browser.close()
        print("LOCAL MODE VERIFICATION PASSED")


asyncio.run(main())
