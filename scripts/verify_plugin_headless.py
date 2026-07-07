"""Headless end-to-end check of the browser plugin against the dev wiki.

Run via scripts/run_headless.sh — a throwaway Playwright container on the
compose network (the gateway image has no browser). Temporarily points the
plugin's GatewayURL config at http://gateway:8787 (gw.lab.cc doesn't resolve
in-container), exercises streaming chat + follow-up history + tag/task
commands, then restores the config.
"""

import asyncio
import json

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
CFG = "$:/config/mblackman/familiar/GatewayURL"
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
        page.on("console", lambda m: m.text.startswith("[familiar]") and print("console:", m.text))
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
        print("debug:", await get_text(page, "$:/temp/familiar/debug"))

        # --- new chat note: templated title + reroll + create ---
        tpl_cfg = "$:/config/mblackman/familiar/ChatNoteTemplate"
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-new-chat-title'})")
        title1 = await get_text(page, "$:/temp/volatile/familiar/new-chat-title")
        print("proposed title (default template):", repr(title1))
        assert title1.startswith("AI Chat: "), "default template not applied"
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-new-chat-title'})")
        title2 = await get_text(page, "$:/temp/volatile/familiar/new-chat-title")
        print("rerolled title:", repr(title2))
        assert title2.startswith("AI Chat: "), "reroll broke the template"
        await set_tiddler(page, tpl_cfg, "Chat {date} {name}")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-new-chat-title'})")
        title3 = await get_text(page, "$:/temp/volatile/familiar/new-chat-title")
        print("custom-template title:", repr(title3))
        assert title3.startswith("Chat 2"), f"custom template not applied: {title3!r}"
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-new-chat-note'})")
        new_note = await get_text(page, "$:/state/familiar/chat-note")
        print("new chat note bound:", repr(new_note))
        assert new_note == title3, "panel not bound to the new note"
        note_fields = await page.evaluate(
            "(n) => { var t = $tw.wiki.getTiddler(n); "
            "return t ? {tags: (t.fields.tags||[]).join(' ')} : null; }",
            new_note,
        )
        assert note_fields and "ai-chat" in note_fields["tags"], "new note missing ai-chat tag"
        assert await get_text(page, "$:/temp/volatile/familiar/new-chat-title") == "", \
            "proposed title not cleared after create"
        story = await page.evaluate("() => $tw.wiki.getTiddlerList('$:/StoryList')")
        assert new_note in story, "new note not opened in the story"
        # user-typed titles get sanitized of filter-breaking chars
        await set_tiddler(page, "$:/temp/volatile/familiar/new-chat-title", "My [weird] {chat}")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-new-chat-note'})")
        weird_note = await get_text(page, "$:/state/familiar/chat-note")
        print("sanitized note title:", repr(weird_note))
        assert weird_note == "My weird chat", f"title not sanitized: {weird_note!r}"
        # clean up the created notes + config so the button click sees defaults
        await page.evaluate(
            "(ts) => ts.forEach(t => $tw.wiki.deleteTiddler(t))",
            [new_note, weird_note, tpl_cfg],
        )
        await set_tiddler(page, "$:/state/familiar/chat-note", "")

        # --- "new AI chat" page-control button: one tap = generated note ---
        await page.click('button[aria-label="new AI chat"]')
        btn_note = await get_text(page, "$:/state/familiar/chat-note")
        print("page-control button created:", repr(btn_note))
        assert btn_note.startswith("AI Chat: "), "button did not create a templated note"
        btn_tags = await page.evaluate(
            "(n) => ($tw.wiki.getTiddler(n).fields.tags || []).join(' ')", btn_note
        )
        assert "ai-chat" in btn_tags, "button note missing ai-chat tag"
        story = await page.evaluate("() => $tw.wiki.getTiddlerList('$:/StoryList')")
        assert btn_note in story, "button note not opened in the story"
        # clean up: drop the note, unbind, and take the test notes off the river
        await page.evaluate("(n) => $tw.wiki.deleteTiddler(n)", btn_note)
        await set_tiddler(page, "$:/state/familiar/chat-note", "")
        await page.evaluate(
            "(ts) => { var sl = $tw.wiki.getTiddler('$:/StoryList'); if (!sl) return; "
            "$tw.wiki.addTiddler(new $tw.Tiddler(sl, "
            "{list: ($tw.wiki.getTiddlerList('$:/StoryList') || []).filter(t => ts.indexOf(t) === -1)})); }",
            [new_note, weird_note, btn_note],
        )
        print("new-chat-note flow OK")

        # --- streaming ask ---
        await set_tiddler(page, "$:/temp/volatile/familiar/question", "What is Caddy used for?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        growth = []
        for _ in range(120):
            await asyncio.sleep(0.5)
            asking = await get_text(page, "$:/state/familiar/asking")
            partial = await get_text(page, "$:/state/familiar/answer")
            if len(partial) and (not growth or len(partial) != growth[-1]):
                growth.append(len(partial))
            if asking == "no":
                break
        print("partial-answer growth samples:", growth[:10], "…" if len(growth) > 10 else "")
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; "
            "return {title: t, role: f.role, type: f.type, sources: f.sources, len: (f.text||'').length}; })"
        )
        print("turns after ask:", json.dumps(turns, indent=1))
        assert len(turns) == 2 and turns[0]["role"] == "user" and turns[1]["role"] == "assistant", "expected 2 turns"
        assert len(growth) >= 2, "answer state never grew incrementally — streaming broken?"

        # --- follow-up uses history ---
        await set_tiddler(page, "$:/temp/volatile/familiar/question", "Which port does it listen on?")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/familiar/asking") == "no":
                break
        turns = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]sort[title]]')"
            ".map(t => $tw.wiki.getTiddler(t).fields.text)"
        )
        print("follow-up answer mentions caddy:", "addy" in turns[-1])
        assert len(turns) == 4, f"expected 4 turns, got {len(turns)}"

        # --- save chat: transcript becomes a real note + turn tiddlers ---
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-save-chat'})")
        note = await get_text(page, "$:/state/familiar/chat-note")
        print("chat saved to note:", repr(note))
        assert note.startswith("AI Chat: "), "chat-note state not set after save"
        temp_left = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]]').length"
        )
        assert temp_left == 0, f"{temp_left} temp turns left after save"
        saved = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]sort[title]]')"
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
        # the whole point: turns are system tiddlers (persist, but stay out of
        # the note feed), while the conversation note itself is a normal note.
        leaked = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[!is[system]tag[ai-chat-turn]]')"
        )
        assert leaked == [], f"chat turns leak into the note feed (Recent): {leaked}"
        note_in_feed = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[!is[system]tag[ai-chat]]').indexOf(n) !== -1", note
        )
        assert note_in_feed, "chat note should surface in the non-system note feed"

        # --- digest: the saved conversation distils into the note body so it
        # becomes retrievable in future asks (debounced ~4s + a generate call) ---
        digest = ""
        for _ in range(40):
            await asyncio.sleep(0.5)
            digest = await get_text(page, note)  # the note's own body text
            if digest.strip():
                break
        marker = await page.evaluate(
            "(n) => $tw.wiki.getTiddler(n).fields['digest-turns'] || ''", note
        )
        print("note digest body:", repr(digest[:160]), "| digest-turns:", marker)
        assert digest.strip(), "conversation note body was never digested"
        assert marker == "4", f"digest-turns marker should be 4, got {marker!r}"
        # a digested note is real retrieval material: non-system, non-empty body
        retrievable = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[!is[system]has[text]]').indexOf(n) !== -1", note
        )
        assert retrievable, "digested chat note should be a retrieval candidate"

        # --- next turn lands in the note while bound ---
        await set_tiddler(page, "$:/temp/volatile/familiar/question", "Summarize that in one sentence.")
        await page.evaluate("() => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai'})")
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/familiar/asking") == "no":
                break
        counts = await page.evaluate(
            "(n) => [$tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]]').length,"
            " $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/chat/]]').length]",
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
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]sort[title]]')"
            ".map(t => $tw.wiki.getTiddler(t).fields.role)",
            note,
        )
        print("persisted roles after reload:", persisted)
        assert len(persisted) == 6, f"expected 6 persisted turns, got {len(persisted)}"
        await page.evaluate(
            "(n) => $tw.rootWidget.dispatchEvent({type: 'tm-resume-chat', param: n})", note
        )
        assert await get_text(page, "$:/state/familiar/chat-note") == note, "resume did not re-bind"
        print("resume re-bound the panel to the note")

        # --- in-note composer: ask directly from the chat note ---
        await set_tiddler(page, "$:/temp/volatile/familiar/note-question/" + note, "And what is AdGuard for?")
        await page.evaluate(
            "(n) => $tw.rootWidget.dispatchEvent({type: 'tm-ask-ai-note', param: n})", note
        )
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await get_text(page, "$:/state/familiar/note-asking/" + note) == "no":
                break
        in_note = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]sort[title]]')"
            ".map(t => $tw.wiki.getTiddler(t).fields.role)",
            note,
        )
        print("roles after in-note ask:", in_note)
        assert len(in_note) == 8 and in_note[-2:] == ["user", "assistant"], \
            f"in-note ask did not append turns: {in_note}"
        assert await get_text(page, "$:/temp/volatile/familiar/note-question/" + note) == "", \
            "in-note question not cleared after send"

        # Remove the (real, synced) chat note + turns so reruns start clean.
        await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]] [[' + n + ']]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))",
            note,
        )

        # --- suggest tags ---
        await page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tm-suggest-tags', param: 'Caddy'})"
        )
        for _ in range(120):
            await asyncio.sleep(0.5)
            tags = await get_text(page, "$:/temp/familiar/tags/Caddy")
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
            tasks = await get_text(page, "$:/temp/familiar/tasks/Caddy")
            if tasks:
                break
        print("tasks:", tasks[:120])
        assert tasks, "no task extraction arrived"

        # Restore the real config and clean the session state.
        await set_tiddler(page, CFG, original_url or "https://gw.lab.cc")
        await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/familiar/]] [prefix[$:/state/familiar/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await asyncio.sleep(4)
        await browser.close()
        print("PLUGIN VERIFICATION PASSED")


asyncio.run(main())
