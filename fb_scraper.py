"""Scrape recent public posts from a Facebook page — free replacement for RSS.app.

Facebook's page plugin (facebook.com/plugins/page.php) is designed to be embedded
anonymously on any website, so it serves the page timeline without login. The
timeline is rendered client-side, so we load it in headless Chromium (Playwright)
and parse the resulting DOM.

Returns items shaped exactly like the old RSS entries:
    {"title": <post text>, "link": <permalink>, "description": "", "image_url": <url or None>}
"""

import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

EMBED_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Profile pictures use the t39.30808-1 asset family and tiny stp sizes;
# post photos use t39.30808-6.
PROFILE_PIC_MARKERS = ("t39.30808-1", "s50x50", "s32x32")


def _plugin_url(page_url: str) -> str:
    return (
        "https://www.facebook.com/plugins/page.php"
        f"?href={quote(page_url, safe='')}"
        "&tabs=timeline&width=500&height=2000"
        "&small_header=true&adapt_container_width=false"
        "&hide_cover=true&show_facepile=false"
    )


def _launch_browser(p):
    """Try system browsers first (present on GitHub runners and most dev
    machines) so we don't need `playwright install`; fall back to Playwright's
    bundled Chromium if it happens to be installed."""
    channels = []
    env_channel = os.environ.get("FB_BROWSER_CHANNEL")
    if env_channel:
        channels.append(env_channel)
    channels += ["chrome", "msedge", None]

    last_err: Optional[Exception] = None
    for ch in channels:
        try:
            kwargs: Dict[str, Any] = {"headless": True}
            if ch:
                kwargs["channel"] = ch
            browser = p.chromium.launch(**kwargs)
            print(f"[FB-SCRAPE] Launched browser channel={ch or 'bundled chromium'}")
            return browser
        except Exception as ex:
            last_err = ex
    raise RuntimeError(
        "No usable browser found for Playwright. Install Chrome/Edge or run "
        f"`playwright install chromium`. Last error: {last_err}"
    )


CONSENT_BUTTON_SELECTORS = [
    'button[title="Allow all cookies"]',
    '[aria-label="Allow all cookies"]',
    'button:has-text("Allow all cookies")',
    'button:has-text("Accept all")',
    'button[title="Only allow essential cookies"]',
    'button:has-text("Only allow essential cookies")',
]


def _dismiss_consent(page) -> None:
    """Facebook shows a cookie-consent dialog to some IPs/regions before any
    content renders. Click through it if present."""
    for sel in CONSENT_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                print(f"[FB-SCRAPE] Consent dialog detected, clicking: {sel}")
                btn.click()
                page.wait_for_timeout(2000)
                return
        except Exception:
            continue


def _print_diagnostics(page, html: str) -> None:
    """When the timeline is missing, log what Facebook served instead."""
    print("[FB-SCRAPE] No posts found in rendered page. Diagnostics:")
    try:
        print(f"[FB-SCRAPE]   title: {page.title()!r}")
    except Exception:
        pass
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    print(f"[FB-SCRAPE]   visible text ({len(text)} chars): {text[:600]!r}")
    lowered = html.lower()
    for marker in ("log in", "login", "cookie", "consent", "not available",
                   "unavailable", "something went wrong", "captcha", "checkpoint"):
        if marker in lowered:
            print(f"[FB-SCRAPE]   marker present in HTML: {marker!r}")


def _clean_post_link(href: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    href = href.split("?")[0].split("#")[0]
    if "/posts/" not in href:
        return None
    return href


def _extract_image(wrapper) -> Optional[str]:
    for img in wrapper.find_all("img", src=True):
        src = img["src"]
        if "scontent" not in src:
            continue
        if any(marker in src for marker in PROFILE_PIC_MARKERS):
            continue
        return src
    return None


def parse_embed_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []
    seen_links = set()

    for wrapper in soup.select("div.userContentWrapper"):
        link = None
        for a in wrapper.find_all("a", href=True):
            link = _clean_post_link(a["href"])
            if link:
                break
        if not link or link in seen_links:
            continue
        seen_links.add(link)

        text = ""
        content = wrapper.select_one(".userContent")
        if content:
            text = content.get_text(" ", strip=True)
            text = re.sub(r"\s*See [Mm]ore\s*$", "", text).strip()

        items.append(
            {
                "title": text,
                "link": link,
                "description": "",
                "image_url": _extract_image(wrapper),
            }
        )

    return items


def get_page_posts(page_url: str, timeout_ms: int = 60000) -> List[Dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    debug_dir = os.environ.get("FB_DEBUG_DUMP")

    with sync_playwright() as p:
        browser = _launch_browser(p)
        try:
            page = browser.new_page(
                viewport={"width": 520, "height": 2100},
                user_agent=EMBED_UA,
                locale="en-US",
            )
            page.goto(_plugin_url(page_url), wait_until="networkidle", timeout=timeout_ms)
            _dismiss_consent(page)

            try:
                page.wait_for_selector("div.userContentWrapper", timeout=15000)
            except Exception:
                pass  # diagnosed below

            # give late-loading images a moment
            page.wait_for_timeout(2500)
            html = page.content()

            items = parse_embed_html(html)
            if not items:
                _print_diagnostics(page, html)

            if debug_dir:
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, "rendered.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                page.screenshot(path=os.path.join(debug_dir, "rendered.png"), full_page=True)
                print(f"[FB-SCRAPE] Debug dump written to {debug_dir}")
        finally:
            browser.close()

    return items


if __name__ == "__main__":
    import json
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.facebook.com/g47ix"
    posts = get_page_posts(url)
    print(json.dumps(posts, indent=2, ensure_ascii=False))
    print(f"\n{len(posts)} post(s) scraped", file=sys.stderr)
