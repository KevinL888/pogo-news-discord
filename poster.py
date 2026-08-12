import json
import os
import re
import sys
import time
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import List, Dict, Optional, Tuple, Any

import requests
from bs4 import BeautifulSoup

# ============================================================
# Config
# ============================================================

BASE_SITE = "https://pokemongo.com"
NEWS_URL = f"{BASE_SITE}/news"
STATE_FILE = "state.json"

def clean_env_url(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    # remove whitespace/newlines that GitHub Secrets sometimes include
    val = val.strip()
    # guard against accidental embedded whitespace
    val = re.sub(r"\s+", "", val)
    return val or None

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_FORUM_CHANNEL_IDS = [
    cid.strip()
    for cid in os.environ.get("DISCORD_FORUM_CHANNEL_IDS", "").split(",")
    if cid.strip()
]


DISCORD_API_BASE = "https://discord.com/api/v10"
FB_RSS_URL = clean_env_url(os.environ.get("G47IX_FB_RSS_URL"))
FB_PAGE_URL = clean_env_url(os.environ.get("FB_PAGE_URL")) or "https://www.facebook.com/g47ix"
# Channel in OUR server that follows G47IX's announcement channel (crossposts land here)
G47IX_MIRROR_CHANNEL_ID = clean_env_url(os.environ.get("G47IX_MIRROR_CHANNEL_ID"))

OFFICIAL_CANDIDATES_LIMIT = int(os.environ.get("OFFICIAL_CANDIDATES_LIMIT", "60"))
MAX_OFFICIAL_POSTS_PER_RUN = int(os.environ.get("MAX_OFFICIAL_POSTS_PER_RUN", "3"))
MAX_FB_POSTS_PER_RUN = int(os.environ.get("MAX_FB_POSTS_PER_RUN", "5"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.38"))
# OCR text is long and noisy, which inflates similarity scores — require more
OCR_MATCH_THRESHOLD = float(os.environ.get("OCR_MATCH_THRESHOLD", "0.60"))
# ...unless the official title's distinctive words appear in the image text itself
OCR_TITLE_OVERLAP = float(os.environ.get("OCR_TITLE_OVERLAP", "0.5"))

# Words too common in Pokémon GO news titles to count as evidence that an
# infographic belongs to a specific article (they gamed the overlap rule:
# "research/encounter/complete" matched a how-to graphic to a Rayquaza article).
# Abbreviations G47IX uses in graphics vs. the full phrases in official titles
OCR_TOKEN_ALIASES = {
    "wcs": {"world", "championships"},
    "gbl": {"battle", "league"},
    "cd": {"community", "day"},
}

GENERIC_TITLE_TOKENS = {
    "research", "timed", "complete", "encounter", "encounters", "catch",
    "raid", "raids", "day", "days", "weekend", "hour", "spotlight",
    "community", "update", "updates", "battle", "battles", "league",
    "max", "mega", "pass", "more", "await", "awaits", "arrives", "returns",
    "return", "ready", "new", "celebrate", "celebration", "season", "global",
    "live", "final", "details", "begins", "soars", "splash",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
# Recurring series graphics that never map to a single official article ("|"-separated)
FB_SKIP_PATTERNS = [
    p.strip().lower()
    for p in os.environ.get("FB_SKIP_PATTERNS", "go weekly update").split("|")
    if p.strip()
]
SLEEP_BETWEEN_POSTS_SEC = float(os.environ.get("SLEEP_BETWEEN_POSTS_SEC", "1.2"))

# If true, we do NOT mark unmatched FB posts as seen (useful while tuning matching)
DEBUG_KEEP_UNMATCHED_FB = os.environ.get("DEBUG_KEEP_UNMATCHED_FB", "0") == "1"

# OCR fallback (disabled by default)
ENABLE_OCR_FALLBACK = os.environ.get("ENABLE_OCR_FALLBACK", "0") == "1"
OCR_MAX_CHARS = int(os.environ.get("OCR_MAX_CHARS", "1500"))  # safety clamp

# Debug matching output
DEBUG_MATCH_TOP_N = int(os.environ.get("DEBUG_MATCH_TOP_N", "3"))

# ============================================================
# State helpers
# ============================================================

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("seen_urls", [])
        data.setdefault("seen_fb_posts", [])
        data.setdefault("posted_infographics", [])
        data.setdefault("threads", {})
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
        "threads": {},
        "bootstrapped": False
}



def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ============================================================
# HTTP helpers
# ============================================================

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
    return BASE_SITE + href


# ============================================================
# Official news scraping
# ============================================================

def get_latest_news_links(limit: int = OFFICIAL_CANDIDATES_LIMIT) -> List[str]:
    html = fetch(NEWS_URL)
    soup = BeautifulSoup(html, "html.parser")

    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/" in href and not href.endswith("/news/"):
            if re.search(r"^/news/[^?#]+", href):
                links.append(absolute_url(href.split("#")[0].split("?")[0]))

    # de-dupe preserve order
    seen = set()
    ordered: List[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered[:limit]


def parse_article_metadata(article_url: str) -> Dict[str, Any]:
    html = fetch(article_url)
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
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

    # Extract body text (important!)
    body_text = ""

    article_div = soup.find("article")
    if article_div:
        body_text = article_div.get_text(separator=" ", strip=True)

    body_text = re.sub(r"\s+", " ", body_text)

    return {
        "title": title,
        "description": description[:250],
        "body": body_text[:4000],  # clamp to safe size
        "image": image,
        "published": published_text,
        "url": article_url,
    }


# ============================================================
# Discord posting (429-safe)
# ============================================================

def post_official(meta: Dict[str, Any], state: Dict[str, Any]) -> None:

    state.setdefault("threads", {})

    for forum_id in DISCORD_FORUM_CHANNEL_IDS:

        # Ensure url entry exists lazily
        if meta["url"] not in state["threads"]:
            state["threads"][meta["url"]] = {"channels": {}}

        # Skip if already posted in this forum
        if forum_id in state["threads"][meta["url"]]["channels"]:
            continue

        embed = {
            "title": meta["title"],
            "url": meta["url"],
            "description": meta["description"],
            "footer": {
                "text": f"Pokémon GO • {meta['published']}" if meta.get("published") else "Pokémon GO"
            },
        }

        if meta.get("image"):
            embed["image"] = {"url": meta["image"]}

        payload = {
            "name": meta["title"][:100],
            "message": {"embeds": [embed]},
        }

        try:
            data = discord_api(
                "POST",
                f"/channels/{forum_id}/threads",
                payload
            )

            thread_id = data["id"]

            state["threads"][meta["url"]]["channels"][forum_id] = {
                "thread_id": thread_id,
                "infographic_posted": False,
            }

            time.sleep(SLEEP_BETWEEN_POSTS_SEC)

        except Exception as ex:
            print(f"[ERROR] Failed creating thread in forum {forum_id}: {ex}")


def post_infographic(official_meta: Dict[str, Any], fb_post: Dict[str, Any], state: Dict[str, Any]) -> None:
    url = official_meta["url"]
    thread_info = state.get("threads", {}).get(url)

    if not thread_info:
        print("[WARN] No threads found for this official post.")
        return

    # Re-upload the image as our own attachment: source URLs (Discord CDN /
    # Facebook CDN) are signed and expire, which blanks the embed later.
    img = download_image(fb_post["image_url"]) if fb_post.get("image_url") else None

    for forum_id, data in thread_info["channels"].items():

        if data.get("infographic_posted"):
            continue

        embed = {
            "title": "Infographic (G47IX)",
            "description": (
                f"Matched to: **{official_meta.get('title','Pokémon GO News')}**\n"
                f"Source: {fb_post.get('link')}"
            ),
            "url": official_meta.get("url"),
        }

        if img:
            file_bytes, filename, content_type = img
            embed["image"] = {"url": f"attachment://{filename}"}
            discord_api_multipart(
                f"/channels/{data['thread_id']}/messages",
                {
                    "embeds": [embed],
                    "attachments": [{"id": 0, "filename": filename}],
                },
                filename,
                file_bytes,
                content_type,
            )
        else:
            # fallback: link the image directly (may expire, better than nothing)
            if fb_post.get("image_url"):
                embed["image"] = {"url": fb_post["image_url"]}
            discord_api(
                "POST",
                f"/channels/{data['thread_id']}/messages",
                {"embeds": [embed]},
            )

        data["infographic_posted"] = True

        time.sleep(SLEEP_BETWEEN_POSTS_SEC)




def download_image(url: str) -> Optional[Tuple[bytes, str, str]]:
    """Download an image so it can be re-uploaded as a Discord attachment.
    Returns (bytes, filename, content_type) or None on any failure."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (DiscordWebhookBot; +https://github.com)",
        }
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()

        content_type = (r.headers.get("Content-Type") or "image/png").split(";")[0].strip()
        if not content_type.startswith("image/"):
            return None
        if len(r.content) > 9_500_000:  # stay under Discord's 10MB bot upload limit
            return None

        name = re.sub(r"[^\w.-]", "_", os.path.basename(url.split("?")[0])) or "infographic"
        if "." not in name:
            name += "." + content_type.split("/")[-1]
        return r.content, name, content_type
    except Exception as ex:
        print(f"[WARN] Failed to download infographic image: {ex}")
        return None


def discord_api_multipart(path: str, payload: Dict[str, Any], filename: str,
                          file_bytes: bytes, content_type: str,
                          max_retries: int = 5) -> Dict[str, Any]:
    """POST a message with a file attachment (multipart), 429-safe."""
    url = f"{DISCORD_API_BASE}{path}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

    for _ in range(max_retries):
        r = requests.post(
            url,
            headers=headers,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (filename, file_bytes, content_type)},
            timeout=60,
        )

        if r.status_code == 429:
            retry_after = float(r.json().get("retry_after", 2.0))
            time.sleep(max(retry_after, 1.0))
            continue

        r.raise_for_status()
        return r.json() if r.text else {}

    raise RuntimeError("Discord API failed after retries")


def discord_api(method: str, path: str, payload: Optional[Dict[str, Any]] = None, max_retries: int = 5) -> Dict[str, Any]:
    url = f"{DISCORD_API_BASE}{path}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    for _ in range(max_retries):
        r = requests.request(method, url, headers=headers, json=payload, timeout=30)

        if r.status_code == 429:
            retry_after = float(r.json().get("retry_after", 2.0))
            time.sleep(max(retry_after, 1.0))
            continue

        r.raise_for_status()
        return r.json() if r.text else {}

    raise RuntimeError("Discord API failed after retries")
    


# ============================================================
# G47IX posts (Discord mirror primary, FB embed / RSS fallbacks)
# ============================================================

def get_facebook_posts() -> List[Dict[str, Any]]:
    """Fetch recent G47IX posts, trying sources in order:
    1. Discord mirror channel (our channel following G47IX's #us-news) — works
       from GitHub Actions since it's just the Discord API.
    2. FB page-plugin embed scrape — works from residential IPs only
       (Facebook login-walls datacenter IPs).
    3. Legacy RSS.app feed, if still configured."""
    if G47IX_MIRROR_CHANNEL_ID:
        try:
            items = get_facebook_posts_discord()
            print(f"[FB] Discord mirror returned {len(items)} message(s).")
            return items
        except Exception as ex:
            print(f"[FB] Discord mirror read failed: {ex}")

    try:
        from fb_scraper import get_page_posts
        posts = get_page_posts(FB_PAGE_URL)
        if posts:
            return posts[:30]
        print("[FB] Embed scrape returned 0 posts.")
    except Exception as ex:
        print(f"[FB] Embed scrape failed: {ex}")

    if FB_RSS_URL:
        print("[FB] Falling back to RSS feed.")
        try:
            return get_facebook_posts_rss()
        except Exception as ex:
            print(f"[FB] RSS fallback failed: {ex}")

    return []


def get_facebook_posts_discord() -> List[Dict[str, Any]]:
    """Read the mirror channel that follows G47IX's announcement channel.
    Crossposted messages carry the infographic as an image attachment and
    sometimes caption text. Requires the bot to have View Channel + Read
    Message History there, and the Message Content intent enabled (without it
    Discord blanks content/attachments on other users' messages)."""
    if not G47IX_MIRROR_CHANNEL_ID:
        return []

    channel = discord_api("GET", f"/channels/{G47IX_MIRROR_CHANNEL_ID}")
    guild_id = channel.get("guild_id", "@me")

    msgs = discord_api("GET", f"/channels/{G47IX_MIRROR_CHANNEL_ID}/messages?limit=30")

    items: List[Dict[str, Any]] = []
    for m in msgs:  # newest first, same ordering the RSS feed had
        image_url = None
        for att in m.get("attachments", []):
            if (att.get("content_type") or "").startswith("image/"):
                image_url = att.get("url")
                break
        if not image_url:
            for emb in m.get("embeds", []):
                image_url = (
                    (emb.get("image") or {}).get("url")
                    or (emb.get("thumbnail") or {}).get("url")
                )
                if image_url:
                    break

        text = (m.get("content") or "").strip()
        # drop Discord markup: mentions <@&123>, timestamps <t:123:d>, custom emoji <:name:123>
        text = re.sub(r"<[@#][!&]?\d+>", " ", text)
        text = re.sub(r"<t:\d+(?::[a-zA-Z])?>", " ", text)
        text = re.sub(r"<a?:\w+:\d+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        link = f"https://discord.com/channels/{guild_id}/{G47IX_MIRROR_CHANNEL_ID}/{m['id']}"

        items.append(
            {
                "title": text,
                "link": link,
                "description": "",
                "image_url": image_url,
            }
        )

    return items


def get_facebook_posts_rss() -> List[Dict[str, Any]]:
    if not FB_RSS_URL:
        return []

    xml_text = fetch(FB_RSS_URL)
    root = ET.fromstring(xml_text)

    items: List[Dict[str, Any]] = []
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

        items.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "image_url": image_url,
            }
        )

    return items[:30]


def is_infographic_post(post: Dict[str, Any]) -> bool:
    return bool(post.get("image_url"))


# ============================================================
# Matching logic
# ============================================================

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "in", "on", "for", "with", "of", "at", "by",
    "is", "are", "be", "will", "from", "into", "during", "event", "events",
    "pokemon", "pokémon", "go", "pokemongo", "pokemongo’s", "its", "it", "this", "that",
}

KEYWORD_BONUS = [
    "lunar", "valentine", "raid", "shadow", "mega", "community", "pass",
    "spotlight", "go", "tour", "fest", "research", "battle",
]

PHRASE_BONUS = [
    "lunar new year",
    "valentine",
    "community day",
    "raid day",
    "go pass",
    "spotlight hour",
    "mega evolution",
    "mega raid",
    "super mega",
]


# ============================================================
# Pokémon name extraction
# ============================================================

# Words that are NOT Pokémon but frequently appear
NON_POKEMON_WORDS = {
    "raid", "raids", "raidday", "day", "event", "unlock", "ultra",
    "shadow", "mega", "community", "festival", "battle",
    "research", "spotlight", "pass", "bonus", "debut",
    "shiny", "local", "time", "weekend", "boost",
}


def extract_pokemon_names_from_text(text: str) -> List[str]:
    """
    Extract likely Pokémon names.

    Rules:
    - token length >= 4
    - not stopword
    - not numeric
    - not in NON_POKEMON_WORDS
    """
    toks = tokens(text)
    candidates = []

    for t in toks:
        if len(t) < 4:
            continue
        if t in NON_POKEMON_WORDS:
            continue
        candidates.append(t)

    return list(set(candidates))



def extract_official_pokemon_names(meta: Dict[str, Any]) -> List[str]:
    """
    Pull Pokémon names from:
    - article title
    - article description
    - article slug
    """
    names: List[str] = []

    names += extract_pokemon_names_from_text(meta.get("title", ""))
    names += extract_pokemon_names_from_text(meta.get("description", ""))
    names += slug_keywords(meta.get("url", ""))

    # De-dupe
    return list(set(names))


def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"https?://\S+", " ", s)
    s = s.replace("#", " ")
    s = s.replace("’", "'")
    s = s.replace("é", "e").replace("É", "E")  # Pokémon → pokemon, not "pok mon"
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def tokens(s: str) -> List[str]:
    s = normalize_text(s)
    out: List[str] = []
    for t in s.split():
        if len(t) <= 2:
            continue
        if t in STOPWORDS:
            continue
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
    m = re.search(r"/news/([^/?#]+)", url or "")
    if not m:
        return []
    slug = m.group(1).replace("-", " ")
    return tokens(slug)


def extract_official_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(https?://(?:www\.)?(?:pokemongo\.com|pokemongolive\.com)/news/[^\s\"']+)", text)
    if m:
        return m.group(1).split("?")[0].split("#")[0]
    return None


def find_official_slug_match(text: str, metas: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], str]]:
    """G47IX often links official pokemongo.com pages that aren't /news/ articles
    (e.g. pokemongo.com/en/gofest/megafinale). A long path segment embedded in a
    candidate article's slug (megafinale ⊂ gofest2026-mega-finale-incoming) is
    near-certain evidence. Returns (meta, matched_segment) for the longest hit."""
    if not text:
        return None

    hits: Dict[int, Tuple[int, Dict[str, Any], str]] = {}
    for m in re.finditer(r"pokemongo(?:live)?\.com(/[A-Za-z0-9/_-]+)", text, re.I):
        for seg in m.group(1).split("/"):
            seg_c = re.sub(r"[-_]", "", seg.lower())
            if len(seg_c) < 6 or seg_c == "news":
                continue
            for meta in metas:
                sm = re.search(r"/news/([^/?#]+)", meta.get("url", "") or "")
                if not sm:
                    continue
                slug_c = re.sub(r"[-_]", "", sm.group(1).lower())
                if seg_c in slug_c or slug_c in seg_c:
                    key = id(meta)
                    if key not in hits or len(seg_c) > hits[key][0]:
                        hits[key] = (len(seg_c), meta, seg)
    if not hits:
        return None
    # longest matched segment wins (most specific evidence)
    _, meta, seg = max(hits.values(), key=lambda h: h[0])
    return meta, seg


def clean_fb_phrase(post: Dict[str, Any]) -> str:
    """
    RSS.app titles often look like:
      'Lunar New Year in Pokémon GO #PokemonGO 🐉 Increased chance ...'
    We want:
      'Lunar New Year in Pokémon GO'
    """
    raw = ((post.get("title") or "") + " " + (post.get("description") or "")).strip()

    cut_markers = [
        " Increased ", " increased ",
        "If you're lucky", "if you're lucky",
        "👉", "->", "→", "|", "•", "—",
    ]
    best = raw
    for m in cut_markers:
        idx = best.find(m)
        if idx != -1:
            best = best[:idx].strip()

    best = re.sub(r"#\w+", " ", best).strip()
    best = re.sub(r"\s+", " ", best).strip()
    return best


def combined_match_score(fb_clean: str, fb_full: str, off_meta: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    off_title = off_meta.get("title", "")
    off_desc = off_meta.get("description", "")
    off_url = off_meta.get("url", "")

    fb_toks = tokens(fb_clean)
    off_body = off_meta.get("body", "")
    off_toks = tokens(off_title + " " + off_desc + " " + off_body)

    tok_score = jaccard(fb_toks, off_toks)
    sim = SequenceMatcher(None, normalize_text(fb_clean), normalize_text(off_title)).ratio()

    slug_toks = slug_keywords(off_url)
    slug_score = jaccard(tokens(fb_clean), slug_toks)

    # Base weighted score
    score = (0.45 * tok_score) + (0.40 * sim) + (0.15 * slug_score)
    fb_set = set(fb_toks)
    off_set = set(off_toks)

    # ------------------------------------------------------------
    # Core topic alignment boost
    # ------------------------------------------------------------

    mega_cluster = {"mega", "raid", "raids", "evolution", "shield", "shields", "level", "charges"}

    fb_mega_hits = sum(1 for t in mega_cluster if t in fb_set)
    off_mega_hits = sum(1 for t in mega_cluster if t in off_set)

    # Strong positive if both clearly talk about Mega mechanics
    if fb_mega_hits >= 2 and off_mega_hits >= 2:
        score += 0.30

    # Strong penalty if FB is Mega-focused but official barely mentions it
    if fb_mega_hits >= 2 and off_mega_hits == 0:
        score -= 0.35

    # ------------------------------------------------------------
    # Strong context boosts (Mega specific)
    # ------------------------------------------------------------
    if "mega" in fb_set and "mega" in off_set:
        score += 0.20

    if "raid" in fb_set and "raid" in off_set:
        score += 0.08

    if "evolution" in fb_set and "evolution" in off_set:
        score += 0.08
        
    # ------------------------------------------------------------
    # Community Day strong alignment boost
    # ------------------------------------------------------------

    community_cluster = {"community", "day"}

    fb_comm_hits = sum(1 for t in community_cluster if t in fb_set)
    off_comm_hits = sum(1 for t in community_cluster if t in off_set)

    if fb_comm_hits >= 2 and off_comm_hits >= 2:
        score += 0.20

    # ------------------------------------------------------------
    # Keyword overlap bonus
    # ------------------------------------------------------------
    for kw in KEYWORD_BONUS:
        if kw in fb_set and kw in off_set:
            score += 0.06

    # ------------------------------------------------------------
    # Pokémon name logic (prevents Lilligant -> Kyurem)
    # ------------------------------------------------------------
    fb_pokemon = extract_pokemon_names_from_text(fb_clean)
    off_pokemon = extract_official_pokemon_names(off_meta)

    matched_pokemon = set(fb_pokemon) & set(off_pokemon)

    if fb_pokemon and off_pokemon:
        if matched_pokemon:
            score += min(0.30, 0.15 * len(matched_pokemon))
        else:
            score -= 0.20  # strong negative if Pokémon differ

    # ------------------------------------------------------------
    # Strong long-event-name boost (EUIC fix)
    # ------------------------------------------------------------
    fb_norm = normalize_text(fb_clean)
    off_norm = normalize_text(off_title)

    # If long overlapping phrase exists, strong boost
    if len(fb_norm) > 30:
        if fb_norm[:40] in off_norm or off_norm[:40] in fb_norm:
            score += 0.15

    # ------------------------------------------------------------
    # Same year boost (helps annual events)
    # ------------------------------------------------------------
    fb_year = re.search(r"\b(20\d{2})\b", fb_norm)
    off_year = re.search(r"\b(20\d{2})\b", off_norm)

    if fb_year and off_year and fb_year.group(1) == off_year.group(1):
        score += 0.05

    # ------------------------------------------------------------
    # Phrase bonus
    # ------------------------------------------------------------
    fb_norm_full = normalize_text(fb_full)
    off_norm_full = normalize_text(off_title + " " + off_desc)

    for phrase in PHRASE_BONUS:
        if phrase in fb_norm_full and phrase in off_norm_full:
            score += 0.08

    # ------------------------------------------------------------
    # Recency boost (favor newest articles)
    # ------------------------------------------------------------
    if off_meta.get("published"):
        try:
            off_date = datetime.strptime(off_meta["published"], "%Y-%m-%d")
            days_old = (datetime.utcnow() - off_date).days
            if days_old <= 2:
                score += 0.05
        except Exception:
            pass

    score = max(0.0, min(score, 1.0))

    return score, {
        "tok": tok_score,
        "sim": sim,
        "slug": slug_score,
        "fb_clean": fb_clean,
        "fb_full_norm": fb_norm_full[:200],
        "off_title_norm": off_norm[:200],
    }


def debug_print_top_matches(fb_post: Dict[str, Any], official_metas: List[Dict[str, Any]], top_n: int = 3) -> None:
    fb_clean = clean_fb_phrase(fb_post)
    fb_full = f"{fb_post.get('title','')} {fb_post.get('description','')}".strip()

    scored: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
    for meta in official_metas:
        s, dbg = combined_match_score(fb_clean, fb_full, meta)
        scored.append((s, meta, dbg))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max(1, top_n)]

    print(f"[MATCH-DEBUG] Top {len(top)} candidates for fb_clean='{fb_clean}':")
    for rank, (s, meta, dbg) in enumerate(top, start=1):
        print(
            f"  #{rank} score={s:.2f} | tok={dbg['tok']:.2f} sim={dbg['sim']:.2f} slug={dbg['slug']:.2f} "
            f"| OFFICIAL='{meta.get('title','')}' | {meta.get('url','')}"
        )


# ============================================================
# OCR fallback (optional / disabled by default)
# ============================================================

def ocr_extract_text_from_image_url(image_url: str) -> Optional[str]:
    """
    Optional OCR fallback.
    - Disabled unless ENABLE_OCR_FALLBACK=1
    - Attempts pytesseract if installed, otherwise returns None.

    To enable Tesseract OCR in GitHub Actions, you'd need to:
      - apt-get install tesseract-ocr (Linux runner)
      - pip install pytesseract pillow
      - set ENABLE_OCR_FALLBACK=1
    """
    if not ENABLE_OCR_FALLBACK:
        return None
    if not image_url:
        return None

    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        print("[OCR] ENABLE_OCR_FALLBACK=1 but pytesseract/Pillow not installed. Skipping OCR.")
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (DiscordWebhookBot; +https://github.com)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(image_url, headers=headers, timeout=30)
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))
        text = pytesseract.image_to_string(img) or ""
        text = text.strip()

        if not text:
            return None

        # clamp size
        text = re.sub(r"\s+", " ", text)
        return text[:OCR_MAX_CHARS]
    except Exception as ex:
        print(f"[OCR] Failed to OCR image. Error: {ex}")
        return None


def article_fully_claimed(url: Optional[str], state: Dict[str, Any]) -> bool:
    """True if this article already has its infographic in every configured
    forum — matching a graphic to it is a dead end (post would be skipped),
    so such articles shouldn't compete for new graphics."""
    if not url or not DISCORD_FORUM_CHANNEL_IDS:
        return False
    channels = state.get("threads", {}).get(url, {}).get("channels", {})
    return all(
        fid in channels and channels[fid].get("infographic_posted")
        for fid in DISCORD_FORUM_CHANNEL_IDS
    )


def match_fb_to_official(fb_post: Dict[str, Any],official_metas: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], float, Dict[str, Any]]]:

    # ------------------------------------------------------------
    # 1) Direct official URL in FB content
    # ------------------------------------------------------------
    direct = (
        extract_official_url_from_text(fb_post.get("title", "")) or
        extract_official_url_from_text(fb_post.get("description", ""))
    )

    if direct:
        for meta in official_metas:
            if meta.get("url") == direct:
                return meta, 1.0, {"reason": "direct_url"}

        try:
            meta = parse_article_metadata(direct)
            return meta, 1.0, {"reason": "direct_url_fetched"}
        except Exception:
            pass

    fb_clean = clean_fb_phrase(fb_post)
    fb_full = f"{fb_post.get('title','')} {fb_post.get('description','')}".strip()

    # ------------------------------------------------------------
    # 1b) Official non-/news/ link whose path segment embeds in a slug
    #     (e.g. pokemongo.com/en/gofest/megafinale)
    # ------------------------------------------------------------
    slug_hit = find_official_slug_match(fb_full, official_metas)
    if slug_hit:
        meta, seg = slug_hit
        s, dbg = combined_match_score(fb_clean, fb_full, meta)
        dbg["reason"] = "official_link_slug"
        dbg["matched_segment"] = seg
        return meta, max(s, MATCH_THRESHOLD), dbg

    # ------------------------------------------------------------
    # 2) Topic Gating (Prevents cross-topic bleed)
    # ------------------------------------------------------------

    fb_tokens = set(tokens(fb_clean))

    mega_cluster = {"mega", "evolution", "shield", "shields", "charges", "level"}

    fb_mega_hits = sum(1 for t in mega_cluster if t in fb_tokens)

    filtered_metas = official_metas

    # If FB is clearly Mega-focused, restrict candidates
    if fb_mega_hits >= 2:
        temp = []
        for meta in official_metas:
            off_tokens = set(tokens(meta.get("title","") + " " + meta.get("body","")))
            off_mega_hits = sum(1 for t in mega_cluster if t in off_tokens)

            if off_mega_hits >= 2:
                temp.append(meta)

        # Only apply filtering if we found reasonable candidates
        if temp:
            filtered_metas = temp

    # ------------------------------------------------------------
    # 3) Normal scoring
    # ------------------------------------------------------------

    best_meta = None
    best_score = 0.0
    best_debug: Optional[Dict[str, Any]] = None

    for meta in filtered_metas:
        s, dbg = combined_match_score(fb_clean, fb_full, meta)

        # Prefer newer articles when scores are very close
        if best_meta and abs(s - best_score) < 0.03:
            if meta.get("published") and best_meta.get("published"):
                if meta["published"] > best_meta["published"]:
                    best_score = s
                    best_meta = meta
                    best_debug = dbg
                    continue

        if s > best_score:
            best_score = s
            best_meta = meta
            best_debug = dbg

    if best_meta and best_score >= MATCH_THRESHOLD:
        best_debug = best_debug or {}
        best_debug["reason"] = "scored"
        return best_meta, best_score, best_debug

    # ------------------------------------------------------------
    # 4) OCR fallback (unchanged)
    # ------------------------------------------------------------

    ocr_text = ocr_extract_text_from_image_url(fb_post.get("image_url", ""))

    if ocr_text:
        # G47IX prints the source article URL in the infographic footer
        # (e.g. "pokemongo.com/news/nationaltrust-2026") — a guaranteed match.
        # Only trust it if the slug matches a known candidate (OCR can garble).
        m = re.search(r"pokemongo(?:live)?\.com/news/([A-Za-z0-9_-]{4,})", ocr_text, re.I)
        if m:
            slug = m.group(1).lower().rstrip("-_")
            for meta in official_metas:
                if meta.get("url", "").rstrip("/").lower().endswith("/" + slug):
                    return meta, 1.0, {
                        "reason": "ocr_direct_url",
                        "ocr_slug": slug,
                        "fb_clean": fb_clean,
                    }

        # Non-/news/ official link in the image text (same evidence as 1b)
        slug_hit = find_official_slug_match(ocr_text, official_metas)
        if slug_hit:
            meta, seg = slug_hit
            s, dbg = combined_match_score(fb_clean, fb_full, meta)
            dbg["reason"] = "ocr_link_slug"
            dbg["matched_segment"] = seg
            return meta, max(s, MATCH_THRESHOLD), dbg

        fb_full_ocr = f"{fb_full} {ocr_text}".strip()
        fb_clean_ocr = clean_fb_phrase({"title": fb_clean, "description": ocr_text})
        ocr_token_set = set(tokens(fb_full_ocr))
        for alias, expansion in OCR_TOKEN_ALIASES.items():
            if alias in ocr_token_set:
                ocr_token_set |= expansion

        # Accept an OCR match on a high score alone, OR a normal score backed by
        # the official title's distinctive words appearing in the image text.
        # (Plain thresholds can't separate these: diffuse body-text overlap can
        # outscore a genuine title match.)
        best_acc: Optional[Tuple[float, Dict[str, Any], Dict[str, Any], float]] = None
        best_rej: Optional[Tuple[float, Dict[str, Any], float]] = None

        for meta in filtered_metas:
            s, dbg = combined_match_score(fb_clean_ocr, fb_full_ocr, meta)

            # Only distinctive title words count as evidence, and we need at
            # least two of them in the image text.
            distinct_toks = set(tokens(meta.get("title", ""))) - GENERIC_TITLE_TOKENS
            matched_toks = distinct_toks & ocr_token_set
            overlap = (len(matched_toks) / len(distinct_toks)) if distinct_toks else 0.0

            acceptable = s >= OCR_MATCH_THRESHOLD or (
                s >= MATCH_THRESHOLD
                and len(matched_toks) >= 2
                and overlap >= OCR_TITLE_OVERLAP
            )
            if acceptable:
                if best_acc is None or s > best_acc[0]:
                    best_acc = (s, meta, dbg, overlap)
            else:
                if best_rej is None or s > best_rej[0]:
                    best_rej = (s, meta, overlap)

        if best_acc:
            s, meta, dbg, overlap = best_acc
            dbg = dbg or {}
            dbg["reason"] = "ocr_scored"
            dbg["title_overlap"] = round(overlap, 2)
            dbg["ocr_excerpt"] = ocr_text[:250]
            return meta, s, dbg

        if best_rej:
            s, meta, overlap = best_rej
            print(
                f"[FB] OCR best candidate rejected (score={s:.2f}, "
                f"title_overlap={overlap:.2f}): '{meta.get('title','')}'"
            )

    # ------------------------------------------------------------
    # Debug output
    # ------------------------------------------------------------

    if DEBUG_MATCH_TOP_N > 0:
        debug_print_top_matches(fb_post, official_metas, top_n=DEBUG_MATCH_TOP_N)

    return None


# ============================================================
# Main
# ============================================================

def main() -> None:
    state = load_state()

    official_urls = get_latest_news_links(OFFICIAL_CANDIDATES_LIMIT)

    # Build official metadata cache (once)
    official_metas: List[Dict[str, Any]] = []
    for u in official_urls:
        try:
            official_metas.append(parse_article_metadata(u))
        except Exception as ex:
            print(f"[WARN] Failed to parse official meta for {u}: {ex}")

    fb_posts = get_facebook_posts()

    # Bootstrap: first run should not spam the channel
    if not state.get("bootstrapped", False):
        print("[BOOTSTRAP] First run detected. Recording latest items as seen (no posting).")
        state["seen_urls"] = list(dict.fromkeys(official_urls))[:OFFICIAL_CANDIDATES_LIMIT]
        state["seen_fb_posts"] = [p["link"] for p in fb_posts if p.get("link")][:30]
        state["posted_infographics"] = []
        state["bootstrapped"] = True
        save_state(state)
        print("[BOOTSTRAP] Done. Next run will post only truly-new items.")
        return

    seen_official = set(state.get("seen_urls", []))
    seen_fb = set(state.get("seen_fb_posts", []))

    # -----------------------------
    # Part A: post NEW official posts (bounded)
    # -----------------------------
    new_official = [u for u in official_urls if u not in seen_official]
    if not new_official:
        print("No new official posts.")
    else:
        # oldest first, bounded
        new_official = list(reversed(new_official))[:MAX_OFFICIAL_POSTS_PER_RUN]
        for url in new_official:
            meta = next((m for m in official_metas if m.get("url") == url), None) or parse_article_metadata(url)
            print(f"[OFFICIAL] Posting: {meta['title']} -> {url}")
            post_official(meta, state)
            state["seen_urls"] = (state["seen_urls"] + [url])[-800:]

    # -----------------------------
    # Part B: FB infographics -> only post if matched (bounded)
    # -----------------------------
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
    new_fb = list(reversed(new_fb))[:MAX_FB_POSTS_PER_RUN]

    for fb_post in new_fb:
        fb_link = fb_post.get("link")
        fb_title = fb_post.get("title", "")
        print(f"[FB] Candidate: {fb_title} -> {fb_link}")

        fb_probe = f"{fb_title} {fb_post.get('description','')}".lower()
        skip_pat = next((p for p in FB_SKIP_PATTERNS if p in fb_probe), None)
        if skip_pat:
            print(f"[FB] Skipping series graphic (matches skip pattern '{skip_pat}').")
            state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            continue

        # Only articles that still need an infographic somewhere may compete
        open_metas = [
            m for m in official_metas
            if not article_fully_claimed(m.get("url"), state)
        ]

        match = match_fb_to_official(fb_post, open_metas)
        if not match:
            print(f"[FB] No official match found (threshold={MATCH_THRESHOLD:.2f}). Skipping.")
            if not DEBUG_KEEP_UNMATCHED_FB:
                state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
            else:
                print("[FB] DEBUG_KEEP_UNMATCHED_FB=1, leaving FB post un-seen for retry/tuning.")
            continue

        official_meta, score, dbg = match
        official_url = official_meta.get("url")

        print(
            f"[FB] Matched! score={score:.2f} reason={dbg.get('reason')} "
            f"| OFFICIAL='{official_meta.get('title')}' | fb_clean='{dbg.get('fb_clean','')}'"
        )

        # Ensure official posted first (so infographic shows “under it”)
        if official_url not in set(state.get("seen_urls", [])):
            try:
                print(f"[FB] Official not seen yet; posting official first: {official_url}")
                post_official(official_meta, state)
                state["seen_urls"] = (state["seen_urls"] + [official_url])[-800:]
            except Exception as ex:
                print(f"[FB] Failed to post official; skipping infographic. Error: {ex}")
                state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]
                continue

        # Post infographic
        try:
            print(f"[FB] Posting infographic under official: {official_url}")
            post_infographic(official_meta, fb_post, state)
        except Exception as ex:
            print(f"[FB] Failed to post infographic. Error: {ex}")

        state["seen_fb_posts"] = (state["seen_fb_posts"] + [fb_link])[-800:]

    save_state(state)


if __name__ == "__main__":
    main()
