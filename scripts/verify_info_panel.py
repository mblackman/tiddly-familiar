"""Headless check of the info-dropdown redesign (run via
scripts/run_headless.sh, like verify_search.py).

Verifies that summary / related / tags / tasks moved off the note body into a
`$:/tags/TiddlerInfo` tab, and that the new tm-insert-summary / tm-insert-tasks
handlers write editable content into the note body. No gateway calls — it seeds
a `summary` field and a tasks state tiddler directly, then drives the handlers.
"""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
NOTE = "Info Panel Test Note"
TASKS_STATE = "$:/temp/familiar/tasks/" + NOTE


async def wait_ready(page):
    for _ in range(60):
        if await page.evaluate(
            "() => typeof $tw !== 'undefined' && !!$tw.wiki && !!$tw.rootWidget"
        ):
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("wiki did not boot")


async def evj(page, expr, arg=None):
    return await page.evaluate(expr, arg)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.text.startswith("[familiar]") and print("console:", m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))

        await page.goto(WIKI, wait_until="load")
        await wait_ready(page)

        # Seed a note with a summary field; no gateway needed.
        await evj(
            page,
            "() => $tw.wiki.addTiddler(new $tw.Tiddler({"
            " title: '%s', text: 'Original body line.',"
            " summary: 'First sentence. Second sentence.'}))" % NOTE,
        )

        # --- 1. the tab is registered under $:/tags/TiddlerInfo ---
        info_tabs = await evj(
            page,
            "() => $tw.wiki.filterTiddlers("
            "'[all[shadows+tiddlers]tag[$:/tags/TiddlerInfo]]')",
        )
        assert "$:/plugins/mblackman/familiar/ui/InfoPanel" in info_tabs, \
            f"InfoPanel not tagged into the info dropdown: {info_tabs}"
        print("info tab registered:", "OK")

        # --- 2. InfoPanel renders the sections for the note (currentTiddler bound) ---
        panel_html = await evj(
            page,
            "(t) => $tw.wiki.renderText('text/html', 'text/vnd.tiddlywiki',"
            " \"<$transclude tiddler='$:/plugins/mblackman/familiar/ui/InfoPanel'/>\","
            " {variables: {currentTiddler: t}})",
            NOTE,
        )
        for needle in ["AI Summary", "First sentence", "Insert into note",
                       "Related notes", "Suggest tags", "Extract tasks"]:
            assert needle in panel_html, f"info panel missing {needle!r}"
        print("info panel renders summary + action buttons:", "OK")

        # --- 3. the note BODY no longer injects related/tags/tasks ---
        # Render the standard ViewTemplate body chain for the note and confirm
        # the generate buttons are gone (they only live in the info tab now).
        body_html = await evj(
            page,
            "(t) => $tw.wiki.renderText('text/html', 'text/vnd.tiddlywiki',"
            " \"<$transclude tiddler='$:/plugins/mblackman/familiar/ui/ViewTemplate'/>\","
            " {variables: {currentTiddler: t}})",
            NOTE,
        )
        for absent in ["Related notes", "Suggest tags", "Extract tasks", "AI Summary"]:
            assert absent not in body_html, \
                f"note body still injects {absent!r} (should be info-tab only)"
        print("note body no longer injects the moved sections:", "OK")

        # --- 4. tm-insert-summary writes an editable block to the top of the body ---
        await evj(
            page,
            "(t) => $tw.rootWidget.dispatchEvent({type: 'tm-insert-summary', param: t})",
            NOTE,
        )
        await asyncio.sleep(0.3)
        text = await evj(page, "(t) => $tw.wiki.getTiddlerText(t)", NOTE)
        assert "<<<.tc-quote" in text and "First sentence. Second sentence." in text, \
            f"summary not inserted: {text!r}"
        assert "Original body line." in text, "insert dropped the original body"
        assert text.index("First sentence") < text.index("Original body line"), \
            "summary should be prepended above the body"
        print("insert-summary prepends quote block:", "OK")

        # idempotent: a second insert must not duplicate it
        await evj(
            page,
            "(t) => $tw.rootWidget.dispatchEvent({type: 'tm-insert-summary', param: t})",
            NOTE,
        )
        await asyncio.sleep(0.3)
        text2 = await evj(page, "(t) => $tw.wiki.getTiddlerText(t)", NOTE)
        assert text2 == text, "second insert-summary duplicated content"
        print("insert-summary is idempotent:", "OK")

        # --- 5. tm-insert-tasks converts markdown bullets to a wikitext list ---
        await evj(
            page,
            "(s) => $tw.wiki.addTiddler(new $tw.Tiddler({title: s,"
            " text: '- Buy milk\\n- Call Bob', type: 'text/markdown'}))",
            TASKS_STATE,
        )
        await evj(
            page,
            "(t) => $tw.rootWidget.dispatchEvent({type: 'tm-insert-tasks', param: t})",
            NOTE,
        )
        await asyncio.sleep(0.3)
        text3 = await evj(page, "(t) => $tw.wiki.getTiddlerText(t)", NOTE)
        assert "!! Tasks" in text3, f"tasks heading missing: {text3!r}"
        assert "* Buy milk" in text3 and "* Call Bob" in text3, \
            f"tasks not converted to wikitext bullets: {text3!r}"
        print("insert-tasks appends a wikitext task list:", "OK")

        # --- 6. clicking a suggested tag ADDS it, keeping existing tags ---
        await evj(
            page,
            "(t) => { var td = $tw.wiki.getTiddler(t);"
            " $tw.wiki.addTiddler(new $tw.Tiddler(td, {tags: ['Alpha', 'Beta']})); }",
            NOTE,
        )
        await evj(
            page,
            "(t) => $tw.wiki.addTiddler(new $tw.Tiddler("
            "{title: '$:/temp/familiar/tags/' + t, text: 'Gamma'}))",
            NOTE,
        )
        # Mount the real InfoPanel bound to the note and click the Gamma pill.
        await evj(
            page,
            "(t) => {"
            " var d = document.createElement('div'); d.id = 'tag-mount';"
            " document.body.appendChild(d);"
            " var wt = \"<$transclude tiddler='$:/plugins/mblackman/familiar/ui/InfoPanel'/>\";"
            " var w = $tw.wiki.makeWidget($tw.wiki.parseText('text/vnd.tiddlywiki', wt),"
            "   {document: document, parentWidget: $tw.rootWidget,"
            "    variables: {currentTiddler: t}});"
            " w.render(d, null);"
            "}",
            NOTE,
        )
        # find the suggestion button whose text is Gamma
        buttons = page.locator("#tag-mount .fam-tag-suggestion")
        n = await buttons.count()
        clicked = False
        for i in range(n):
            if "Gamma" in (await buttons.nth(i).inner_text()):
                await buttons.nth(i).click()
                clicked = True
                break
        assert clicked, "no Gamma tag-suggestion button rendered"
        await asyncio.sleep(0.3)
        tags = await evj(page, "(t) => $tw.wiki.getTiddler(t).fields.tags || []", NOTE)
        assert set(tags) == {"Alpha", "Beta", "Gamma"}, \
            f"tag-add should append, got: {tags}"
        print("suggested tag adds without replacing existing tags:", tags, "OK")

        # --- clean up ---
        await evj(
            page,
            "() => $tw.wiki.filterTiddlers('[[%s]] [prefix[$:/temp/familiar/]]"
            " [prefix[$:/state/familiar/]]').forEach(t => $tw.wiki.deleteTiddler(t))"
            % NOTE,
        )
        await asyncio.sleep(3)
        await browser.close()
        print("INFO PANEL VERIFICATION PASSED")


asyncio.run(main())
