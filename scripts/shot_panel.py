"""Screenshot the Ask AI sidebar panel in the dev wiki (runs in the gateway
container like verify_plugin_headless.py). Seeds a fake transcript so the
bubble layout is visible, grabs the panel, then cleans up."""

import asyncio

from playwright.async_api import async_playwright

WIKI = "http://tw-dev:8080"
OUT = "/tmp/panel.png"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(WIKI, wait_until="load")
        for _ in range(60):
            if await page.evaluate("() => typeof $tw !== 'undefined' && !!$tw.rootWidget"):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        await page.evaluate("""() => {
            [["000001","user","What is Caddy used for?"],
             ["000002","assistant","Caddy is the reverse proxy for the home lab. It terminates TLS with the Hole Lab CA and routes `*.lab.hole` hostnames to the right containers."],
             ["000003","user","Which port does it listen on?"],
             ["000004","assistant","It listens on ports 80 and 443 on the TrueNAS host."]]
            .forEach(([n, role, text]) => $tw.wiki.addTiddler(new $tw.Tiddler(
                {title: "$:/temp/ai-gateway/chat/" + n, role: role, text: text,
                 type: role === "assistant" ? "text/markdown" : undefined})));
        }""")
        await page.click("text=Ask AI")
        await asyncio.sleep(1)
        panel = page.locator(".ai-gw-panel")
        await panel.screenshot(path=OUT)
        await page.evaluate(
            "() => $tw.wiki.filterTiddlers('[prefix[$:/temp/ai-gateway/]] [prefix[$:/state/ai-gateway/]]')"
            ".forEach(t => $tw.wiki.deleteTiddler(t))"
        )
        await browser.close()
        print("saved", OUT)


asyncio.run(main())
