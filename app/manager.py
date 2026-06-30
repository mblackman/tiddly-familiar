import asyncio
import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import NotebookConfig

logger = logging.getLogger(__name__)

DRIVER_JS = (Path(__file__).parent / "driver.js").read_text()


class NotebookManager:
    def __init__(self, config: NotebookConfig, context: BrowserContext):
        self.config = config
        self._context = context
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._ready = False

    async def ensure_ready(self):
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await self._unlock()

    async def _unlock(self):
        logger.info("Notebook '%s': opening page at %s", self.config.name, self.config.app_url)
        self._page = await self._context.new_page()
        await self._page.add_init_script(DRIVER_JS)
        await self._page.goto(self.config.app_url, wait_until="load")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

        # Wait until the wiki + sync layer have booted before touching the UI.
        for _ in range(60):
            await asyncio.sleep(0.5)
            ready = await self._page.evaluate(
                "() => typeof window.__gw !== 'undefined' && window.__gw.ready()"
            )
            if ready:
                break
        else:
            raise TimeoutError(
                f"Notebook '{self.config.name}' did not become ready within 30s"
            )

        if self.config.token and self.config.password:
            await self._login()
        elif self.config.password:
            # Generic password-only unlock (non-TiddlyPWA wikis)
            try:
                await self._page.wait_for_selector(
                    self.config.unlock.password_selector, timeout=10_000
                )
                await self._page.fill(
                    self.config.unlock.password_selector, self.config.password
                )
                await self._page.keyboard.press("Enter")
            except Exception:
                pass

        count = await self._page.evaluate(
            "() => $tw.wiki.filterTiddlers('[!is[system]]').length"
        )
        logger.info("Notebook '%s': ready (%d user tiddlers)", self.config.name, count)
        self._ready = True

    async def _login(self):
        """TiddlyPWA sync-server login + remember flow."""
        cfg = self.config.unlock

        # Persistent profiles survive restarts with keys already in IDB.
        remembered = await self._page.evaluate(
            "() => $tw.wiki.getTiddlerText('$:/status/TiddlyPWARemembered', '')"
        )
        if remembered == "yes":
            logger.info("Notebook '%s': session already remembered, skipping login", self.config.name)
            return

        logger.info("Notebook '%s': attempting TiddlyPWA login", self.config.name)

        try:
            await self._page.wait_for_selector(cfg.token_selector, timeout=10_000)
        except Exception:
            logger.info("Notebook '%s': no login form — treating as open wiki", self.config.name)
            return

        await self._page.fill(cfg.token_selector, self.config.token)
        await self._page.fill(cfg.password_selector, self.config.password)
        await self._page.locator(cfg.login_button).click()

        form_gone = False
        try:
            await self._page.wait_for_selector(
                cfg.token_selector, state="hidden", timeout=30_000
            )
            form_gone = True
        except Exception:
            pass

        if not form_gone:
            raise RuntimeError(
                f"Notebook '{self.config.name}': login form did not dismiss — "
                "check TWPWA_OPS_TOKEN and TWPWA_OPS_PASSWORD"
            )

        logger.info("Notebook '%s': login succeeded, waiting for initial sync", self.config.name)
        await asyncio.sleep(2)

        # tiddlypwa-remember pops a browser confirm() — accept it before dispatch.
        self._page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await self._page.evaluate(
            "() => $tw.rootWidget.dispatchEvent({type: 'tiddlypwa-remember', paramObject: {}})"
        )

        for _ in range(20):
            await asyncio.sleep(0.5)
            remembered = await self._page.evaluate(
                "() => $tw.wiki.getTiddlerText('$:/status/TiddlyPWARemembered', '')"
            )
            if remembered == "yes":
                logger.info("Notebook '%s': session remembered", self.config.name)
                return

        logger.warning("Notebook '%s': tiddlypwa-remember did not confirm within 10s", self.config.name)

    async def filter_tiddlers(self, filter_str: str, full: bool = False) -> list:
        await self.ensure_ready()
        if full:
            return await self._page.evaluate(
                "(f) => window.__gw.filterFull(f)", filter_str
            )
        return await self._page.evaluate("(f) => window.__gw.filter(f)", filter_str)

    async def get_tiddler(self, title: str) -> dict | None:
        await self.ensure_ready()
        return await self._page.evaluate("(t) => window.__gw.getTiddler(t)", title)

    async def put_tiddler(self, title: str, fields: dict, text: str) -> bool:
        await self.ensure_ready()
        return await self._page.evaluate(
            "([t, f, x]) => window.__gw.putTiddler(t, f, x)", [title, fields, text]
        )

    async def delete_tiddler(self, title: str) -> bool:
        await self.ensure_ready()
        return await self._page.evaluate("(t) => window.__gw.deleteTiddler(t)", title)

    async def render(self, title: str, mode: str = "plain") -> str:
        await self.ensure_ready()
        return await self._page.evaluate(
            "([t, m]) => window.__gw.render(t, m)", [title, mode]
        )

    async def render_text(self, text: str, mode: str = "plain") -> str:
        await self.ensure_ready()
        return await self._page.evaluate(
            "([x, m]) => window.__gw.renderText(x, m)", [text, mode]
        )

    async def sync(self) -> bool:
        await self.ensure_ready()
        return await self._page.evaluate("() => window.__gw.sync()")

    async def probe(self) -> dict:
        if self._ready and self._page:
            return await self._page.evaluate("() => window.__gw.probe()")

        page = await self._context.new_page()
        try:
            await page.add_init_script(DRIVER_JS)
            await page.goto(self.config.app_url, wait_until="load")
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            return await page.evaluate("() => window.__gw.probe()")
        finally:
            await page.close()

    async def close(self):
        if self._page:
            await self._page.close()
            self._page = None
        await self._context.close()
        self._ready = False


class AppManager:
    def __init__(self):
        self._playwright = None
        self._notebooks: dict[str, NotebookManager] = {}

    async def start(self, configs: dict, profiles_dir: str):
        self._playwright = await async_playwright().start()
        profiles = Path(profiles_dir)
        for name, cfg in configs.items():
            profile_dir = profiles / name
            profile_dir.mkdir(parents=True, exist_ok=True)
            # launch_persistent_context owns the browser process — no separate
            # Browser handle needed. ignore_https_errors is belt-and-suspenders
            # alongside the NSS cert store set up in the Dockerfile.
            context = await self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=True,
                ignore_https_errors=True,
            )
            self._notebooks[name] = NotebookManager(cfg, context)

        # Unlock all notebooks concurrently so the first real request is served
        # immediately without any lazy-init delay.
        results = await asyncio.gather(
            *[nb.ensure_ready() for nb in self._notebooks.values()],
            return_exceptions=True,
        )
        for name, result in zip(self._notebooks, results):
            if isinstance(result, Exception):
                logger.error("Notebook '%s': startup failed: %s", name, result)

    async def stop(self):
        for nb in self._notebooks.values():
            await nb.close()
        if self._playwright:
            await self._playwright.stop()

    def notebook(self, name: str) -> NotebookManager:
        if name not in self._notebooks:
            raise KeyError(name)
        return self._notebooks[name]
