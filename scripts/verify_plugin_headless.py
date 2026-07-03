"""Headless end-to-end check of the browser plugin against the dev wiki.

Runs INSIDE the gateway container (docker cp + docker exec), where Chromium
and the compose network live. Temporarily points the plugin's GatewayURL
config at http://gateway:8787 (gw.lab.cc doesn't resolve in-container),
exercises streaming chat + follow-up history + tag/task commands, then
restores the config.
"""

import asyncio
import json

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG = "$:/config/mblackman/ai-gateway/GatewayURL"
IN_NET_URL = "http://gateway:8787"


async def eval_tw(page, js):
    return await page.evaluate(js)


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
    return await page.evaluate(
        "(t) => $tw.wiki.getTiddlerText(t, '')", title
    )


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        page.on("console", lambda m: m.text.startswith("[ai-gateway]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        original_url = await get_text(page, CFG)
        print("original GatewayURL:", original_url)
        await set_tiddler(page, CFG, IN_NET_URL)
        await asyncio.sleep(4)  # let the syncer push the config to the server

        # Fresh load so the startup module reads the in-network URL.
        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        await asyncio.sleep(2)
        print("debug:", await get_text(page, "$:/temp/ai-gateway/debug"))

        # --- streaming ask ---
        await set_tiddler(page, "$:/state/ai-gateway/question", "What is Caddy used for?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        growth = []
        for _ in range(120):
            await asyncio.sleep(0.5)
            asking = await get_text(page, "$:/state/ai-gateway/asking")
            partial = await get_text(page, "$:/state/ai-gateway/answer")
            if len(partial) and (not growth or len(partial) != growth[-1]):
                growth.append(len(partial))
            if asking == "no":
                break
        print("partial-answer growth samples:", growth[:10], "…" if len(growth) > 10 else "")
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; "
            "return {title: t, role: f.role, type: f.type, sources: f.sources, len: (f.text||'').length}; })"
        )
        print("turns after ask:", json.dumps(turns, indent=1))
        assert len(turns) == 2 and turns[0]["role"] == "user" and turns[1]["role"] == "assistant", "expected 2 turns"
        assert len(growth) >= 2, "answer state never grew incrementally — streaming broken?"

        # --- follow-up uses history ---
        await set_tiddler(page, "$:/state/ai-gateway/question", "Which port does it listen on?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/ai-gateway/asking") == "no":
                break
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]sort[title]]')"
            ".map(t => $tw.wiki.getTiddler(t).fields.text)"
        )
        print("follow-up answer mentions caddy:", "addy" in turns[-1])
        assert len(turns) == 4, f"expected 4 turns, got {len(turns)}"

        # --- save chat: transcript becomes a real note + turn tiddlers ---
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-save-chat'})")
        note = await get_text(page, "$:/state/ai-gateway/chat-note")
        print("chat saved to note:", repr(note))
        assert note.startswith("AI Chat: "), "chat-note state not set after save"
        temp_left = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]]').length"
        )
        assert temp_left == 0, f"{temp_left} temp turns left after save"
        saved = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[' + n + '/turn/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; "
            "return {role: f.role, tags: (f.tags||[]).join(' ')}; })",
            note,
        )
        print("saved turns:", json.dumps(saved))
        assert len(saved) == 4 and all(t["tags"] == "ai-chat-turn" for t in saved), "bad saved turns"
        note_tags = await page.evaluate(
            "(n) => ($tw.wiki.getTiddler(n).fields.tags || []).join(' ')", note
        )
        assert "ai-chat" in note_tags, "chat note not tagged ai-chat"

        # --- next turn lands in the note while bound ---
        await set_tiddler(page, "$:/state/ai-gateway/question", "Summarize that in one sentence.")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/ai-gateway/asking") == "no":
                break
        counts = await page.evaluate(
            "(n) => [$tw.wiki.filterTiddlers('[prefix[' + n + '/turn/]]').length,"
            " $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/chat/]]').length]",
            note,
        )
        print("turns in note / temp after bound ask:", counts)
        assert counts == [6, 0], f"bound ask went to the wrong place: {counts}"

        # --- persistence: turns survive a full reload; resume re-binds ---
        await asyncio.sleep(4)  # let the syncer push the note + turns
        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        await asyncio.sleep(2)
        persisted = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[' + n + '/turn/]sort[title]]')"
            ".map(t => $tw.wiki.getTiddler(t).fields.role)",
            note,
        )
        print("persisted roles after reload:", persisted)
        assert len(persisted) == 6, f"expected 6 persisted turns, got {len(persisted)}"
        await page.evaluate(
            "(n) => $tw.rootWidget.dispatchEvent({type: 'tm-resume-chat', param: n})", note
        )
        assert await get_text(page, "$:/state/ai-gateway/chat-note") == note, "resume did not re-bind"
        print("resume re-bound the panel to the note")

        # Remove the (real, synced) chat note + turns so reruns start clean.
        await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[' + n + '/turn/]] [[' + n + ']]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))",
            note,
        )

        # --- suggest tags ---
        await page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tm-suggest-tags', param: 'Caddy'})"
        )
        for _ in range(120):
            await asyncio.sleep(0.5)
            tags = await get_text(page, "$:/temp/ai-gateway/tags/Caddy")
            if tags:
                break
        print("suggested tags:", tags)
        assert tags, "no tag suggestions arrived"

        # --- extract tasks ---
        await page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tm-extract-tasks', param: 'Caddy'})"
        )
        for _ in range(120):
            await asyncio.sleep(0.5)
            tasks = await get_text(page, "$:/temp/ai-gateway/tasks/Caddy")
            if tasks:
                break
        print("tasks:", tasks[:120])
        assert tasks, "no task extraction arrived"

        # Restore the real config and clean the session state.
        await set_tiddler(page, CFG, original_url or "https://gw.lab.cc")
        await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/]] [prefix[$:/state/ai-gateway/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await asyncio.sleep(4)
        await browser.close()
        print("PLUGIN VERIFICATION PASSED")


asyncio.run(main())
