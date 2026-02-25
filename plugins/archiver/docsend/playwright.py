#!/usr/bin/env python3
"""
DocSend Scraper using browser-use CLOUD + Playwright control.

Strategy:
1. Create a browser session via browser-use cloud API (gets CDP URL)
2. Connect Playwright to the cloud browser via CDP
3. Run deterministic Playwright automation (no LLM needed)
4. Use DocSend's /page_data/ API to get slide URLs
5. Download images and compile to PDF

This gives us:
- Cloud browsers (no local display needed)
- No headless detection issues (browser-use handles this)
- Deterministic control via Playwright (no LLM costs)
"""

import asyncio
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import httpx
from PIL import Image
from io import BytesIO
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

from browser_use_sdk import AsyncBrowserUse

# Configuration
DEFAULT_EMAIL = "ricardo@paradigm.xyz"
OUTPUT_DIR = Path("./docsend_output")
API_KEY = os.environ.get("PARCHIVER_BROWSER_USE_API_KEY")
BROWSER_USE_PROXY_COUNTRY = os.environ.get("PARCHIVER_BROWSER_USE_PROXY_COUNTRY")
BROWSER_USE_PROFILE_ID = os.environ.get("PARCHIVER_BROWSER_USE_PROFILE_ID")
MAX_PARALLEL = 5


class ScrapeStatus(Enum):
    """Possible scrape outcomes."""
    SUCCESS = "success"
    PARTIAL = "partial"  # Some slides failed but got most
    PASSWORD_REQUIRED = "password_required"
    LINK_EXPIRED = "link_expired"
    NO_SLIDES = "no_slides"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class ScrapeConfig:
    """Configuration for scraping behavior."""
    # Timeouts (in ms)
    page_load_timeout: int = 45000
    auth_timeout: int = 5000  # Short timeout - email field should appear quickly
    network_idle_timeout: int = 20000

    # Retries
    max_retries: int = 3
    retry_delay: float = 2.0  # seconds

    # Browser session
    browser_timeout: int = 240  # seconds (max 240)
    browser_width: int = 1920
    browser_height: int = 1080

    # Thresholds
    min_success_ratio: float = 0.5  # Consider partial success if > 50% slides


@dataclass
class ScrapeResult:
    """Result of a scrape operation."""
    url: str
    company: str
    status: ScrapeStatus = ScrapeStatus.ERROR
    total_pages: int = 0
    downloaded: int = 0
    failed_slides: list = field(default_factory=list)
    error: Optional[str] = None
    pdf_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "company": self.company,
            "status": self.status.value,
            "total_pages": self.total_pages,
            "downloaded": self.downloaded,
            "failed_slides": self.failed_slides,
            "error": self.error,
            "pdf_path": self.pdf_path,
        }


async def retry_async(
    func,
    max_retries: int = 3,
    delay: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: callable = None,
):
    """Retry an async function with exponential backoff."""
    last_exception = None
    for attempt in range(max_retries):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait_time = delay * (2 ** attempt)  # Exponential backoff
                if on_retry:
                    on_retry(attempt + 1, max_retries, e, wait_time)
                await asyncio.sleep(wait_time)
    raise last_exception


# Default config
DEFAULT_CONFIG = ScrapeConfig()


async def enter_password(page, password: str, config: ScrapeConfig = DEFAULT_CONFIG) -> bool:
    """Enter password on a password-protected DocSend page.

    Handles both standalone password forms and combined email+password forms
    (DocSend uses type="text" passcode fields, not type="password").

    Returns:
        True if password was accepted, False otherwise.
    """
    try:
        # DocSend passcode fields use type="text", not type="password"
        pw_selectors = [
            'input[type="password"]',
            '#link_auth_form_passcode',
            'input[name*="passcode"]',
        ]

        password_field = None
        for sel in pw_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state='visible', timeout=3000)
                password_field = loc
                break
            except:
                continue

        if not password_field:
            return False

        # If email field is on the same form, fill it first.
        # Use form-scoped selector to avoid matching the hidden
        # feedback/chat email field (#feedback_sender_email).
        # Includes folder-style ReactModal email fields.
        email_selectors = [
            '#link_auth_form_email',
            '#new_link_auth_form input[type="email"]',
            '.js-auth-form_email-field',
            '.ReactModal__Content input[type="email"]',
            '#email[type="email"]',
        ]
        for em_sel in email_selectors:
            try:
                email_input = page.locator(em_sel).first
                await email_input.wait_for(state='visible', timeout=2000)
                await email_input.fill(DEFAULT_EMAIL)
                await asyncio.sleep(0.5)
                break
            except:
                continue

        await password_field.fill(password)
        await asyncio.sleep(0.5)

        # Try to find and click submit button
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Continue")',
            'button:has-text("Confirm")',
            'button:has-text("Enter")',
        ]

        clicked = False
        for selector in submit_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    clicked = True
                    break
            except:
                continue

        if not clicked:
            await page.keyboard.press('Enter')

        # Wait for navigation/load
        await page.wait_for_load_state('networkidle', timeout=config.network_idle_timeout)
        await asyncio.sleep(2)
        # Check if still on password/passcode page (single-doc form or folder modal)
        still_on_auth = await page.query_selector(
            'input[type="password"], #link_auth_form_passcode, input[name*="passcode"]'
        )
        if still_on_auth:
            # Could be a hidden field after modal dismissed — check visibility
            try:
                is_visible = await page.locator(
                    'input[type="password"], #link_auth_form_passcode, input[name*="passcode"]'
                ).first.is_visible(timeout=1000)
                if is_visible:
                    return False
            except:
                pass

        return True

    except PlaywrightTimeout:
        return False
    except Exception as e:
        print(f"[WARN] Password entry error: {e}")
        return False


async def get_slide_count(page) -> int:
    """Get total slide count from page indicator."""
    selectors = ['.toolbar-page-indicator', '.page-label', '[class*="page-indicator"]']
    for selector in selectors:
        try:
            indicator = await page.query_selector(selector)
            if indicator:
                text = await indicator.text_content()
                match = re.search(r'(\d+)\s*/\s*(\d+)', text)
                if match:
                    return int(match.group(2))
        except:
            continue
    return 0


async def fetch_slide_urls(
    page,
    total_pages: int,
    company: str,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> tuple[list[str], list[int]]:
    """Fetch all slide image URLs via the page_data API.

    Returns:
        Tuple of (urls list, failed slide numbers)
    """
    urls = []
    failed = []
    base_url = page.url.split('?')[0]

    for i in range(1, total_pages + 1):
        url = None
        last_error = None

        for attempt in range(config.max_retries):
            try:
                result = await page.evaluate(f"""
                    (async () => {{
                        const resp = await fetch('{base_url}/page_data/{i}');
                        if (!resp.ok) return {{error: 'HTTP ' + resp.status}};
                        const text = await resp.text();
                        if (!text) return {{error: 'Empty response'}};
                        const data = JSON.parse(text);
                        return {{url: data.imageUrl || data.directImageUrl || null}};
                    }})()
                """)

                if result and result.get('url'):
                    url = result['url']
                    break
                elif result and result.get('error'):
                    last_error = result['error']
                    if 'HTTP 4' in last_error:  # 4xx errors - don't retry
                        break
            except Exception as e:
                last_error = str(e)

            if attempt < config.max_retries - 1:
                await asyncio.sleep(config.retry_delay)

        if url:
            urls.append(url)
        else:
            urls.append(None)
            failed.append(i)
            if last_error:
                print(f"  [WARN] {company}: Slide {i} URL fetch failed: {last_error}")

    return urls, failed


async def download_images(
    urls: list[str],
    output_dir: Path,
    company: str,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> tuple[list[Path], list[int]]:
    """Download images from URLs with retry logic.

    Returns:
        Tuple of (downloaded paths, failed slide numbers)
    """
    downloaded = []
    failed = []

    async with httpx.AsyncClient(timeout=30.0) as http:
        for i, url in enumerate(urls, 1):
            if not url:
                failed.append(i)
                continue

            success = False
            last_error = None

            for attempt in range(config.max_retries):
                try:
                    response = await http.get(url)
                    response.raise_for_status()

                    img_path = output_dir / f"slide_{i:03d}.png"
                    img = Image.open(BytesIO(response.content))
                    img.save(img_path, "PNG")
                    downloaded.append(img_path)
                    success = True
                    break

                except httpx.HTTPStatusError as e:
                    last_error = f"HTTP {e.response.status_code}"
                    if e.response.status_code in (401, 403, 404):
                        # Don't retry auth/not found errors
                        break
                except Exception as e:
                    last_error = str(e)

                if attempt < config.max_retries - 1:
                    await asyncio.sleep(config.retry_delay)

            if not success:
                failed.append(i)
                print(f"  [WARN] {company}: Slide {i} download failed: {last_error}")

    return downloaded, failed


async def create_pdf(image_paths: list[Path], output_path: Path):
    """Create PDF from images."""
    if not image_paths:
        return

    images = [Image.open(p) for p in sorted(image_paths)]
    rgb_images = [img.convert('RGB') if img.mode != 'RGB' else img for img in images]

    if rgb_images:
        rgb_images[0].save(
            output_path, "PDF", save_all=True,
            append_images=rgb_images[1:] if len(rgb_images) > 1 else []
        )


async def scrape_docsend(
    url: str,
    company: str,
    output_dir: Path,
    email: str,
    semaphore: asyncio.Semaphore,
    password: str | None = None,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> ScrapeResult:
    """Scrape a single DocSend URL using cloud browser + Playwright.

    Args:
        url: DocSend URL to scrape
        company: Company name for output organization
        output_dir: Directory to save output files
        email: Email to use for email-gated documents
        semaphore: Semaphore for concurrency control
        password: Optional password for password-protected documents
        config: Scraping configuration
    """

    async with semaphore:
        print(f"\n[START] {company}: {url}")

        result = ScrapeResult(url=url, company=company)

        # Create output directory
        company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
        company_dir.mkdir(parents=True, exist_ok=True)

        bu_client = AsyncBrowserUse(api_key=API_KEY)
        browser_session = None
        playwright_browser = None

        try:
            # Step 1: Create cloud browser session with retry
            print(f"[CLOUD] {company}: Creating browser session...")

            async def create_session():
                session_kwargs = {
                    "timeout": config.browser_timeout,
                    "browser_screen_width": config.browser_width,
                    "browser_screen_height": config.browser_height,
                }
                if BROWSER_USE_PROXY_COUNTRY:
                    session_kwargs["proxy_country_code"] = BROWSER_USE_PROXY_COUNTRY.lower()
                if BROWSER_USE_PROFILE_ID:
                    session_kwargs["profile_id"] = BROWSER_USE_PROFILE_ID
                return await bu_client.browsers.create_browser_session(**session_kwargs)

            try:
                browser_session = await retry_async(
                    create_session,
                    max_retries=config.max_retries,
                    delay=config.retry_delay,
                    on_retry=lambda a, m, e, w: print(f"[RETRY] {company}: Browser session attempt {a}/{m}, waiting {w:.1f}s..."),
                )
            except Exception as e:
                result.status = ScrapeStatus.ERROR
                result.error = f"Failed to create browser session: {e}"
                print(f"[ERROR] {company}: {result.error}")
                return result

            cdp_url = browser_session.cdp_url
            print(f"[CLOUD] {company}: CDP URL: {cdp_url}")
            print(f"[LIVE] {company}: {browser_session.live_url}")

            # Step 2: Connect Playwright to cloud browser
            async with async_playwright() as p:
                print(f"[PLAYWRIGHT] {company}: Connecting to cloud browser...")
                playwright_browser = await p.chromium.connect_over_cdp(cdp_url)

                # Get the default context and page
                contexts = playwright_browser.contexts
                if contexts:
                    context = contexts[0]
                    pages = context.pages
                    page = pages[0] if pages else await context.new_page()
                else:
                    context = await playwright_browser.new_context()
                    page = await context.new_page()

                # Step 3: Navigate to DocSend URL with retry
                print(f"[NAV] {company}: Navigating to DocSend...")

                async def navigate():
                    await page.goto(url, wait_until='networkidle', timeout=config.page_load_timeout)

                try:
                    await retry_async(
                        navigate,
                        max_retries=config.max_retries,
                        delay=config.retry_delay,
                        exceptions=(PlaywrightTimeout, Exception),
                        on_retry=lambda a, m, e, w: print(f"[RETRY] {company}: Navigation attempt {a}/{m}, waiting {w:.1f}s..."),
                    )
                except PlaywrightTimeout:
                    result.status = ScrapeStatus.TIMEOUT
                    result.error = "Page load timeout"
                    print(f"[ERROR] {company}: Page load timeout after {config.max_retries} attempts")
                    return result
                except Exception as e:
                    result.status = ScrapeStatus.ERROR
                    result.error = f"Navigation failed: {e}"
                    print(f"[ERROR] {company}: {result.error}")
                    return result

                # Check for error states
                title = await page.title()
                if '404' in title or 'not found' in title.lower():
                    result.status = ScrapeStatus.LINK_EXPIRED
                    print(f"[SKIP] {company}: Link expired/not found")
                    return result

                # Check for password field
                password_field = await page.query_selector('input[type="password"]')
                if password_field:
                    if password:
                        print(f"[AUTH] {company}: Entering password...")
                        success = await enter_password(page, password, config)
                        if not success:
                            result.status = ScrapeStatus.PASSWORD_REQUIRED
                            result.error = "Password rejected or entry failed"
                            print(f"[SKIP] {company}: Password rejected")
                            return result
                        print(f"[AUTH] {company}: Password accepted")
                    else:
                        result.status = ScrapeStatus.PASSWORD_REQUIRED
                        print(f"[SKIP] {company}: Password required")
                        return result

                # Dismiss cookie consent if present
                try:
                    accept_btn = page.locator('button:has-text("Accept All")').first
                    if await accept_btn.is_visible(timeout=2000):
                        await accept_btn.click()
                        await asyncio.sleep(1)
                except:
                    pass

                # Check for email prompt - always try to auth if present
                # (some documents show preview but need auth for API access)
                email_field = None
                try:
                    email_field = page.locator('#prompt input[type="email"]').first
                    await email_field.wait_for(state='visible', timeout=config.auth_timeout)
                    print(f"[AUTH] {company}: Email field found")
                except:
                    # Try fallback - find visible email input (not the feedback one)
                    try:
                        # Look for email input in a modal/prompt container
                        modal_email = page.locator('.modal input[type="email"], #prompt input[type="email"], [class*="auth"] input[type="email"]').first
                        if await modal_email.count() > 0:
                            box = await modal_email.bounding_box(timeout=2000)
                            if box and box['width'] > 50:
                                email_field = modal_email
                    except:
                        pass

                if email_field:
                    try:
                        print(f"[AUTH] {company}: Entering email...")
                        await email_field.click(timeout=5000)
                        await email_field.fill(email, timeout=5000)

                        # Click continue button
                        submit_btn = page.locator('button:has-text("Continue")').first
                        try:
                            if await submit_btn.is_visible(timeout=2000):
                                await submit_btn.click(timeout=5000)
                            else:
                                await page.keyboard.press('Enter')
                        except:
                            await page.keyboard.press('Enter')

                        # Wait for document to load
                        await page.wait_for_load_state('networkidle', timeout=config.network_idle_timeout)
                        await asyncio.sleep(2)
                    except PlaywrightTimeout as e:
                        print(f"[WARN] {company}: Auth interaction timeout, continuing anyway...")
                    except Exception as e:
                        print(f"[WARN] {company}: Auth error: {e}, continuing anyway...")
                else:
                    # No email field - check if document is accessible
                    try:
                        indicator = page.locator('.toolbar-page-indicator').first
                        if await indicator.is_visible(timeout=3000):
                            print(f"[AUTH] {company}: Document accessible without email")
                    except:
                        pass

                # Get slide count with retry
                total_pages = 0
                for attempt in range(config.max_retries):
                    total_pages = await get_slide_count(page)
                    if total_pages > 0:
                        break
                    await asyncio.sleep(2)

                if total_pages == 0:
                    result.status = ScrapeStatus.NO_SLIDES
                    result.error = "Could not determine slide count"
                    print(f"[ERROR] {company}: No slides found")
                    return result

                result.total_pages = total_pages
                print(f"[FOUND] {company}: {total_pages} slides")

                # Fetch all slide URLs via API
                print(f"[FETCH] {company}: Getting slide URLs...")
                slide_urls, fetch_failed = await fetch_slide_urls(page, total_pages, company, config)
                valid_urls = [u for u in slide_urls if u]
                print(f"[FETCH] {company}: Got {len(valid_urls)}/{total_pages} URLs")

                if len(valid_urls) == 0:
                    result.status = ScrapeStatus.ERROR
                    result.error = "Failed to fetch any slide URLs"
                    result.failed_slides = fetch_failed
                    print(f"[ERROR] {company}: No slide URLs retrieved")
                    return result

                # Download images
                print(f"[DOWNLOAD] {company}: Downloading images...")
                downloaded, download_failed = await download_images(slide_urls, company_dir, company, config)
                result.downloaded = len(downloaded)
                result.failed_slides = list(set(fetch_failed + download_failed))
                print(f"[DOWNLOAD] {company}: {len(downloaded)}/{total_pages} slides")

                # Create PDF even with partial results
                if downloaded:
                    pdf_path = company_dir / f"{company.replace(' ', '_')}.pdf"
                    await create_pdf(downloaded, pdf_path)
                    result.pdf_path = str(pdf_path)
                    print(f"[PDF] {company}: {pdf_path}")

                # Determine final status
                if len(downloaded) == total_pages:
                    result.status = ScrapeStatus.SUCCESS
                elif len(downloaded) >= total_pages * config.min_success_ratio:
                    result.status = ScrapeStatus.PARTIAL
                    result.error = f"Got {len(downloaded)}/{total_pages} slides"
                    print(f"[WARN] {company}: Partial success - {len(downloaded)}/{total_pages} slides")
                elif len(downloaded) > 0:
                    result.status = ScrapeStatus.PARTIAL
                    result.error = f"Only got {len(downloaded)}/{total_pages} slides"
                    print(f"[WARN] {company}: Low success rate - {len(downloaded)}/{total_pages} slides")
                else:
                    result.status = ScrapeStatus.ERROR
                    result.error = "All downloads failed"

        except PlaywrightTimeout as e:
            result.status = ScrapeStatus.TIMEOUT
            result.error = str(e)
            print(f"[ERROR] {company}: Timeout - {e}")

        except Exception as e:
            result.status = ScrapeStatus.ERROR
            result.error = str(e)
            print(f"[ERROR] {company}: {e}")

        finally:
            # Clean up
            if playwright_browser:
                try:
                    await playwright_browser.close()
                except:
                    pass

            if browser_session:
                try:
                    await bu_client.browsers.update_browser_session(
                        session_id=browser_session.id,
                        action="stop"
                    )
                    print(f"[CLEANUP] {company}: Browser session stopped")
                except Exception as e:
                    print(f"[WARN] {company}: Failed to stop session: {e}")

        return result


async def main():
    """Main entry point."""

    # URLs to scrape - modify this list or load from external source
    # Format: {"url": "...", "company": "...", "password": "..." (optional)}
    docsend_urls = [
        # Add your DocSend URLs here
        # {"url": "https://docsend.com/view/abc123", "company": "Example Company"},
        # {"url": "https://docsend.com/view/xyz789", "company": "Protected Co", "password": "secret"},
    ]

    if not docsend_urls:
        print("No URLs configured. Add URLs to the docsend_urls list in main().")
        print("Example: {'url': 'https://docsend.com/view/abc123', 'company': 'Example'}")
        print("With password: {'url': '...', 'company': '...', 'password': 'secret'}")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("DocSend Scraper (browser-use CLOUD + Playwright)")
    print("=" * 60)
    print(f"URLs to scrape: {len(docsend_urls)}")
    print(f"Max parallel: {MAX_PARALLEL}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"API Key: {API_KEY[:20]}..." if API_KEY else "API Key: NOT SET!")
    print("-" * 60)

    if not API_KEY:
        print("ERROR: BROWSER_USE_API_KEY not set!")
        return

    semaphore = asyncio.Semaphore(MAX_PARALLEL)

    tasks = [
        scrape_docsend(
            u["url"],
            u["company"],
            OUTPUT_DIR,
            EMAIL,
            semaphore,
            password=u.get("password"),
        )
        for u in docsend_urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    success = [r for r in results if isinstance(r, ScrapeResult) and r.status == ScrapeStatus.SUCCESS]
    partial = [r for r in results if isinstance(r, ScrapeResult) and r.status == ScrapeStatus.PARTIAL]
    skipped = [r for r in results if isinstance(r, ScrapeResult) and r.status in (ScrapeStatus.PASSWORD_REQUIRED, ScrapeStatus.LINK_EXPIRED)]
    failed = [r for r in results if isinstance(r, ScrapeResult) and r.status in (ScrapeStatus.ERROR, ScrapeStatus.TIMEOUT, ScrapeStatus.NO_SLIDES, ScrapeStatus.AUTH_FAILED)]

    print(f"✓ Success: {len(success)}")
    print(f"◐ Partial: {len(partial)}")
    print(f"⊘ Skipped: {len(skipped)}")
    print(f"✗ Failed: {len(failed)}")

    for r in results:
        if isinstance(r, ScrapeResult):
            if r.status == ScrapeStatus.SUCCESS:
                icon = "✓"
            elif r.status == ScrapeStatus.PARTIAL:
                icon = "◐"
            elif r.status in (ScrapeStatus.PASSWORD_REQUIRED, ScrapeStatus.LINK_EXPIRED):
                icon = "⊘"
            else:
                icon = "✗"

            print(f"  {icon} {r.company}: {r.status.value} ({r.downloaded}/{r.total_pages} slides)")
            if r.error:
                print(f"      Error: {r.error}")
            if r.failed_slides:
                print(f"      Failed slides: {r.failed_slides}")
        else:
            print(f"  ✗ Exception: {r}")


if __name__ == "__main__":
    asyncio.run(main())
