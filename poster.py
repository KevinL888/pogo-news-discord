import json
import os
import re
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

NEWS_URL = "https://pokemongolive.com/news"
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FB_RSS_URL = os.environ.get("G47IX_FB_RSS_URL")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Backward-compatible defaults
            if "seen_urls" not in data:
                data["seen_urls"] = []
            if "seen_fb_posts" not in data:
                data["seen_fb_posts"] = []
            return data
    return {"seen_urls": [], "seen_fb_posts": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://pokemongolive.com" + href


def fetch(url: str) -> str:
    # Simple browser-ish headers help avoid blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (DiscordWebhookBot; +https://github.com)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def get_latest_news_links():
    html = fetch(NEWS_URL)
    soup = BeautifulSoup(html, "html.parser")

    # Pokémon GO news pages contain many links; we filter for /news/...
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/" in href and not href.endswith("/news/"):
            # Avoid junk like share links, etc.
            if re.search(r"^/news/[^?#]+", href):
                links.append(absolute_url(href.split("#")[0].split("?")[0]))

    # De-duplicate while preserving order
    seen = set()
    ordered = []
    for u in links:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    # Usually newest is near the top; keep first ~10 candidates
    return ordered[:10]


def parse_article_metadata(article_url: str):
    html = fetch(article_url)
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop=None, name=None):
        if prop:
            tag = soup.find("meta", attrs={"property": prop})
        else:
            tag = soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta(prop="og:title") or "Pokémon GO News"
    description = meta(prop="og:description") or meta(name="description") or ""
    image = meta(prop="og:image")
    published = meta(prop="article:published_time")

    # Normalize published time if present
    published_text = None
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            published_text = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            published_text = published

    return {
        "title": title,
        "description": description[:250],
        "image": image,
        "published": published_text,
    }


def post_to_discord(article_url: str, meta: dict):
    if not WEBHOOK_URL:
        print("Missing DISCORD_WEBHOOK_URL env var.", file=sys.stderr)
        sys.exit(1)

    # Build a clean embed similar to PatchBot style
    embed = {
        "title": meta["title"],
        "url": article_url,
        "description": meta["description"],
    }

    if meta.get("image"):
        embed["image"] = {"url": meta["image"]}

    if meta.get("published"):
        embed["footer"] = {"text": f"Pokémon GO • {meta['published']}"}
    else:
        embed["footer"] = {"text": "Pokémon GO"}

    payload = {
        "username": "Pokémon GO News",
        "embeds": [embed],
    }

    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()


# -----------------------------
# Facebook (G47IX) RSS support
# -----------------------------

def get_facebook_posts():
    """
    Reads the RSS.app feed for https://www.facebook.com/g47ix/
    Returns list of dicts: {title, link, description, image_url}
    """
    if not FB_RSS_URL:
        return []

    xml_text = fetch(FB_RSS_URL)
    root = ET.fromstring(xml_text)

    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()

        image_url = None

        # RSS enclosure (most common)
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image_url = enclosure.attrib.get("url")

        # Some feeds use media:content
        if not image_url:
            for mc in item.findall(".//{http://search.yahoo.com/mrss/}content"):
                url = mc.attrib.get("url")
                if url:
                    image_url = url
                    break

        items.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "image_url": image_url,
            }
        )

    return items[:15]


def is_infographic_post(post: dict) -> bool:
    # Minimal filter for now: must have an image attachment
    return bool(post.get("image_url"))


def main():
    state = load_state()

    # -----------------------------
    # Part 1: Official news -> Discord (existing)
    # -----------------------------
    seen_urls = set(state.get("seen_urls", []))

    candidates = get_latest_news_links()
    if not candidates:
        print("No news links found on page.")
        return

    new_items = [u for u in candidates if u not in seen_urls]

    if not new_items:
        print("No new official posts.")
    else:
        # Post in reverse order so older new items go first (nice if multiple)
        for url in reversed(new_items):
            meta = parse_article_metadata(url)
            print(f"[OFFICIAL] Posting: {meta['title']} -> {url}")
            post_to_discord(url, meta)
            state["seen_urls"] = (state.get("seen_urls", []) + [url])[-200:]  # keep last 200

    # -----------------------------
    # Part 2: Facebook feed detection (NO posting yet)
    # -----------------------------
    fb_posts = get_facebook_posts()
    seen_fb = set(state.get("seen_fb_posts", []))

    new_fb_posts = [
        p for p in fb_posts
        if p.get("link") and p["link"] not in seen_fb and is_infographic_post(p)
    ]

    if not FB_RSS_URL:
        print("[FB] No G47IX_FB_RSS_URL set; skipping Facebook feed.")
    elif not fb_posts:
        print("[FB] No items found in feed.")
    elif not new_fb_posts:
        print("[FB] No new infographic posts.")
    else:
        for post in reversed(new_fb_posts):
            print(f"[FB] Found infographic post (not posting yet): {post.get('title')} -> {post.get('link')}")
            # Mark as seen for now (we'll change this once OCR+matching is added)
            state["seen_fb_posts"] = (state.get("seen_fb_posts", []) + [post["link"]])[-200:]

    save_state(state)


if __name__ == "__main__":
    main()
