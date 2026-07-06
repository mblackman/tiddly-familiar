"""Verify the one-time migration of legacy (pre-0.13.0) saved chat turns.

Seeds a wiki with old-style "<note>/turn/NNNNNN" turn tiddlers (non-system,
tagged ai-chat-turn), reloads so startup's migrateChatTurns() runs, and checks
the turns moved under the system SAVED_CHAT_PREFIX while the conversation note
stayed put. Run via scripts/run_headless.sh.
"""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
NOTE = "AI Chat: LEGACY MIGRATION TEST"


async def wait_ready(page):
    for _ in range(60):
        if await page.evaluate(
            "() => typeof $tw !== 'undefined' && !!$tw.wiki && !!$tw.rootWidget"
        ):
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("wiki did not boot")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)

        # Seed a legacy saved chat: note + two old-style turns.
        await page.evaluate(
            "(n) => {"
            "$tw.wiki.addTiddler(new $tw.Tiddler({title: n, tags: ['ai-chat'], text: ''}));"
            "$tw.wiki.addTiddler(new $tw.Tiddler({title: n + '/turn/000001', tags: ['ai-chat-turn'], role: 'user', text: 'legacy question'}));"
            "$tw.wiki.addTiddler(new $tw.Tiddler({title: n + '/turn/000002', tags: ['ai-chat-turn'], role: 'assistant', type: 'text/markdown', text: 'legacy answer'}));"
            "}",
            NOTE,
        )
        await asyncio.sleep(4)  # let the syncer persist the seed to the server

        # Reload → startup migrateChatTurns() runs.
        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)
        await asyncio.sleep(2)

        old_left = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[' + n + '/turn/]]')", NOTE
        )
        migrated = await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]sort[title]]')"
            ".map(t => { var f = $tw.wiki.getTiddler(t).fields; return {role: f.role, text: f.text}; })",
            NOTE,
        )
        note_survives = await page.evaluate(
            "(n) => $tw.wiki.tiddlerExists(n)", NOTE
        )
        leaked = await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[!is[system]tag[ai-chat-turn]]')"
        )
        print("legacy turns still at old title:", old_left)
        print("migrated turns:", migrated)
        print("note survives:", note_survives, "| leaked into feed:", leaked)

        assert old_left == [], f"legacy turns not moved: {old_left}"
        assert [t["role"] for t in migrated] == ["user", "assistant"], "migrated turns wrong"
        assert migrated[0]["text"] == "legacy question", "turn content lost in migration"
        assert note_survives, "conversation note should be left in place"
        assert leaked == [], f"turns still leak into the note feed: {leaked}"

        # Clean up the seeded note + migrated turns.
        await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[$:/familiar/chat/' + n + '/turn/]] [[' + n + ']]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))",
            NOTE,
        )
        await asyncio.sleep(4)
        await browser.close()
        print("CHAT MIGRATION VERIFICATION PASSED")


asyncio.run(main())
