#!/usr/bin/env python3
"""
DocSend Router - Intelligent routing for DocSend URLs.

This module provides:
- State detection for DocSend URLs (expired, password required, download enabled, etc.)
- Routing logic to handle each state appropriately
- Orchestration for processing multiple URLs

Usage:
    from parchiver_docsend import route_all_docsends, DocSendState

    urls = [
        {"url": "https://docsend.com/view/abc", "company": "Acme", "password": "secret"},
    ]
    results = await route_all_docsends(urls, output_dir=Path("./output"))
"""

import asyncio
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

load_dotenv()

from browser_use_sdk import AsyncBrowserUse

# Import slide scraper functions
from ..docsend.playwright import (
    ScrapeConfig,
    ScrapeResult,
    ScrapeStatus,
    DEFAULT_CONFIG,
    DEFAULT_EMAIL,
    get_slide_count,
    fetch_slide_urls,
    download_images,
    create_pdf,
    retry_async,
    enter_password,
)

# Configuration
API_KEY = os.environ.get("PARCHIVER_BROWSER_USE_API_KEY")
BROWSER_USE_PROXY_COUNTRY = os.environ.get("PARCHIVER_BROWSER_USE_PROXY_COUNTRY")
BROWSER_USE_PROFILE_ID = os.environ.get("PARCHIVER_BROWSER_USE_PROFILE_ID")
DOCSEND_DEBUG_DIR = os.environ.get("PARCHIVER_DOCSEND_DEBUG_DIR")
MAX_PARALLEL = 5


class DocSendState(Enum):
    """Possible states of a DocSend link."""
    LINK_EXPIRED = "link_expired"           # 404 or "not found"
    PASSWORD_REQUIRED = "password_required"  # Password field visible
    EMAIL_GATED = "email_gated"             # Email required to view
    DOWNLOAD_ENABLED = "download_enabled"    # Download button available
    DOWNLOAD_DISABLED = "download_disabled"  # View-only, need to scrape slides
    FOLDER = "folder"                        # Folder/space with ZIP download
    BLOCKED = "blocked"                      # CloudFront or WAF block
    UNKNOWN = "unknown"                      # Couldn't determine


def is_folder_url(url: str) -> bool:
    """Check if a DocSend URL is a folder/space link (contains /s/ segment)."""
    path = urlparse(url).path
    # Folder URLs: /view/s/xxx or /v/xxx/s/yyy
    return '/s/' in path


@dataclass
class StateDetectionResult:
    """Result of state detection."""
    state: DocSendState
    title: str | None = None
    page_count: int | None = None
    error: str | None = None


async def enter_email(page: Page, email: str, config: ScrapeConfig = DEFAULT_CONFIG) -> bool:
    """Enter email on an email-gated DocSend page.

    Returns:
        True if email was accepted, False otherwise.
    """
    try:
        # Try multiple selectors for email field
        email_selectors = [
            '#prompt input[type="email"]',
            '.ReactModal__Content input[type="email"]',
            '#email[type="email"]',
            '.modal input[type="email"]',
            '[class*="auth"] input[type="email"]',
            'input[type="email"]',
        ]

        email_field = None
        for selector in email_selectors:
            try:
                field = page.locator(selector).first
                if await field.count() > 0:
                    box = await field.bounding_box(timeout=2000)
                    if box and box['width'] > 50:
                        email_field = field
                        break
            except:
                continue

        if not email_field:
            return False

        await email_field.click(timeout=5000)
        await email_field.fill(email, timeout=5000)

        # Try to click continue/confirm button
        submit_selectors = [
            'button:has-text("Continue")',
            'button:has-text("Confirm")',
            'button[type="submit"]',
        ]
        clicked = False
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=5000)
                    clicked = True
                    break
            except:
                continue
        if not clicked:
            await page.keyboard.press('Enter')

        # Wait for document to load
        await page.wait_for_load_state('networkidle', timeout=config.network_idle_timeout)
        await asyncio.sleep(2)

        return True

    except PlaywrightTimeout:
        return False
    except Exception as e:
        print(f"[WARN] Email entry error: {e}")
        return False


async def detect_docsend_state(
    page: Page,
    url: str,
    password: str | None = None,
    http_status: int | None = None,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> StateDetectionResult:
    """Detect the current state of a DocSend page.

    Args:
        page: Playwright page already navigated to the DocSend URL
        url: The DocSend URL (for logging)
        password: Optional password to try if page is password-protected
        config: Scraping configuration

    Returns:
        StateDetectionResult with detected state and metadata
    """
    result = StateDetectionResult(state=DocSendState.UNKNOWN)

    try:
        # Detect CloudFront/WAF blocks before treating as expired
        title = await page.title()
        result.title = title

        if http_status in (403, 429, 503):
            result.state = DocSendState.BLOCKED
            result.error = f"http_{http_status}"
            return result

        if "request could not be satisfied" in title.lower():
            result.state = DocSendState.BLOCKED
            result.error = "cloudfront_blocked"
            return result

        try:
            blocked_text = await page.locator(
                'text=/request could not be satisfied|cloudfront|access denied|request blocked/i'
            ).first.text_content(timeout=2000)
            if blocked_text:
                result.state = DocSendState.BLOCKED
                result.error = "cloudfront_blocked"
                return result
        except:
            pass

        if '404' in title or 'not found' in title.lower() or 'expired' in title.lower():
            result.state = DocSendState.LINK_EXPIRED
            return result

        # Check for password/passcode field BEFORE body-text expired checks,
        # because email/password-gated pages can show "no longer available"
        # as placeholder text before auth.
        # DocSend uses type="text" for passcode fields, not type="password".
        password_field = await page.query_selector(
            'input[type="password"], #link_auth_form_passcode, input[name*="passcode"]'
        )
        if not password_field:
            # Fallback: try selectors individually (some CDP connections
            # have issues with comma-separated selectors)
            for pw_sel in ['input[type="password"]', '#link_auth_form_passcode', 'input[name*="passcode"]']:
                password_field = await page.query_selector(pw_sel)
                if password_field:
                    break
        if password_field:
            if password:
                # Try the password (enter_password also fills email if present)
                success = await enter_password(page, password, config)
                if not success:
                    result.state = DocSendState.PASSWORD_REQUIRED
                    result.error = "Password rejected or entry failed"
                    return result
                # Password worked — skip email/expired checks, go straight
                # to download button and slide count detection below.
            else:
                result.state = DocSendState.PASSWORD_REQUIRED
                return result
        else:
            # No password field — check email gate and expired text
            has_email_field = False
            try:
                email_locator = page.locator('#prompt input[type="email"]').first
                await email_locator.wait_for(state='visible', timeout=config.auth_timeout)
                has_email_field = True
            except:
                # Try fallback selectors (including folder-style ReactModal email)
                fallback_selectors = [
                    '.modal input[type="email"], [class*="auth"] input[type="email"]',
                    '.ReactModal__Content input[type="email"], #email[type="email"]',
                ]
                for fb_sel in fallback_selectors:
                    if has_email_field:
                        break
                    try:
                        modal_email = page.locator(fb_sel).first
                        if await modal_email.count() > 0:
                            box = await modal_email.bounding_box(timeout=2000)
                            if box and box['width'] > 50:
                                has_email_field = True
                    except:
                        pass

            if has_email_field:
                result.state = DocSendState.EMAIL_GATED
                return result

            # Check page content for expired messages (only when no auth
            # fields found — authenticated pages still have "no longer
            # available" text hidden in the DOM).  Only count it as
            # expired if the element is actually *visible* on screen.
            try:
                expired_locator = page.locator('text=/link.*expired|no longer available|not available|not found/i').first
                if await expired_locator.is_visible(timeout=2000):
                    expired_text = await expired_locator.text_content(timeout=2000)
                    if expired_text:
                        result.state = DocSendState.LINK_EXPIRED
                        return result
            except:
                pass

        # Check for folder/space (Download as ZIP or per-file Download buttons)
        try:
            zip_btn = page.locator('button[aria-label="Download as ZIP"]').first
            if await zip_btn.is_visible(timeout=2000):
                result.state = DocSendState.FOLDER
                return result
        except:
            pass
        try:
            file_dl_btn = page.locator('button[aria-label="Download file"]').first
            if await file_dl_btn.is_visible(timeout=2000):
                result.state = DocSendState.FOLDER
                return result
        except:
            pass

        # Check for download button
        download_selectors = [
            '[data-testid="download"]',
            '.download-button',
            'button:has-text("Download")',
            'a:has-text("Download PDF")',
            '[class*="download"]',
        ]

        for selector in download_selectors:
            try:
                download_btn = page.locator(selector).first
                if await download_btn.is_visible(timeout=2000):
                    result.state = DocSendState.DOWNLOAD_ENABLED
                    break
            except:
                continue

        # If no download button found, check if we can access slides
        if result.state == DocSendState.UNKNOWN:
            # Check for page indicator (indicates viewable content)
            page_count = await get_slide_count(page)
            if page_count > 0:
                result.page_count = page_count
                result.state = DocSendState.DOWNLOAD_DISABLED
            else:
                # Try waiting a bit more for content to load
                await asyncio.sleep(2)
                page_count = await get_slide_count(page)
                if page_count > 0:
                    result.page_count = page_count
                    result.state = DocSendState.DOWNLOAD_DISABLED

    except Exception as e:
        result.error = str(e)

    return result


async def download_docsend_pdf(
    page: Page,
    output_dir: Path,
    filename: str,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> Path | None:
    """Download PDF directly when download is enabled.

    Args:
        page: Playwright page on the DocSend document
        output_dir: Directory to save the PDF
        filename: Filename for the PDF (without extension)
        config: Scraping configuration

    Returns:
        Path to downloaded PDF or None if download failed
    """
    download_selectors = [
        '[data-testid="download"]',
        '.download-button',
        'button:has-text("Download")',
        'a:has-text("Download PDF")',
        'a:has-text("Download")',
    ]

    for selector in download_selectors:
        try:
            download_btn = page.locator(selector).first
            if await download_btn.is_visible(timeout=2000):
                async with page.expect_download(timeout=60000) as download_info:
                    await download_btn.click()

                download = await download_info.value
                save_path = output_dir / f"{filename}.pdf"
                await download.save_as(save_path)
                return save_path
        except PlaywrightTimeout:
            continue
        except Exception as e:
            print(f"[WARN] Download attempt with {selector} failed: {e}")
            continue

    return None


async def _intercept_cloudfront_download(
    page: Page,
    click_locator,
    timeout_seconds: int = 180,
) -> tuple[str | None, str | None]:
    """Click a button and intercept the resulting CloudFront download URL.

    Returns:
        Tuple of (download_url, suggested_filename) or (None, None).
    """
    download_url = None
    suggested_filename = None

    def on_response(response):
        nonlocal download_url, suggested_filename
        # Skip Dropbox analytics noise
        if 'dropbox.com/log' in response.url:
            return
        content_disp = response.headers.get('content-disposition', '')
        if 'cloudfront.net' in response.url and 'attachment' in content_disp:
            download_url = response.url
            # Extract filename from content-disposition
            import re
            fn_match = re.search(r'filename="?([^";\n]+)"?', content_disp)
            if fn_match:
                suggested_filename = fn_match.group(1)

    page.on('response', on_response)

    await click_locator.click()

    for _ in range(timeout_seconds // 3):
        if download_url:
            break
        await asyncio.sleep(3)

    page.remove_listener('response', on_response)
    return download_url, suggested_filename


async def download_folder(
    page: Page,
    output_dir: Path,
    filename: str,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> Path | None:
    """Download a DocSend folder/space.

    Handles two folder variants:
    1. ZIP download: single "Download as ZIP" button generates a server-side
       ZIP archive and delivers it via CloudFront.
    2. Per-file download: individual "Download file" buttons on each document
       row, each delivering a file via CloudFront.

    Args:
        page: Playwright page on the DocSend folder (post-auth)
        output_dir: Directory to save the output
        filename: Base filename (without extension)
        config: Scraping configuration

    Returns:
        Path to downloaded ZIP/directory or None if download failed
    """
    import httpx

    # --- Variant 1: Download as ZIP ---
    try:
        zip_btn = page.locator('button[aria-label="Download as ZIP"]').first
        if await zip_btn.is_visible(timeout=3000):
            print(f"[ZIP] Waiting for ZIP generation...")
            url, fname = await _intercept_cloudfront_download(page, zip_btn)
            if not url:
                print(f"[WARN] ZIP download URL not intercepted after timeout")
                return None

            save_path = output_dir / f"{filename}.zip"
            print(f"[ZIP] Downloading ZIP from CloudFront...")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                    async with client.stream('GET', url) as resp:
                        resp.raise_for_status()
                        with open(save_path, 'wb') as f:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                f.write(chunk)
                size = save_path.stat().st_size
                print(f"[ZIP] Saved: {save_path} ({size:,} bytes / {size/1024/1024:.1f} MB)")
                return save_path
            except Exception as e:
                print(f"[WARN] ZIP download failed: {e}")
                if save_path.exists():
                    save_path.unlink()
                return None
    except:
        pass

    # --- Variant 2: Per-file downloads ---
    dl_buttons = page.locator('button[aria-label="Download file"]')
    count = await dl_buttons.count()
    if count == 0:
        print(f"[WARN] No download buttons found in folder")
        return None

    print(f"[FOLDER] Downloading {count} files individually...")
    downloaded = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        for i in range(count):
            btn = dl_buttons.nth(i)
            try:
                url, fname = await _intercept_cloudfront_download(page, btn, timeout_seconds=60)
                if not url:
                    print(f"  [WARN] File {i+1}/{count}: no download URL intercepted")
                    continue

                safe_fname = fname or f"file_{i+1}"
                # Sanitize filename
                safe_fname = safe_fname.replace('/', '_').replace('\\', '_')
                save_path = output_dir / safe_fname

                async with client.stream('GET', url) as resp:
                    resp.raise_for_status()
                    with open(save_path, 'wb') as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)

                size = save_path.stat().st_size
                print(f"  [{i+1}/{count}] {safe_fname} ({size:,} bytes)")
                downloaded.append(save_path)
            except Exception as e:
                print(f"  [WARN] File {i+1}/{count}: download failed: {e}")

            # Brief pause between downloads
            await asyncio.sleep(1)

    if not downloaded:
        return None

    print(f"[FOLDER] Downloaded {len(downloaded)}/{count} files")
    # Return the output directory itself as the "path"
    return output_dir


async def scrape_slides(
    page: Page,
    company: str,
    output_dir: Path,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> ScrapeResult:
    """Scrape slides when download is disabled (view-only mode).

    This function assumes the page is already authenticated and showing the document.

    Args:
        page: Playwright page on the DocSend document
        company: Company name for the output
        output_dir: Directory to save output
        config: Scraping configuration

    Returns:
        ScrapeResult with status and details
    """
    result = ScrapeResult(url=page.url, company=company)

    # Get slide count
    total_pages = 0
    for attempt in range(config.max_retries):
        total_pages = await get_slide_count(page)
        if total_pages > 0:
            break
        await asyncio.sleep(2)

    if total_pages == 0:
        result.status = ScrapeStatus.NO_SLIDES
        result.error = "Could not determine slide count"
        return result

    result.total_pages = total_pages
    print(f"[FOUND] {company}: {total_pages} slides")

    # Create output directory
    company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
    company_dir.mkdir(parents=True, exist_ok=True)

    # Fetch all slide URLs via API
    print(f"[FETCH] {company}: Getting slide URLs...")
    slide_urls, fetch_failed = await fetch_slide_urls(page, total_pages, company, config)
    valid_urls = [u for u in slide_urls if u]
    print(f"[FETCH] {company}: Got {len(valid_urls)}/{total_pages} URLs")

    if len(valid_urls) == 0:
        result.status = ScrapeStatus.ERROR
        result.error = "Failed to fetch any slide URLs"
        result.failed_slides = fetch_failed
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
    elif len(downloaded) > 0:
        result.status = ScrapeStatus.PARTIAL
        result.error = f"Only got {len(downloaded)}/{total_pages} slides"
    else:
        result.status = ScrapeStatus.ERROR
        result.error = "All downloads failed"

    return result


async def route_docsend(
    url: str,
    company: str,
    output_dir: Path,
    password: str | None = None,
    email: str = DEFAULT_EMAIL,
    semaphore: asyncio.Semaphore | None = None,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> ScrapeResult:
    """Route a single DocSend URL to the appropriate handler.

    This function:
    1. Creates a cloud browser session
    2. Navigates to the URL
    3. Detects the page state
    4. Routes to the appropriate handler based on state

    Args:
        url: DocSend URL to process
        company: Company name for output organization
        output_dir: Directory to save output files
        password: Optional password (pre-extracted from email)
        email: Email to use for email-gated documents
        semaphore: Optional semaphore for concurrency control
        config: Scraping configuration

    Returns:
        ScrapeResult with status and details
    """
    async def _route():
        print(f"\n[ROUTER] {company}: {url}")
        result = ScrapeResult(url=url, company=company)

        bu_client = AsyncBrowserUse(api_key=API_KEY)
        browser_session = None
        playwright_browser = None

        try:
            # Create cloud browser session
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
                    on_retry=lambda a, m, e, w: print(f"[RETRY] {company}: Browser session attempt {a}/{m}..."),
                )
            except Exception as e:
                result.status = ScrapeStatus.ERROR
                result.error = f"Failed to create browser session: {e}"
                return result

            cdp_url = browser_session.cdp_url
            print(f"[LIVE] {company}: {browser_session.live_url}")

            async with async_playwright() as p:
                playwright_browser = await p.chromium.connect_over_cdp(cdp_url)

                contexts = playwright_browser.contexts
                if contexts:
                    context = contexts[0]
                    pages = context.pages
                    page = pages[0] if pages else await context.new_page()
                else:
                    context = await playwright_browser.new_context()
                    page = await context.new_page()

                # Navigate to URL
                print(f"[NAV] {company}: Navigating to DocSend...")
                await asyncio.sleep(15)
                try:
                    response = await page.goto(url, wait_until='networkidle', timeout=config.page_load_timeout)
                except PlaywrightTimeout:
                    result.status = ScrapeStatus.TIMEOUT
                    result.error = "Page load timeout"
                    return result

                # Dismiss cookie consent if present
                try:
                    accept_btn = page.locator('button:has-text("Accept All")').first
                    if await accept_btn.is_visible(timeout=2000):
                        await accept_btn.click()
                        await asyncio.sleep(1)
                except:
                    pass

                if DOCSEND_DEBUG_DIR:
                    debug_dir = Path(DOCSEND_DEBUG_DIR)
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_prefix = f"{company.replace(' ', '_').replace('/', '_') or 'unknown'}"
                    try:
                        await page.screenshot(path=str(debug_dir / f"{debug_prefix}.png"), full_page=True)
                    except Exception as exc:
                        print(f"[WARN] {company}: Debug screenshot failed: {exc}")
                    try:
                        html_path = debug_dir / f"{debug_prefix}.html"
                        html_path.write_text(await page.content())
                    except Exception as exc:
                        print(f"[WARN] {company}: Debug HTML dump failed: {exc}")

                # Detect state
                print(f"[TRIAGE] {company}: Detecting state...")
                state_result = await detect_docsend_state(
                    page,
                    url,
                    password,
                    response.status if response else None,
                    config,
                )
                print(f"[STATE] {company}: {state_result.state.value}")

                # Route based on state
                match state_result.state:
                    case DocSendState.LINK_EXPIRED:
                        result.status = ScrapeStatus.LINK_EXPIRED
                        print(f"[SKIP] {company}: Link expired")
                        return result

                    case DocSendState.BLOCKED:
                        result.status = ScrapeStatus.ERROR
                        result.error = state_result.error or "Blocked by CloudFront/WAF"
                        print(f"[SKIP] {company}: Blocked by CloudFront/WAF")
                        return result

                    case DocSendState.PASSWORD_REQUIRED:
                        result.status = ScrapeStatus.PASSWORD_REQUIRED
                        result.error = state_result.error or "Password required but not provided or rejected"
                        print(f"[SKIP] {company}: Password required")
                        return result

                    case DocSendState.EMAIL_GATED:
                        # Enter email and re-triage
                        print(f"[AUTH] {company}: Entering email...")
                        success = await enter_email(page, email, config)
                        if not success:
                            # Email entry failed — check if the page is actually
                            # expired. Expired pages can show an email field too,
                            # which is indistinguishable from a real gate before
                            # attempting entry.
                            try:
                                expired_el = page.locator(
                                    'text=/link.*expired|no longer available|not found/i'
                                ).first
                                expired_text = await expired_el.text_content(timeout=2000)
                                if expired_text:
                                    result.status = ScrapeStatus.LINK_EXPIRED
                                    print(f"[SKIP] {company}: Link expired (detected after auth failure)")
                                    return result
                            except:
                                pass
                            result.status = ScrapeStatus.AUTH_FAILED
                            result.error = "Failed to enter email"
                            return result

                        # Re-detect state after email entry
                        state_result = await detect_docsend_state(page, url, password, None, config)
                        print(f"[STATE] {company}: After email: {state_result.state.value}")

                        if state_result.state == DocSendState.FOLDER:
                            # Folder — download as ZIP
                            print(f"[FOLDER] {company}: Downloading folder as ZIP...")
                            company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
                            company_dir.mkdir(parents=True, exist_ok=True)
                            zip_path = await download_folder(page, company_dir, company.replace(" ", "_"), config)
                            if zip_path:
                                result.status = ScrapeStatus.SUCCESS
                                result.pdf_path = str(zip_path)
                                print(f"[ZIP] {company}: Downloaded to {zip_path}")
                            else:
                                result.status = ScrapeStatus.ERROR
                                result.error = "Folder ZIP download failed"
                            return result
                        elif state_result.state == DocSendState.DOWNLOAD_ENABLED:
                            # Download directly
                            company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
                            company_dir.mkdir(parents=True, exist_ok=True)
                            pdf_path = await download_docsend_pdf(page, company_dir, company.replace(" ", "_"), config)
                            if pdf_path:
                                result.status = ScrapeStatus.SUCCESS
                                result.pdf_path = str(pdf_path)
                                print(f"[PDF] {company}: Downloaded to {pdf_path}")
                            else:
                                # Fall back to scraping
                                return await scrape_slides(page, company, output_dir, config)
                        else:
                            # Scrape slides
                            return await scrape_slides(page, company, output_dir, config)

                    case DocSendState.FOLDER:
                        # Folder already authenticated — download as ZIP
                        print(f"[FOLDER] {company}: Downloading folder as ZIP...")
                        company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
                        company_dir.mkdir(parents=True, exist_ok=True)
                        zip_path = await download_folder(page, company_dir, company.replace(" ", "_"), config)
                        if zip_path:
                            result.status = ScrapeStatus.SUCCESS
                            result.pdf_path = str(zip_path)
                            print(f"[ZIP] {company}: Downloaded to {zip_path}")
                        else:
                            result.status = ScrapeStatus.ERROR
                            result.error = "Folder ZIP download failed"
                        return result

                    case DocSendState.DOWNLOAD_ENABLED:
                        # Download directly
                        print(f"[DOWNLOAD] {company}: Download enabled, downloading PDF...")
                        company_dir = output_dir / company.replace(" ", "_").replace("/", "_")
                        company_dir.mkdir(parents=True, exist_ok=True)
                        pdf_path = await download_docsend_pdf(page, company_dir, company.replace(" ", "_"), config)
                        if pdf_path:
                            result.status = ScrapeStatus.SUCCESS
                            result.pdf_path = str(pdf_path)
                            print(f"[PDF] {company}: Downloaded to {pdf_path}")
                            return result
                        else:
                            # Fall back to slide scraping
                            print(f"[WARN] {company}: Download failed, falling back to slide scrape...")
                            return await scrape_slides(page, company, output_dir, config)

                    case DocSendState.DOWNLOAD_DISABLED:
                        # Scrape slides
                        print(f"[SCRAPE] {company}: Download disabled, scraping slides...")
                        return await scrape_slides(page, company, output_dir, config)

                    case DocSendState.UNKNOWN:
                        # Try scraping anyway
                        print(f"[WARN] {company}: Unknown state, attempting slide scrape...")
                        return await scrape_slides(page, company, output_dir, config)

                return result

        except Exception as e:
            result.status = ScrapeStatus.ERROR
            result.error = str(e)
            print(f"[ERROR] {company}: {e}")
            return result

        finally:
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
                except:
                    pass

    if semaphore:
        async with semaphore:
            return await _route()
    else:
        return await _route()


async def route_all_docsends(
    urls: list[dict],
    output_dir: Path,
    email: str = DEFAULT_EMAIL,
    max_parallel: int = MAX_PARALLEL,
    config: ScrapeConfig = DEFAULT_CONFIG,
) -> list[ScrapeResult]:
    """Route multiple DocSend URLs to appropriate handlers.

    Args:
        urls: List of dicts with 'url', 'company', and optional 'password' keys
        output_dir: Directory to save output files
        email: Email to use for email-gated documents
        max_parallel: Maximum number of parallel browser sessions
        config: Scraping configuration

    Returns:
        List of ScrapeResult objects
    """
    if not urls:
        print("No URLs to process.")
        return []

    if not API_KEY:
        print("ERROR: BROWSER_USE_API_KEY not set!")
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("DocSend Router")
    print("=" * 60)
    print(f"URLs to process: {len(urls)}")
    print(f"Max parallel: {max_parallel}")
    print(f"Output: {output_dir}")
    print("-" * 60)

    semaphore = asyncio.Semaphore(max_parallel)

    tasks = [
        route_docsend(
            url=u["url"],
            company=u.get("company", "Unknown"),
            output_dir=output_dir,
            password=u.get("password"),
            email=email,
            semaphore=semaphore,
            config=config,
        )
        for u in urls
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to ScrapeResults
    processed_results = []
    for i, r in enumerate(results):
        if isinstance(r, ScrapeResult):
            processed_results.append(r)
        else:
            # Exception occurred
            processed_results.append(ScrapeResult(
                url=urls[i]["url"],
                company=urls[i].get("company", "Unknown"),
                status=ScrapeStatus.ERROR,
                error=str(r),
            ))

    # Print summary
    print("\n" + "=" * 60)
    print("ROUTING SUMMARY")
    print("=" * 60)

    success = [r for r in processed_results if r.status == ScrapeStatus.SUCCESS]
    partial = [r for r in processed_results if r.status == ScrapeStatus.PARTIAL]
    expired = [r for r in processed_results if r.status == ScrapeStatus.LINK_EXPIRED]
    password = [r for r in processed_results if r.status == ScrapeStatus.PASSWORD_REQUIRED]
    failed = [r for r in processed_results if r.status in (ScrapeStatus.ERROR, ScrapeStatus.TIMEOUT, ScrapeStatus.NO_SLIDES, ScrapeStatus.AUTH_FAILED)]

    print(f"SUCCESS: {len(success)}")
    print(f"PARTIAL: {len(partial)}")
    print(f"EXPIRED: {len(expired)}")
    print(f"PASSWORD_REQUIRED: {len(password)}")
    print(f"FAILED: {len(failed)}")

    for r in processed_results:
        status_icon = {
            ScrapeStatus.SUCCESS: "+",
            ScrapeStatus.PARTIAL: "~",
            ScrapeStatus.LINK_EXPIRED: "-",
            ScrapeStatus.PASSWORD_REQUIRED: "!",
        }.get(r.status, "x")

        slides_info = f" ({r.downloaded}/{r.total_pages})" if r.total_pages else ""
        print(f"  [{status_icon}] {r.company}: {r.status.value}{slides_info}")
        if r.error:
            print(f"      Error: {r.error}")

    return processed_results


async def main():
    """Example usage - process URLs from command line or hardcoded list."""
    import json
    import sys

    # Example: Load from results.json
    if len(sys.argv) > 1:
        results_file = Path(sys.argv[1])
        if results_file.exists():
            with open(results_file) as f:
                data = json.load(f)

            # Extract DocSend links
            urls = []
            for email_data in data:
                for link in email_data.get("links", []):
                    if link.get("type") == "docsend":
                        urls.append({
                            "url": link["url"],
                            "company": email_data.get("subject", "Unknown")[:50],
                            "password": link.get("password"),
                        })

            if urls:
                results = await route_all_docsends(urls, output_dir=Path("./docsend_output"))
                return

    # Fallback: hardcoded test URLs
    docsend_urls = [
        # Add your DocSend URLs here for testing
        # {"url": "https://docsend.com/view/abc123", "company": "Example", "password": "optional"},
    ]

    if not docsend_urls:
        print("Usage: python docsend_router.py [results.json]")
        print("Or add URLs to docsend_urls list in main().")
        return

    results = await route_all_docsends(docsend_urls, output_dir=Path("./docsend_output"))


if __name__ == "__main__":
    asyncio.run(main())
