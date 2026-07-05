"""Screenshot an ai-chat note rendered in the story river (run via
scripts/run_headless.sh, like shot_panel.py). Seeds a saved chat note with
turns, opens it, grabs the tiddler frame, then cleans up."""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
OUT = "/tmp/note_chat.png"
NOTE = "AI Chat: screenshot demo"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        await page.goto(WIKI, wait_until="load")
        for _ in range(60):
            if await page.evaluate("() => typeof $tw !== 'undefined' && !!$tw.rootWidget"):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        await page.evaluate("""(note) => {
            $tw.wiki.addTiddler(new $tw.Tiddler({title: note, tags: ["ai-chat"], text: ""}));
            [["000001","user","What is Caddy used for?"],
             ["000002","assistant","Caddy is the reverse proxy for the home lab. It terminates TLS with the Hole Lab CA and routes `*.lab.hole` hostnames to the right containers."],
             ["000003","user","Which port does it listen on?"],
             ["000004","assistant","It listens on ports 80 and 443 on the TrueNAS host."]]
            .forEach(([n, role, text]) => $tw.wiki.addTiddler(new $tw.Tiddler(
                {title: note + "/turn/" + n, role: role, text: text, tags: ["ai-chat-turn"],
                 type: role === "assistant" ? "text/markdown" : undefined})));
            $tw.wiki.addTiddler(new $tw.Tiddler({title: "$:/StoryList", list: [note]}));
        }""", NOTE)
        await asyncio.sleep(1.5)
        frame = page.locator("div[data-tiddler-title='" + NOTE + "']")
        await frame.screenshot(path=OUT)
        await page.evaluate(
            "(n) => $tw.wiki.filterTiddlers('[prefix[' + n + ']] [prefix[$:/state/familiar/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))", NOTE
        )
        await browser.close()
        print("saved", OUT)


asyncio.run(main())
