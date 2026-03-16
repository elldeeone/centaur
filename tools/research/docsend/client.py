"""DocSend downloader — browser-use cloud browser + Playwright."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from io import BytesIO

import httpx
from PIL import Image

from centaur_sdk import secret


def _session_value(session: object, *keys: str) -> object | None:
    for key in keys:
        if isinstance(session, dict) and key in session:
            return session[key]
        if hasattr(session, key):
            return getattr(session, key)
    return None


class DocsendClient:
    """Download DocSend documents as PDF via cloud browser."""

    def download(
        self,
        url: str,
        email: str = "",
        passcode: str | None = None,
    ) -> dict:
        """Download a DocSend document as PDF.

        Args:
            url: DocSend URL (e.g. https://docsend.com/view/abc123)
            email: Email for email-gated documents
            passcode: Passcode for password-protected documents

        Returns:
            Dict with status, filename, data (base64 PDF), page_count, error
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, self._run(url, email, passcode)).result()
        return asyncio.run(self._run(url, email, passcode))

    async def _run(self, url: str, email: str, passcode: str | None) -> dict:
        url = url.rstrip("/")
        if not re.match(r"https?://", url):
            url = f"https://{url}"

        api_key = os.environ.get("BROWSER_USE_API_KEY") or secret("BROWSER_USE_API_KEY", "")
        if not api_key:
            return _err("BROWSER_USE_API_KEY not configured")

        try:
            from browser_use_sdk import AsyncBrowserUse
            from playwright.async_api import async_playwright
        except ImportError:
            return _err("browser_use_sdk or playwright not installed")

        bu = AsyncBrowserUse(api_key=api_key)
        session = None
        browser = None

        try:
            # 1. Spin up cloud browser
            browsers = bu.browsers
            create = getattr(browsers, "create_browser_session", None) or browsers.create
            session_kwargs = {
                "timeout": 240,
                "browser_screen_width": 1920,
                "browser_screen_height": 1080,
            }
            proxy_country = os.environ.get("BROWSER_USE_PROXY_COUNTRY", "")
            if proxy_country:
                session_kwargs["proxy_country_code"] = proxy_country.lower()
            profile_id = os.environ.get("BROWSER_USE_PROFILE_ID", "")
            if profile_id:
                session_kwargs["profile_id"] = profile_id
            session = await create(**session_kwargs)

            cdp_url = _session_value(session, "cdp_url", "cdpUrl")
            session_id = _session_value(session, "id", "session_id", "sessionId")
            if not cdp_url or not session_id:
                return _err("Cloud browser session missing cdp_url or id")

            # 2. Connect Playwright over CDP
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(cdp_url)
                contexts = browser.contexts
                ctx = contexts[0] if contexts else await browser.new_context()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                # 3. Navigate
                try:
                    await page.goto(url, wait_until="networkidle", timeout=45000)
                except Exception:
                    pass  # proceed with whatever loaded

                title = (await page.title()).lower()
                if "404" in title or "not found" in title:
                    return _err("Document not found or expired", status="expired")
                if "request could not be satisfied" in title:
                    return _err("Blocked by CloudFront", status="blocked")

                # Dismiss cookie banner early
                try:
                    btn = page.locator('button:has-text("Accept All")').first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                # 4. Handle auth gates — passcode first, then email
                pw_field = await page.query_selector(
                    'input[type="password"], #link_auth_form_passcode, input[name*="passcode"]'
                )
                if not pw_field:
                    for pw_sel in [
                        'input[type="password"]',
                        '#link_auth_form_passcode',
                        'input[name*="passcode"]',
                    ]:
                        pw_field = await page.query_selector(pw_sel)
                        if pw_field:
                            break

                if pw_field:
                    if not passcode:
                        return _err("Password-protected document", status="passcode_required")
                    await _auth(page, email, passcode)
                elif await _has_email_gate(page):
                    if not email:
                        return _err("Email-gated document", status="email_required")
                    await _auth(page, email, passcode)

                # 5. Get slide count (retry a few times for SPA rendering)
                total = 0
                for _ in range(3):
                    total = await _slide_count(page)
                    if total > 0:
                        break
                    await asyncio.sleep(2)
                if total == 0:
                    return _err("Could not determine page count")

                # 6. Fetch all slide image URLs via in-browser fetch (avoids WAF)
                base = page.url.split("?")[0]
                slide_urls = await page.evaluate(
                    """async (args) => {
                        const [base, n] = args;
                        const fetches = [];
                        for (let i = 1; i <= n; i++) {
                            fetches.push(
                                fetch(base + '/page_data/' + i)
                                    .then(r => r.ok ? r.json() : null)
                                    .then(d => d ? (d.imageUrl || d.directImageUrl || null) : null)
                                    .catch(() => null)
                            );
                        }
                        return Promise.all(fetches);
                    }""",
                    [base, total],
                )
                valid = [u for u in (slide_urls or []) if u]
                if not valid:
                    return _err("Failed to fetch slide image URLs", page_count=total)

        except Exception as e:
            return _err(str(e))
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if session:
                try:
                    sid = str(_session_value(session, "id", "session_id", "sessionId"))
                    stop = getattr(bu.browsers, "update_browser_session", None)
                    if stop:
                        await stop(session_id=sid, action="stop")
                    elif hasattr(bu.browsers, "stop"):
                        await bu.browsers.stop(sid)
                except Exception:
                    pass

        # 7. Download images in parallel (S3 URLs — no CloudFront gate)
        images = await _download_images(valid)
        good = [img for img in images if img is not None]
        if not good:
            return _err("Failed to download slide images", page_count=total)

        # 8. Assemble PDF
        buf = BytesIO()
        good[0].save(buf, "PDF", save_all=True, append_images=good[1:] if len(good) > 1 else [])

        slug_m = re.search(r"docsend\.com/view/(?:s/)?([a-zA-Z0-9]+)", url)
        slug = slug_m.group(1) if slug_m else "document"

        return {
            "status": "ok",
            "filename": f"docsend_{slug}.pdf",
            "data": base64.b64encode(buf.getvalue()).decode(),
            "mime_type": "application/pdf",
            "page_count": total,
            "downloaded": len(good),
            "error": None if len(good) == total else f"Got {len(good)}/{total} slides",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _has_email_gate(page) -> bool:
    for sel in [
        '#prompt input[type="email"]',
        '.ReactModal__Content input[type="email"]',
        '[class*="auth"] input[type="email"]',
        '.modal input[type="email"]',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                box = await loc.bounding_box(timeout=2000)
                if box and box["width"] > 50:
                    return True
        except Exception:
            continue
    return False


async def _auth(page, email: str, passcode: str | None) -> None:
    # Fill email if present (also needed for passcode-gated docs)
    for sel in [
        '#link_auth_form_email',
        '#new_link_auth_form input[type="email"]',
        '#prompt input[type="email"]',
        '.ReactModal__Content input[type="email"]',
        '#email[type="email"]',
        'input[type="email"]',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.fill(email or "user@example.com")
                await asyncio.sleep(0.5)
                break
        except Exception:
            continue

    if passcode:
        for sel in [
            '#link_auth_form_passcode',
            'input[type="password"]',
            'input[name*="passcode"]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(passcode)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

    for sel in [
        'button:has-text("Continue")',
        'button:has-text("Submit")',
        'button:has-text("Confirm")',
        'button[type="submit"]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                break
        except Exception:
            continue
    else:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(2)


async def _slide_count(page) -> int:
    for sel in [".toolbar-page-indicator", ".page-label", '[class*="page-indicator"]']:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.text_content()
                m = re.search(r"(\d+)\s*/\s*(\d+)", text or "")
                if m:
                    return int(m.group(2))
        except Exception:
            continue
    thumbs = await page.query_selector_all('[class*="document-thumb-container"]')
    if thumbs:
        nums = []
        for t in thumbs:
            n = await t.get_attribute("data-page-num")
            if n:
                nums.append(int(n))
        if nums:
            return max(nums)
    return 0


async def _download_images(urls: list[str]) -> list[Image.Image | None]:
    async with httpx.AsyncClient(timeout=30.0) as client:

        async def fetch(img_url: str) -> Image.Image | None:
            try:
                r = await client.get(img_url)
                r.raise_for_status()
                rgba = Image.open(BytesIO(r.content))
                rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                rgb.paste(rgba, mask=rgba.split()[3] if rgba.mode == "RGBA" else None)
                return rgb
            except Exception:
                return None

        return list(await asyncio.gather(*[fetch(u) for u in urls]))


def _err(error: str, status: str = "error", page_count: int = 0) -> dict:
    return {
        "status": status,
        "error": error,
        "data": None,
        "page_count": page_count,
        "filename": None,
    }


def _client() -> DocsendClient:
    return DocsendClient()
