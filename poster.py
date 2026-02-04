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

# NOTE: Pokémon GO news has been seen using pokemongo.com URLs.
BASE_SITE = "https://pokemongo.com"
NEWS_URL = f"{BASE_SITE}/news"
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FB_RSS_URL = os.environ.get("G47IX_FB_RSS_URL")

# --- behavior knobs ---
OFFICIAL_CANDIDATES_LIMIT = 60          # how many official articles we consider for matching
MAX_OFFICIAL_POSTS_PER_RUN = 3          # prevent floods
MAX_FB_POSTS_PER_RUN = 5                # prevent floods
MATCH_THRESHOLD = 0.42                  # token-based matching; tune 0.38-0.55
SLEEP_BETWEEN_POSTS_SEC = 1.2           # gentle pacing to avoid rate limit


# -----------------------------
# State helpers
# -----------------------------
def load_state() -> Dict:
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

    return {"seen_urls": [], "seen_fb_posts": [], "posted_infographics": [], "bootstrapped": False}


def save_state(state: Dict):
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


def absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    # most links are /news/...
    return BASE_SITE + href


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

    # de-dupe preserve order
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
# Discord posting (429-safe)
# -----------------------------
def discord_post(payload: Dict, max_retries: int = 5):
    if not WEBHOOK_URL:
        print("Missing DISCORD_WEBHOOK_URL env var.", file=sys.stderr)
        sys.exit(1)

    for attempt in range(max_retries):
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)

        if r.status_code == 429:
            # Discord returns retry info either in JSON or header
            retry_after = None
            try:
                data = r.json()
                retry_after = data.get("retry_after")
            except Exception:
                pass

            if retry_after is None:
                retry_after = r.headers.get("Retry-After")

            try:
                wait = float(retry_after)
            except Exception:
                wait = 2.5

            wait = max(wait, 1.0)
            print(f"[DISCORD] Rate limited (429). Waiting {wait:.2f}s then retrying...")
            time.sleep(wait)
            continue

        r.raise_for_status()
        return

    raise RuntimeError("Discord webhook failed after retries (rate-limited or error).")


def post_official(meta: Dict):
    embed = {
        "title": meta["title"],
        "url": meta["url"],
        "description": meta["description"],
        "footer": {"text": f"Pokémon GO • {meta['published']}" if meta.get("published") else "Pokémon GO"},
    }
    if meta.get("image"):
        embed["image"] = {"url": meta["image"]}

    payload = {"username": "Pokémon GO News", "embeds": [embed]}
    discord_post(payload)
    time.sleep(SLEEP_BETWEEN_POSTS_SEC)


def post_infographic(official_meta: Dict, fb_post: Dict):
    img = fb_post.get("image_url")
    fb_link = fb_post.get("link")

    embed = {
        "title": "Infographic (G47IX)",
        "description": f"Matched to: **{official_meta.get('title','Pokémon GO News')}**\nSource: {fb_link}",
        "url": official_meta.get("url"),
    }
    if img:
        embed["image"] = {"url": img}

    payload = {"username": "Pokémon GO News", "embeds": [embed]}
    discord_post(payload)
    time.sleep(SLEEP_BETWEEN_POSTS_SEC)


# -----------------------------
# Facebook RSS parsing
# -----------------------------
def parse_rss_pubdate(pubdate: str) -> Optional[datetime]:
    # RSS.app usually uses RFC822-ish dates; we’ll best-effort parse
    if not pubdate:
        return None
    pubdate = pubdate.strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
    ):
        try:
            return datetime.strptime(pubdate, fmt)
        except Exception:
            continue
    return None


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
        pubdate_raw = (item.findtext("pubDate") or "").strip()

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

        items.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "image_url": image_url,
                "pubDate": pubdate_raw,
                "pubDate_dt": parse_rss_pubdate(pubdate_raw),
            }
        )

    return items[:30]


def is_infographic_post(post: Dict) -> bool:
    return bool(post.get("image_url"))


# -----------------------------
# Matching logic (keyword/token + similarity)
# -----------------------------
STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "in", "on", "for", "with", "of", "at", "by",
    "is", "are", "be", "will", "from", "into", "during", "event", "events", "pokemon",
    "pokémon", "go", "pokemongo", "pokemongo’s", "its", "it", "this", "that", "new",
}

def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"https?://\S+", " ", s)
    s = s.replace("#", " ")
    s = s.replace("’", "'")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def tokens(s: str) -> List[str]:
    s = normalize_text(s)
    out = []
    for t in s.split():
        if len(t) <= 2:
            continue
        if t in STOPWORDS:
            continue
        # remove pure years like 2026
        if re.fullmatch(r"\d{4}", t):
            continue
        out.append(t)
    return out


def jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def slug_keywords(url: str) -> List[str]:
    # /news/lunar-new-year-event-2026 -> ["lunar","new","year","event"]
    m = re.search(r"/news/([^/?#]+)", url or "")
    if not m:
        return []
    slug = m.group(1).replace("-", " ")
    return tokens(slug)


def extract_official_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    # support both pokemongolive.com and pokemongo.com
    m = re.search(r"(https?://(?:www\.)?(?:pokemongo\.com|pokemongolive\.com)/news/[^\s\"']+)", text)
    if m:
        return m.group(1).split("?")[0].split("#")[0]
    return None


def combined_match_score(fb_post: Dict, off_meta: Dict) -> float:
    fb_text = f"{fb_post.get('title','')} {fb_post.get('description','')}"
    off_text = f"{off_meta.get('title','')} {off_meta.get('description','')}"

    fb_toks = tokens(fb_text)
    off_toks = tokens(off_text)
    tok_score = jaccard(fb_toks, off_toks)

    sim = SequenceMatcher(None, normalize_text(fb_post.get("title", "")), normalize_text(off_meta.get("title", ""))).ratio()

    # slug bonus (very important for cases like Lunar New Year)
    slug_toks = slug_keywords(off_meta.get("url", ""))
    slug_score = jaccard(tokens(fb_post.get("title","")), slug_toks)

    score = (0.50 * tok_score) + (0.35 * sim) + (0.15 * slug_score)

    # small bonus if the phrase “lunar new year” / “valentine” etc appears in both
    fb_norm = normalize_text(fb_text)
    off_norm = normalize_text(off_text)
    for phrase in ["lunar new year", "valentine", "community day", "raid day", "go pass", "spotlight hour"]:
        if phrase in fb_norm and phrase in off_norm:
            score += 0.08

    return min(score, 1.0)


def match_fb_to_official(fb_post: Dict, official_metas: List[Dict]) -> Optional[Tuple[Dict, float]]:
    # 1) direct official URL in FB content
    direct = (
        extract_official_url_from_text(fb_post.get("title", "")) or
        extract_official_url_from_text(fb_post.get("description", ""))
    )
    if direct:
        for meta in official_metas:
            if meta.get("url") == direct:
                return meta, 1.0
        try:
            meta = parse_article_metadata(direct)
            return meta, 1.0
        except Exception:
            pass

    # 2) best score over candidates
    best_meta = None
    best = 0.0
    for meta in official_metas:
        s = combined_match_score(fb_post, meta)
        if s > best:
            best = s
            best_meta = meta

    if best_meta and best >= MATCH_THRESHOLD:
        return best_meta, best

    return None


# -----------------------------
# Main
# -----------------------------
def main():
    state = load_state()

    official_urls = get_latest_news_links(OFFICIAL_CANDIDATES_LIMIT)

    # Build meta cache (once)
    official_metas = []
    for u in official_urls:
        try:
            official_metas.append(parse_article_metadata(u))
        except Exception as ex:
            print(f"[WARN] Failed to parse official meta for {u}: {ex}")

    fb_posts = get_facebook_posts() if FB_RSS_URL else []

    # -----------------------------
    # Bootstrap mode (prevents flooding on first run)
    # -----------------------------
    if not state.get("bootstrapped", False):
        print("[BOOTSTRAP] First run detected. Recording latest items as seen (no posting).")

        # mark latest official + latest fb as seen
        state["seen_urls"] = list(dict.fromkeys(official_urls))[:OFFICIAL_CANDIDATES_LIMIT]
        state["seen_fb_posts"] = [p["link"] for p in fb_posts if p.get("link")][:30]
        state["posted_infographics"] = []
        state["bootstrapped"] = True

        save_state(state)
        print("[BOOTSTRAP] Done. Next run will post only truly-new items.")
        return

    seen_official = set(state.get("seen_urls", []))
    seen_fb = set(state.get("seen_fb_posts", []))
    posted_infographics = set(state.get("posted_infographics", []))

    # -----------------------------
    # Part A: post NEW official posts (bounded)
    # -----------------------------
    new_official = [u for u in official_urls if u not in seen_official]

    if not new_official:
        print("No new official posts.")
    else:
        # oldest first, but limit per run
        new_official = list(reversed(new_official))  # oldest first
        new_official = new_official[:MAX_OFFICIAL_POSTS_PER_RUN]

        for url in new_official:
            meta = next((m for m in official_metas if m.get("url") == url), None) or parse_article_metadata(url)
            print(f"[OFFICIAL] Posting: {meta['title']} -> {url}")
            post_official(meta)
            state["seen_urls"] = (state["seen_urls"] + [url])[-800:]

    # -----------------------------
    # Part B: FB infographics -> only post if matched
    # -----------------------------
    if not FB_RSS_URL:
        print("[FB] No G47IX_FB_RSS_URL set; skipping Facebook feed.")
        save_state(state)
        return

    if not fb_posts:
        print("[FB] No items found in feed.")
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

    # oldest first, bounded
    new_fb = list(reversed(new_fb))
    new_fb = new_fb[:MAX_FB_POSTS_PER_RUN]

    for fb_post in new_fb:
        fb_link = fb_post.get("link")
        fb_title = fb_post.get("title", "")
        print(f"[FB] Candidate: {fb_title} -> {fb_link}")

        match = match_fb_to_official(fb_post, official_metas)
        if not match:
            print(f"[FB] No official match found (threshold={MATCH_THRESHOLD:.2f}). Skipping.")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            continue

        official_meta, score = match
        official_url = official_meta.get("url")
        print(f"[FB] Matched! score={score:.2f} | OFFICIAL='{official_meta.get('title')}'")

        if official_url in posted_infographics:
            print("[FB] Already posted infographic for that official article. Skipping.")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            continue

        # Ensure official is posted first (so infographic is “under it”)
        if official_url not in set(state.get("seen_urls", [])):
            try:
                print(f"[FB] Official not seen yet; posting official first: {official_url}")
                post_official(official_meta)
                state["seen_urls"] = (state["seen_urls"] + [official_url])[-800:]
            except Exception as ex:
                print(f"[FB] Failed to post official; skipping infographic. Error: {ex}")
                state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
                continue

        # Post infographic
        try:
            print(f"[FB] Posting infographic under official: {official_url}")
            post_infographic(official_meta, fb_post)
            state["posted_infographics"] = (state["posted_infographics"] + [official_url])[-800:]
        except Exception as ex:
            print(f"[FB] Failed to post infographic. Error: {ex}")

        state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]

    save_state(state)


if __name__ == "__main__":
    main()
