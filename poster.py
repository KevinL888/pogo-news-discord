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
DISCORD_FORUM_CHANNEL_ID = os.environ.get("DISCORD_FORUM_CHANNEL_ID")

DISCORD_API_BASE = "https://discord.com/api/v10"
FB_RSS_URL = clean_env_url(os.environ.get("G47IX_FB_RSS_URL"))

OFFICIAL_CANDIDATES_LIMIT = int(os.environ.get("OFFICIAL_CANDIDATES_LIMIT", "60"))
MAX_OFFICIAL_POSTS_PER_RUN = int(os.environ.get("MAX_OFFICIAL_POSTS_PER_RUN", "3"))
MAX_FB_POSTS_PER_RUN = int(os.environ.get("MAX_FB_POSTS_PER_RUN", "5"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.38"))
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

    return {
        "title": title,
        "description": description[:250],
        "image": image,
        "published": published_text,
        "url": article_url,
    }


# ============================================================
# Discord posting (429-safe)
# ============================================================

def post_official(meta: Dict[str, Any], state: Dict[str, Any]) -> None:
    # If thread already exists, do nothing
    if meta["url"] in state.get("threads", {}):
        return

    embed: Dict[str, Any] = {
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
        "message": {
            "embeds": [embed]
        }
    }

    data = discord_api(
        "POST",
        f"/channels/{DISCORD_FORUM_CHANNEL_ID}/threads",
        payload
    )

    thread_id = data["id"]

    state.setdefault("threads", {})
    state["threads"][meta["url"]] = {
        "thread_id": thread_id,
        "infographic_posted": False,
    }

    time.sleep(SLEEP_BETWEEN_POSTS_SEC)



def post_infographic(official_meta: Dict[str, Any], fb_post: Dict[str, Any], state: Dict[str, Any]) -> None:
    threads = state.get("threads", {})
    thread = threads.get(official_meta["url"])

    if not thread:
        print("[WARN] Tried to post infographic but thread does not exist.")
        return

    if thread.get("infographic_posted"):
        return

    embed: Dict[str, Any] = {
        "title": "Infographic (G47IX)",
        "description": (
            f"Matched to: **{official_meta.get('title','Pokémon GO News')}**\n"
            f"Source: {fb_post.get('link')}"
        ),
        "url": official_meta.get("url"),
    }

    if fb_post.get("image_url"):
        embed["image"] = {"url": fb_post["image_url"]}

    discord_api(
        "POST",
        f"/channels/{thread['thread_id']}/messages",
        {"embeds": [embed]}
    )

    thread["infographic_posted"] = True
    time.sleep(SLEEP_BETWEEN_POSTS_SEC)



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
# Facebook RSS parsing
# ============================================================

def get_facebook_posts() -> List[Dict[str, Any]]:
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
    off_toks = tokens(off_title + " " + off_desc)

    tok_score = jaccard(fb_toks, off_toks)
    sim = SequenceMatcher(None, normalize_text(fb_clean), normalize_text(off_title)).ratio()

    slug_toks = slug_keywords(off_url)
    slug_score = jaccard(tokens(fb_clean), slug_toks)

    # Base weighted score
    score = (0.55 * tok_score) + (0.35 * sim) + (0.10 * slug_score)

    fb_set = set(fb_toks)
    off_set = set(off_toks)

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
            score += min(0.25, 0.10 * len(matched_pokemon))
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


def match_fb_to_official(fb_post: Dict[str, Any], official_metas: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], float, Dict[str, Any]]]:
    # 1) direct official URL in FB content
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

    best_meta = None
    best_score = 0.0
    best_debug: Optional[Dict[str, Any]] = None

    for meta in official_metas:
        s, dbg = combined_match_score(fb_clean, fb_full, meta)
        if s > best_score:
            best_score = s
            best_meta = meta
            best_debug = dbg

    if best_meta and best_score >= MATCH_THRESHOLD:
        best_debug = best_debug or {}
        best_debug["reason"] = "scored"
        return best_meta, best_score, best_debug

    # 2) OCR fallback (only if enabled)
    ocr_text = ocr_extract_text_from_image_url(fb_post.get("image_url", ""))
    if ocr_text:
        # Re-score using OCR text as part of FB content
        fb_full_ocr = f"{fb_full} {ocr_text}".strip()
        fb_clean_ocr = clean_fb_phrase({"title": fb_clean, "description": ocr_text})

        best_meta2 = None
        best_score2 = 0.0
        best_debug2: Optional[Dict[str, Any]] = None

        for meta in official_metas:
            s, dbg = combined_match_score(fb_clean_ocr, fb_full_ocr, meta)
            if s > best_score2:
                best_score2 = s
                best_meta2 = meta
                best_debug2 = dbg

        if best_meta2 and best_score2 >= MATCH_THRESHOLD:
            best_debug2 = best_debug2 or {}
            best_debug2["reason"] = "ocr_scored"
            best_debug2["ocr_excerpt"] = ocr_text[:250]
            return best_meta2, best_score2, best_debug2

    # Debug output for tuning
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

    fb_posts = get_facebook_posts() if FB_RSS_URL else []

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
    new_fb = list(reversed(new_fb))[:MAX_FB_POSTS_PER_RUN]

    for fb_post in new_fb:
        fb_link = fb_post.get("link")
        fb_title = fb_post.get("title", "")
        print(f"[FB] Candidate: {fb_title} -> {fb_link}")

        match = match_fb_to_official(fb_post, official_metas)
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
