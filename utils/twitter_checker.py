"""
utils/twitter_checker.py
Long-lived Playwright browser session embedded inside the Discord bot.

This module is intentionally self-contained — it does NOT import from the
twitter-scraper project so there's no sys.path hacking or config conflict.
It replicates only the parts needed: auth + targeted reply search.

Design goals
------------
* One shared browser context for the whole bot lifetime (fast — no cold start
  on every /verify command).
* An asyncio.Lock so concurrent /verify calls queue safely.
* Auto-reconnect: if a page navigation fails after a stale session, the checker
  re-authenticates and retries once before propagating the error.
* All config is passed in explicitly — no global singletons.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

log = logging.getLogger(__name__)

# ── Stealth init script ───────────────────────────────────────────────────────
_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
window.chrome = { runtime: {} };
"""

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── DOM selectors ─────────────────────────────────────────────────────────────
_SEL_ARTICLE        = 'article[data-testid="tweet"]'
_SEL_USER_NAME_BLOK = '[data-testid="User-Name"]'
_SEL_TWEET_TEXT     = '[data-testid="tweetText"]'
_SEL_TWEET_LINK     = 'a[href*="/status/"]'
_SEL_HOME_TIMELINE  = '[data-testid="primaryColumn"]'
_SEL_EMAIL_INPUT    = 'input[autocomplete="username"]'
_SEL_USERNAME_INPUT = 'input[data-testid="ocfEnterTextTextInput"]'
_SEL_PASSWORD_INPUT = 'input[name="password"]'
_SEL_NEXT_BTN       = '[role="button"]:has-text("Next")'
_SEL_LOGIN_BTN      = '[data-testid="LoginForm_Login_Button"]'
_SEL_TOAST          = '[data-testid="toast"]'

LOGIN_URL = "https://x.com/i/flow/login"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MatchedReply:
    """Details of the reply that confirmed the user's participation."""
    twitter_handle:  str    # normalized (no @, lowercase)
    display_name:    str
    reply_text:      str
    reply_url:       str
    timestamp:       str    # ISO datetime string from <time datetime="…">


# ── Checker ───────────────────────────────────────────────────────────────────

class TwitterChecker:
    """Embedded Playwright session used by /verify.

    Lifecycle
    ---------
    checker = await TwitterChecker.create(
        email="you@x.com",
        username="yourhandle",
        password="secret",
        session_dir=Path("data/twitter_session"),
        headless=True,
    )
    match = await checker.find_reply(
        tweet_url="https://x.com/user/status/123",
        twitter_handle="alice",  # normalized, no @
        scroll_steps=15,
    )
    await checker.close()
    """

    def __init__(
        self,
        *,
        email: str,
        username: str,
        password: str,
        session_dir: Path,
        headless: bool,
    ) -> None:
        self._email      = email
        self._username   = username
        self._password   = password
        self._session_dir = session_dir
        self._headless   = headless

        self._pw:      Playwright     | None = None
        self._browser: Browser        | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()   # one scrape at a time

    # ── Constructor ───────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        email: str,
        username: str,
        password: str,
        session_dir: Path,
        headless: bool,
    ) -> "TwitterChecker":
        checker = cls(
            email=email,
            username=username,
            password=password,
            session_dir=session_dir,
            headless=headless,
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        await checker._start_browser()
        log.info("TwitterChecker ready (headless=%s)", headless)
        return checker

    async def close(self) -> None:
        await self._save_state()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        log.info("TwitterChecker closed.")

    # ── Public API ────────────────────────────────────────────────────────────

    async def find_reply(
        self,
        tweet_url: str,
        twitter_handle: str,
        scroll_steps: int = 15,
        scroll_delay_ms: int = 1200,
    ) -> MatchedReply | None:
        """Search a tweet's reply section for a specific @handle.

        Returns a `MatchedReply` on the first matching article, or None if the
        handle isn't found within `scroll_steps` scrolls.

        Thread-safe: concurrent calls are serialized via an asyncio.Lock so the
        shared browser context is never accessed in parallel.
        """
        async with self._lock:
            try:
                return await self._find_with_retry(
                    tweet_url, twitter_handle, scroll_steps, scroll_delay_ms
                )
            except Exception as exc:
                log.error("find_reply failed: %s", exc, exc_info=True)
                raise

    async def scrape_all_replies(
        self,
        tweet_url: str,
        scroll_steps: int = 15,
        scroll_delay_ms: int = 1200,
    ) -> list[MatchedReply]:
        """Scrape *every* reply on a tweet in a single pass.

        Unlike find_reply (which stops at the first match), this collects all
        replies so the caller can cache them and serve many users from one
        scrape. Returns a de-duplicated list of MatchedReply.

        Thread-safe via the same asyncio.Lock.
        """
        async with self._lock:
            try:
                return await self._scrape_all_with_retry(
                    tweet_url, scroll_steps, scroll_delay_ms
                )
            except Exception as exc:
                log.error("scrape_all_replies failed: %s", exc, exc_info=True)
                raise

    # ── Internal scraping ─────────────────────────────────────────────────────

    async def _scrape_all_with_retry(
        self,
        tweet_url: str,
        scroll_steps: int,
        scroll_delay_ms: int,
    ) -> list[MatchedReply]:
        """Collect-all variant with the same stale-session retry logic."""
        page = await self._context.new_page()  # type: ignore[union-attr]
        try:
            return await self._scrape_all(page, tweet_url, scroll_steps, scroll_delay_ms)
        except PWTimeout:
            log.warning("Navigation timeout — session may be stale. Re-authenticating…")
            await page.close()
            await self._reauth()
            page = await self._context.new_page()  # type: ignore[union-attr]
            try:
                return await self._scrape_all(page, tweet_url, scroll_steps, scroll_delay_ms)
            finally:
                await page.close()
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _scrape_all(
        self,
        page: Page,
        tweet_url: str,
        scroll_steps: int,
        scroll_delay_ms: int,
    ) -> list[MatchedReply]:
        log.info("Scraping ALL replies on %s (%d scroll steps)", tweet_url, scroll_steps)

        await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(_SEL_ARTICLE, timeout=15_000)
        await page.wait_for_timeout(2_000)

        seen_urls: set[str] = set()
        replies: list[MatchedReply] = []

        for step in range(scroll_steps):
            articles = await page.query_selector_all(_SEL_ARTICLE)

            for article in articles:
                reply = await self._extract_article(article)
                if reply is None or reply.reply_url in seen_urls:
                    continue
                seen_urls.add(reply.reply_url)
                replies.append(reply)

            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await page.wait_for_timeout(scroll_delay_ms)

            if step > 2:
                h0 = await page.evaluate("document.body.scrollHeight")
                await page.wait_for_timeout(400)
                h1 = await page.evaluate("document.body.scrollHeight")
                if h0 == h1:
                    log.info("Page bottom at step %d — stopping.", step + 1)
                    break

        log.info("Collected %d unique replies from %s", len(replies), tweet_url)
        return replies

    async def _find_with_retry(
        self,
        tweet_url: str,
        handle: str,
        scroll_steps: int,
        scroll_delay_ms: int,
    ) -> MatchedReply | None:
        """Try once; if stale session detected, re-authenticate and retry."""
        page = await self._context.new_page()  # type: ignore[union-attr]
        try:
            result = await self._scrape_for_handle(
                page, tweet_url, handle, scroll_steps, scroll_delay_ms
            )
            return result
        except PWTimeout:
            # Possibly a stale session — close page, re-auth, retry
            log.warning("Navigation timeout — session may be stale. Re-authenticating…")
            await page.close()
            await self._reauth()
            page = await self._context.new_page()  # type: ignore[union-attr]
            try:
                return await self._scrape_for_handle(
                    page, tweet_url, handle, scroll_steps, scroll_delay_ms
                )
            finally:
                await page.close()
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _scrape_for_handle(
        self,
        page: Page,
        tweet_url: str,
        target_handle: str,   # already normalized
        scroll_steps: int,
        scroll_delay_ms: int,
    ) -> MatchedReply | None:
        log.info("Checking @%s on %s (%d scroll steps)", target_handle, tweet_url, scroll_steps)

        await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(_SEL_ARTICLE, timeout=15_000)
        await page.wait_for_timeout(2_000)  # let dynamic content settle

        seen_urls: set[str] = set()

        for step in range(scroll_steps):
            articles = await page.query_selector_all(_SEL_ARTICLE)

            for article in articles:
                reply = await self._extract_article(article)
                if reply is None:
                    continue
                if reply.reply_url in seen_urls:
                    continue
                seen_urls.add(reply.reply_url)

                if reply.twitter_handle == target_handle:
                    log.info(
                        "✅ Found @%s at scroll step %d — reply URL: %s",
                        target_handle, step + 1, reply.reply_url,
                    )
                    return reply

            # Scroll down ~80 % of viewport
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await page.wait_for_timeout(scroll_delay_ms)

            # Early-exit on page bottom
            if step > 2:
                h0 = await page.evaluate("document.body.scrollHeight")
                await page.wait_for_timeout(400)
                h1 = await page.evaluate("document.body.scrollHeight")
                if h0 == h1:
                    log.info("Page bottom at step %d — stopping.", step + 1)
                    break

        log.info("@%s not found in %d scroll steps.", target_handle, scroll_steps)
        return None

    @staticmethod
    async def _extract_article(article) -> MatchedReply | None:
        """Extract data from a single tweet article element."""
        user_el = await article.query_selector(_SEL_USER_NAME_BLOK)
        if user_el is None:
            return None

        spans = await user_el.query_selector_all("span span")
        display_name = (await spans[0].inner_text()).strip() if len(spans) > 0 else ""
        raw_handle   = (await spans[1].inner_text()).strip() if len(spans) > 1 else ""

        normalized = raw_handle.lstrip("@").lower().strip()
        if not normalized:
            return None

        text_el = await article.query_selector(_SEL_TWEET_TEXT)
        text = (await text_el.inner_text()).strip() if text_el else ""

        link_el = await article.query_selector(_SEL_TWEET_LINK)
        href = await link_el.get_attribute("href") if link_el else ""
        reply_url = f"https://x.com{href}" if href and href.startswith("/") else href

        time_el = await article.query_selector("time")
        timestamp = await time_el.get_attribute("datetime") if time_el else ""

        if not reply_url:
            return None

        return MatchedReply(
            twitter_handle=normalized,
            display_name=display_name,
            reply_text=text,
            reply_url=reply_url,
            timestamp=timestamp,
        )

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    async def _start_browser(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context_kwargs: dict = {
            "viewport":    {"width": 1280, "height": 800},
            "user_agent":  _USER_AGENT,
            "locale":      "en-US",
            "timezone_id": "America/New_York",
        }
        state_path = self._state_path()
        if state_path.exists():
            log.info("Restoring Twitter session from %s", state_path)
            context_kwargs["storage_state"] = str(state_path)

        self._context = await self._browser.new_context(**context_kwargs)
        await self._context.add_init_script(_INIT_SCRIPT)

        # Validate or perform login
        page = await self._context.new_page()
        try:
            if not await self._is_authenticated(page):
                log.info("No valid session — logging in.")
                await self._login(page)
                await self._save_state()
                log.info("Login complete — session saved.")
            else:
                log.info("Twitter session is valid.")
        finally:
            await page.close()

    async def _reauth(self) -> None:
        """Close the old context and open a fresh authenticated one."""
        if self._context:
            await self._context.close()
        self._context = await self._browser.new_context(  # type: ignore[union-attr]
            viewport={"width": 1280, "height": 800},
            user_agent=_USER_AGENT,
        )
        await self._context.add_init_script(_INIT_SCRIPT)
        page = await self._context.new_page()
        try:
            await self._login(page)
            await self._save_state()
        finally:
            await page.close()

    async def _is_authenticated(self, page: Page) -> bool:
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_selector(_SEL_HOME_TIMELINE, timeout=8_000)
            return True
        except PWTimeout:
            return False

    async def _login(self, page: Page) -> None:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Step 1 — email
        await self._fill_and_next(page, _SEL_EMAIL_INPUT, self._email, "email")

        # Step 2 — username challenge (optional)
        try:
            await page.wait_for_selector(_SEL_USERNAME_INPUT, timeout=4_000)
            await self._fill_and_next(page, _SEL_USERNAME_INPUT, self._username, "username")
        except PWTimeout:
            pass

        # Step 3 — password
        await page.wait_for_selector(_SEL_PASSWORD_INPUT, timeout=10_000)
        await page.fill(_SEL_PASSWORD_INPUT, self._password)
        await page.click(_SEL_LOGIN_BTN)

        try:
            await page.wait_for_selector(_SEL_HOME_TIMELINE, timeout=20_000)
        except PWTimeout:
            toast = ""
            try:
                toast = await page.text_content(_SEL_TOAST, timeout=3_000) or ""
            except PWTimeout:
                pass
            raise RuntimeError(
                f"Twitter login failed. {('Error: ' + toast) if toast else ''}"
                " Run with TWITTER_HEADLESS=false to debug."
            )

    @staticmethod
    async def _fill_and_next(page: Page, sel: str, value: str, step: str) -> None:
        await page.wait_for_selector(sel, timeout=10_000)
        await page.fill(sel, value)
        log.debug("Filled %s field", step)
        await page.wait_for_timeout(400)
        await page.click(_SEL_NEXT_BTN)
        await page.wait_for_timeout(800)

    def _state_path(self) -> Path:
        return self._session_dir / "state.json"

    async def _save_state(self) -> None:
        if self._context:
            await self._context.storage_state(path=str(self._state_path()))
            log.debug("Twitter session state saved.")
