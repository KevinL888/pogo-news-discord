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

NEWS_URL = "https://pokemongo.com/news"   # pokemongolive.com redirects here now
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FB_RSS_URL = os.environ.get("G47IX_FB_RSS_URL")

# Safety limits so you never spam if state gets wiped
MAX_OFFICIAL_POSTS_PER_RUN = 3
MAX_FB_INFOGRAPHICS_PER_RUN = 3

# How many official news items to keep in memory for matching
OFFICIAL_MATCH_POOL = 120

# Matching threshold (0..1). Lower = more matches (but more false positives)
MATCH_THRESHOLD = 0.42

# -----------------------------
# State helpers
# -----------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("seen_urls", [])
        data.setdefault("seen_fb_posts", [])
        data.setdefault("posted_infographics", [])
        data.setdefault("bootstrapped", False)

        # keep bounded
        data["seen_urls"] = data["seen_urls"][-800:]
        data["seen_fb_posts"] = data["seen_fb_posts"][-800:]
        data["posted_infographics"] = data["posted_infographics"][-800:]

        return data

    return {
        "seen_urls": [],
        "seen_fb_posts": [],
        "posted_infographics": [],
        "bootstrapped": False
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# -----------------------------
# HTTP helpers
# -----------------------------

def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (DiscordWebhookBot; +https://github.com)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def absolute_news_url(href: str) -> str:
    if href.startswith("http"):
        return href.split("#")[0].split("?")[0]
    # on pokemongo.com, href often looks like /news/...
    return "https://pokemongo.com" + href.split("#")[0].split("?")[0]


# -----------------------------
# Official news listing parsing
# -----------------------------

def get_official_listing(limit: int = OFFICIAL_MATCH_POOL) -> List[Dict]:
    """
    Parses the News listing page and returns a list like:
      { "url": "...", "title": "..." }
    This is much faster/cleaner for matching than fetching each article meta.
    """
    html = fetch(NEWS_URL)
    soup = BeautifulSoup(html, "html.parser")

    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/" in href and not href.endswith("/news/"):
            title = (a.get_text(" ", strip=True) or "").strip()
            url = absolute_news_url(href)
            if title and url:
                items.append({"url": url, "title": title})

    # De-dupe by URL preserving order
    seen = set()
    ordered = []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            ordered.append(it)

    return ordered[:limit]


def parse_article_metadata(article_url: str) -> Dict:
    """
    Fetches OG metadata for nice Discord embed image/description.
    """
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

def discord_post(payload: Dict, max_retries: int = 5):
    if not WEBHOOK_URL:
        print("Missing DISCORD_WEBHOOK_URL env var.", file=sys.stderr)
        sys.exit(1)

    for attempt in range(1, max_retries + 1):
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)

        # Discord rate limit
        if r.status_code == 429:
            retry_after = 2.0
            try:
                # Discord usually returns JSON with retry_after seconds
                data = r.json()
                retry_after = float(data.get("retry_after", retry_after))
            except Exception:
                # sometimes it’s a header
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except Exception:
                        pass

            wait = max(1.0, retry_after)
            print(f"[DISCORD] 429 rate limited. Waiting {wait:.2f}s then retrying (attempt {attempt}/{max_retries})...")
            time.sleep(wait)
            continue

        # Other errors
        if r.status_code >= 400:
            print(f"[DISCORD] Error {r.status_code}: {r.text}")
            r.raise_for_status()

        return  # success

    raise RuntimeError("Failed to post to Discord after retries due to rate limiting.")


def post_official(official_url: str):
    meta = parse_article_metadata(official_url)

    embed = {
        "title": meta["title"],
        "url": official_url,
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
        "url": official_url,
        "description": f"Matched to: **{official_title}**\nSource: {link}" if link else f"Matched to: **{official_title}**",
    }
    if img:
        embed["image"] = {"url": img}

    payload = {"username": "Pokémon GO News", "embeds": [embed]}
    discord_post(payload)


# -----------------------------
# Facebook RSS parsing
# -----------------------------

def get_facebook_posts() -> List[Dict]:
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

        enclosure = item.find("enclosure")
        if enclosure is not None:
            image_url = enclosure.attrib.get("url")

        if not image_url:
            for mc in item.findall(".//{http://search.yahoo.com/mrss/}content"):
                url = mc.attrib.get("url")
                if url:
                    image_url = url
                    break

        items.append({
            "title": title,
            "link": link,
            "description": description,
            "image_url": image_url,
        })

    return items[:50]


def is_infographic_post(post: Dict) -> bool:
    return bool(post.get("image_url"))


# -----------------------------
# Matching logic
# -----------------------------

def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"https?://\S+", " ", s)
    s = s.replace("#", " ")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def token_set(s: str) -> set:
    s = normalize_text(s)
    if not s:
        return set()
    return set([t for t in s.split(" ") if len(t) >= 3])


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def score_match(fb_title: str, off_title: str) -> float:
    fb_norm = normalize_text(fb_title)
    off_norm = normalize_text(off_title)

    if not fb_norm or not off_norm:
        return 0.0

    seq = SequenceMatcher(None, fb_norm, off_norm).ratio()
    fb_tokens = token_set(fb_title)
    off_tokens = token_set(off_title)
    jac = jaccard(fb_tokens, off_tokens)

    # containment bonus
    bonus = 0.0
    if fb_norm in off_norm or off_norm in fb_norm:
        bonus += 0.12

    # weighted blend: sequence catches phrasing similarity, jaccard catches keyword overlap
    return (0.65 * seq) + (0.35 * jac) + bonus


def best_official_match(fb_post: Dict, official_listing: List[Dict]) -> Optional[Tuple[Dict, float]]:
    fb_title = fb_post.get("title", "")
    best = None
    best_score = 0.0

    for off in official_listing:
        off_title = off.get("title", "")
        sc = score_match(fb_title, off_title)
        if sc > best_score:
            best_score = sc
            best = off

    if best and best_score >= MATCH_THRESHOLD:
        return best, best_score
    return None


# -----------------------------
# Main
# -----------------------------

def main():
    state = load_state()

    official_listing = get_official_listing(OFFICIAL_MATCH_POOL)
    official_urls = [x["url"] for x in official_listing]

    fb_posts = get_facebook_posts() if FB_RSS_URL else []

    # -----------------------------
    # Bootstrap: first run should NOT post anything
    # -----------------------------
    if not state.get("bootstrapped", False):
        print("[BOOTSTRAP] First run detected. Marking latest items as seen without posting.")

        # Mark latest official as seen
        state["seen_urls"] = (state.get("seen_urls", []) + official_urls)[-800:]

        # Mark latest FB items as seen
        for p in fb_posts[:50]:
            if p.get("link"):
                state["seen_fb_posts"] = (state.get("seen_fb_posts", []) + [p["link"]])[-800:]

        state["bootstrapped"] = True
        save_state(state)
        print("[BOOTSTRAP] Done. Next run will post only new items.")
        return

    seen_official = set(state.get("seen_urls", []))
    seen_fb = set(state.get("seen_fb_posts", []))
    posted_infographics = set(state.get("posted_infographics", []))

    # -----------------------------
    # A) Post NEW official items (capped)
    # -----------------------------
    new_official = [u for u in official_urls if u not in seen_official]
    if not new_official:
        print("No new official posts.")
    else:
        # post oldest first, cap how many
        to_post = list(reversed(new_official))[:MAX_OFFICIAL_POSTS_PER_RUN]
        for url in to_post:
            print(f"[OFFICIAL] Posting: {url}")
            post_official(url)
            state["seen_urls"] = (state["seen_urls"] + [url])[-800:]

        if len(new_official) > MAX_OFFICIAL_POSTS_PER_RUN:
            print(f"[OFFICIAL] {len(new_official) - MAX_OFFICIAL_POSTS_PER_RUN} more official posts waiting for future runs.")

    # -----------------------------
    # B) FB infographics → only post if matched (capped)
    # -----------------------------
    if not FB_RSS_URL:
        print("[FB] No G47IX_FB_RSS_URL set; skipping Facebook feed.")
        save_state(state)
        return

    new_fb = [
        p for p in fb_posts
        if p.get("link") and p["link"] not in seen_fb and is_infographic_post(p)
    ]

    if not new_fb:
        print("[FB] No new infographic posts.")
        save_state(state)
        return

    posted_this_run = 0

    # oldest first
    for fb_post in reversed(new_fb):
        fb_link = fb_post.get("link")
        fb_title = fb_post.get("title", "")
        print(f"[FB] Candidate: {fb_title} -> {fb_link}")

        match = best_official_match(fb_post, official_listing)
        if not match:
            print(f"[FB] No official match found (threshold={MATCH_THRESHOLD}). Skipping.")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            continue

        off, score = match
        official_url = off["url"]
        official_title = off["title"]
        print(f"[MATCH] score={score:.2f} | OFFICIAL='{official_title}'")

        # only once per official article
        if official_url in posted_infographics:
            print("[FB] Already posted infographic for this official article. Skipping.")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            continue

        # Ensure official is posted first (if it somehow wasn't)
        if official_url not in set(state.get("seen_urls", [])):
            print("[FB] Official not posted yet; posting official first...")
            post_official(official_url)
            state["seen_urls"] = (state["seen_urls"] + [official_url])[-800:]

        # Post infographic “under it”
        print("[FB] Posting infographic…")
        post_infographic(official_url, official_title, fb_post)

        state["posted_infographics"] = (state["posted_infographics"] + [official_url])[-800:]
        state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
        posted_this_run += 1

        if posted_this_run >= MAX_FB_INFOGRAPHICS_PER_RUN:
            print(f"[FB] Reached per-run cap ({MAX_FB_INFOGRAPHICS_PER_RUN}). Remaining will post on later runs.")
            break

    save_state(state)


if __name__ == "__main__":
    main()
