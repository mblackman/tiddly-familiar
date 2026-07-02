import asyncio
import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import NotebookConfig

logger = logging.getLogger(__name__)

DRIVER_JS = (Path(__file__).parent / "driver.js").read_text()

# Substrings Playwright uses when the page/context/browser has gone away. Matching
# on message keeps us decoupled from Playwright's exact exception classes across
# versions (TargetClosedError, Error, etc.).
_PAGE_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "target closed",
    "execution context was destroyed",
    "page has been closed",
    "browser has been closed",
    "has been closed",
)


def _is_page_closed_error(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _PAGE_CLOSED_MARKERS)


class NotebookManager:
    def __init__(self, config: NotebookConfig, context: BrowserContext):
        self.config = config
        self._context = context
        self._page: Page | None = None
        # _lock serializes (re)initialization; _op_lock serializes the actual
        # page operations. They must be distinct: operations call ensure_ready(),
        # so sharing one lock would deadlock.
        self._lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()
        self._ready = False

    async def ensure_ready(self):
        # A closed page can't serve anything — force a re-unlock rather than
        # short-circuiting on a stale _ready flag.
        if self._ready and self._page is not None and self._page.is_closed():
            self._ready = False
        if self._ready:
            return
        async with self._lock:
            if self._ready and self._page is not None and self._page.is_closed():
                self._ready = False
            if self._ready:
                return
            await self._unlock()

    async def _eval(self, js: str, arg=None):
        """Run a page.evaluate under the op-lock, transparently reconnecting once if
        the page/session died underneath us."""
        await self.ensure_ready()
        async with self._op_lock:
            try:
                if arg is None:
                    return await self._page.evaluate(js)
                return await self._page.evaluate(js, arg)
            except Exception as e:
                if not _is_page_closed_error(e):
                    raise
                logger.warning(
                    "Notebook '%s': page died mid-op, reconnecting", self.config.name
                )
                self._ready = False
        # Re-init outside the op-lock (ensure_ready takes _lock), then retry once.
        await self.ensure_ready()
        async with self._op_lock:
            if arg is None:
                return await self._page.evaluate(js)
            return await self._page.evaluate(js, arg)

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
        if full:
            return await self._eval("(f) => window.__gw.filterFull(f)", filter_str)
        return await self._eval("(f) => window.__gw.filter(f)", filter_str)

    async def get_tiddler(self, title: str) -> dict | None:
        return await self._eval("(t) => window.__gw.getTiddler(t)", title)

    async def put_tiddler(self, title: str, fields: dict, text: str) -> bool:
        return await self._eval(
            "([t, f, x]) => window.__gw.putTiddler(t, f, x)", [title, fields, text]
        )

    async def delete_tiddler(self, title: str) -> bool:
        return await self._eval("(t) => window.__gw.deleteTiddler(t)", title)

    async def render(self, title: str, mode: str = "plain") -> str:
        return await self._eval("([t, m]) => window.__gw.render(t, m)", [title, mode])

    async def render_text(self, text: str, mode: str = "plain") -> str:
        return await self._eval(
            "([x, m]) => window.__gw.renderText(x, m)", [text, mode]
        )

    async def sync(self) -> bool:
        return await self._eval("() => window.__gw.sync()")

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
            # Browser handle needed. TLS for *.lab.hole is verified against the
            # Hole Lab root CA in the NSS store (set up in the Dockerfile);
            # ignoring HTTPS errors here would defeat that pinning.
            context = await self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=True,
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
