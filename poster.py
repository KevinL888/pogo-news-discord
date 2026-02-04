import json
import os
import re
import sys
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup

NEWS_URL = "https://pokemongolive.com/news"
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FB_RSS_URL = os.environ.get("G47IX_FB_RSS_URL")

# How many official articles to consider for matching (higher = better match, slightly more work)
OFFICIAL_CANDIDATES_LIMIT = int(os.environ.get("OFFICIAL_CANDIDATES_LIMIT", "50"))

# Matching threshold (0..1). Higher = stricter (less false positives), lower = more matches.
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.55"))

# Safety: limit how many OFFICIAL posts we will send in a single run (prevents backfill spam + 429s)
MAX_OFFICIAL_POSTS_PER_RUN = int(os.environ.get("MAX_OFFICIAL_POSTS_PER_RUN", "3"))

# First-run safety: if state is empty, seed this many newest official URLs as "seen" and DO NOT POST
BOOTSTRAP_SEEN_COUNT = int(os.environ.get("BOOTSTRAP_SEEN_COUNT", "20"))

# Discord rate-limit handling
DISCORD_MAX_RETRIES = int(os.environ.get("DISCORD_MAX_RETRIES", "5"))


# -----------------------------
# State helpers
# -----------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Backward-compatible defaults
        data.setdefault("seen_urls", [])
        data.setdefault("seen_fb_posts", [])
        data.setdefault("posted_infographics", [])
        data.setdefault("bootstrapped", False)

        # Keep these from growing forever
        data["seen_urls"] = data["seen_urls"][-500:]
        data["seen_fb_posts"] = data["seen_fb_posts"][-500:]
        data["posted_infographics"] = data["posted_infographics"][-500:]

        return data

    return {
        "seen_urls": [],
        "seen_fb_posts": [],
        "posted_infographics": [],
        "bootstrapped": False,
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# -----------------------------
# HTTP helpers
# -----------------------------
def absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://pokemongolive.com" + href


def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (DiscordWebhookBot; +https://github.com)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


# -----------------------------
# Official news scraping
# -----------------------------
def get_latest_news_links(limit: int = OFFICIAL_CANDIDATES_LIMIT) -> List[str]:
    html = fetch(NEWS_URL)
    soup = BeautifulSoup(html, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/" in href and not href.endswith("/news/"):
            if re.search(r"^/news/[^?#]+", href):
                links.append(absolute_url(href.split("#")[0].split("?")[0]))

    # De-dupe preserving order
    seen = set()
    ordered = []
    for u in links:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered[:limit]


def parse_article_metadata(article_url: str) -> Dict:
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
        "url": article_url,
    }


# -----------------------------
# Discord posting (with 429 handling)
# -----------------------------
def discord_post(payload: Dict):
    if not WEBHOOK_URL:
        print("Missing DISCORD_WEBHOOK_URL env var.", file=sys.stderr)
        sys.exit(1)

    for attempt in range(1, DISCORD_MAX_RETRIES + 1):
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)

        # OK
        if 200 <= r.status_code < 300:
            return

        # Rate limited
        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.5))
            except Exception:
                retry_after = 2.0

            # Add a small buffer
            sleep_s = min(retry_after + 0.25, 15.0)
            print(f"[DISCORD] 429 rate limited. Sleeping {sleep_s:.2f}s (attempt {attempt}/{DISCORD_MAX_RETRIES})")
            time.sleep(sleep_s)
            continue

        # Other error
        try:
            r.raise_for_status()
        except Exception as ex:
            print(f"[DISCORD] Error posting webhook: {r.status_code} {r.text}")
            raise ex

    raise RuntimeError("[DISCORD] Failed to post after retries due to rate limiting.")


def post_official(article_url: str, meta: Dict):
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

    payload = {"username": "Pokémon GO News", "embeds": [embed]}
    discord_post(payload)


def post_infographic(official_url: str, official_title: str, fb_post: Dict):
    img = fb_post.get("image_url")
    link = fb_post.get("link")

    embed = {
        "title": "Infographic (G47IX)",
        "description": f"Matched to: **{official_title}**\nSource: {link}" if link else f"Matched to: **{official_title}**",
        "url": official_url,
    }
    if img:
        embed["image"] = {"url": img}

    payload = {"username": "Pokémon GO News", "embeds": [embed]}
    discord_post(payload)


# -----------------------------
# Facebook RSS (RSS.app) parsing
# -----------------------------
def get_facebook_posts() -> List[Dict]:
    """
    Reads the RSS feed from RSS.app for the G47IX Facebook page.
    Returns list: {title, link, description, image_url}
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

        # RSS enclosure
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image_url = enclosure.attrib.get("url")

        # media:content
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

    return items[:20]


def is_infographic_post(post: Dict) -> bool:
    # Minimal: must have an image
    return bool(post.get("image_url"))


# -----------------------------
# Matching logic (no OCR yet)
# -----------------------------
def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"https?://\S+", " ", s)      # remove URLs
    s = s.replace("#", " ")                  # keep hashtag words
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)    # strip punctuation/emojis
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def extract_official_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(https?://(?:www\.)?pokemongo(?:live)?\.com/news/[^\s\"']+)", text)
    if m:
        return m.group(1).split("?")[0].split("#")[0]
    return None


def best_title_match(fb_text: str, official_metas: List[Dict]) -> Optional[Tuple[Dict, float]]:
    fb_norm = normalize_text(fb_text)
    if not fb_norm:
        return None

    best = None
    best_score = 0.0

    for meta in official_metas:
        off_norm = normalize_text(meta.get("title", ""))
        if not off_norm:
            continue

        score = SequenceMatcher(None, fb_norm, off_norm).ratio()

        # Bonus if one contains the other
        if fb_norm in off_norm or off_norm in fb_norm:
            score += 0.15

        if score > best_score:
            best_score = score
            best = meta

    if best and best_score >= MATCH_THRESHOLD:
        return best, best_score

    return None


def match_fb_to_official(fb_post: Dict, official_metas: List[Dict]) -> Optional[Dict]:
    """
    Returns matched official meta or None.
    Priority:
      1) direct official URL present in FB title/description
      2) title similarity
    """
    direct = (
        extract_official_url_from_text(fb_post.get("title", "")) or
        extract_official_url_from_text(fb_post.get("description", ""))
    )
    if direct:
        for meta in official_metas:
            if meta.get("url") == direct:
                return meta
        try:
            return parse_article_metadata(direct)
        except Exception:
            pass

    fb_title = fb_post.get("title", "")
    match = best_title_match(fb_title, official_metas)
    if match:
        meta, score = match
        print(f"[MATCH] FB -> Official by title similarity: score={score:.2f} | FB='{fb_title}' | OFFICIAL='{meta.get('title')}'")
        return meta

    return None


# -----------------------------
# Main
# -----------------------------
def main():
    state = load_state()

    official_urls = get_latest_news_links(OFFICIAL_CANDIDATES_LIMIT)

    # BOOTSTRAP protection: prevents posting old stuff if state was wiped
    if not state.get("bootstrapped", False) and len(state.get("seen_urls", [])) == 0:
        seed = official_urls[:BOOTSTRAP_SEEN_COUNT]
        state["seen_urls"] = seed[-500:]
        state["bootstrapped"] = True
        save_state(state)
        print(f"[BOOTSTRAP] Seeded {len(seed)} official URLs as seen. No posting this run.")
        return

    # Build official metadata cache (once per run)
    official_metas = []
    for u in official_urls:
        try:
            official_metas.append(parse_article_metadata(u))
        except Exception as ex:
            print(f"[WARN] Failed to parse official meta for {u}: {ex}")

    seen_official = set(state.get("seen_urls", []))
    seen_fb = set(state.get("seen_fb_posts", []))
    posted_infographics = set(state.get("posted_infographics", []))

    # -------------------------------------------------
    # Part A: Post new OFFICIAL posts (rate-safe + capped)
    # -------------------------------------------------
    new_official_urls = [u for u in official_urls if u not in seen_official]

    if not new_official_urls:
        print("No new official posts.")
    else:
        # Only take the newest N (prevent spam)
        to_post = list(reversed(new_official_urls))  # older-first
        if len(to_post) > MAX_OFFICIAL_POSTS_PER_RUN:
            # Keep the newest N, still post them in chronological order
            to_post = to_post[-MAX_OFFICIAL_POSTS_PER_RUN:]

        for url in to_post:
            meta = next((m for m in official_metas if m.get("url") == url), None)
            if not meta:
                meta = parse_article_metadata(url)

            print(f"[OFFICIAL] Posting: {meta['title']} -> {url}")
            post_official(url, meta)
            state["seen_urls"] = (state["seen_urls"] + [url])[-500:]

    # -------------------------------------------------
    # Part B: Process FB infographics → ONLY post if matched
    # -------------------------------------------------
    if not FB_RSS_URL:
        print("[FB] No G47IX_FB_RSS_URL set; skipping Facebook feed.")
        save_state(state)
        return

    fb_posts = get_facebook_posts()
    if not fb_posts:
        print("[FB] No items found in feed.")
        save_state(state)
        return

    new_fb_posts = [
        p for p in fb_posts
        if p.get("link") and p["link"] not in seen_fb and is_infographic_post(p)
    ]

    if not new_fb_posts:
        print("[FB] No new infographic posts.")
        save_state(state)
        return

    # Older first
    for fb_post in reversed(new_fb_posts):
        fb_link = fb_post.get("link")
        fb_title = fb_post.get("title", "")

        print(f"[FB] New candidate: {fb_title} -> {fb_link}")

        matched_official = match_fb_to_official(fb_post, official_metas)

        if not matched_official:
            print(f"[FB] No official match found. Skipping post: {fb_title}")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-500:]
            continue

        official_url = matched_official.get("url")
        official_title = matched_official.get("title", "Pokémon GO News")

        if official_url in posted_infographics:
            print(f"[FB] Already posted infographic for official: {official_url}. Skipping.")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-500:]
            continue

        # Ensure official article is posted first (so infographic appears “under it”)
        if official_url not in set(state.get("seen_urls", [])):
            try:
                print(f"[FB] Official not posted yet; posting official first: {official_title} -> {official_url}")
                post_official(official_url, matched_official)
                state["seen_urls"] = (state["seen_urls"] + [official_url])[-500:]
            except Exception as ex:
                print(f"[FB] Failed to post official for matched infographic; skipping infographic. Error: {ex}")
                state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-500:]
                continue

        # Post infographic
        try:
            print(f"[FB] Posting infographic under official: {official_title} -> {official_url}")
            post_infographic(official_url, official_title, fb_post)
            state["posted_infographics"] = (state["posted_infographics"] + [official_url])[-500:]
        except Exception as ex:
            print(f"[FB] Failed to post infographic. Error: {ex}")

        state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-500:]

    save_state(state)


if __name__ == "__main__":
    main()
