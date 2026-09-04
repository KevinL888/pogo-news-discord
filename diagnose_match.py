"""Replay the FB-graphic -> official-article match for one mirror message.

Matching failures are hard to reason about after the fact: the run log shows a
score but not the OCR text it came from, and by the next run the post is marked
seen and never retried. This replays a single message through the real matcher
and prints every intermediate the gate depends on.

    python diagnose_match.py [MESSAGE_ID] [EXPECTED_SLUG]

Needs DISCORD_BOT_TOKEN, G47IX_MIRROR_CHANNEL_ID, ENABLE_OCR_FALLBACK=1 and
tesseract on PATH. Posts nothing — read-only.
"""

import os
import sys
from typing import Any, Dict, List

import poster
from poster import (
    GENERIC_TITLE_TOKENS,
    MATCH_THRESHOLD,
    OCR_MATCH_THRESHOLD,
    OCR_TITLE_OVERLAP,
    OCR_TOKEN_ALIASES,
    OCR_UNIQUE_TOKEN_FLOOR,
    clean_fb_phrase,
    combined_match_score,
    discord_api,
    get_latest_news_links,
    match_fb_to_official,
    mirror_guild_id,
    mirror_message_to_item,
    ocr_extract_text_from_image_url,
    parse_article_metadata,
    tokens,
)

# The Gible Community Day Classic graphic, rejected on 2026-08-31 at score=0.32
DEFAULT_MESSAGE_ID = "1544034826376454177"
DEFAULT_EXPECTED_SLUG = "communitydayclassic-gible-september-2026"


def fetch_item(message_id: str) -> Dict[str, Any]:
    """Pull one message by id, even if it has aged out of the last-30 window."""
    channel_id = poster.G47IX_MIRROR_CHANNEL_ID
    msgs = discord_api(
        "GET", f"/channels/{channel_id}/messages?around={message_id}&limit=3"
    )
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if msg is None:
        raise SystemExit(f"Message {message_id} not found in channel {channel_id}")
    return mirror_message_to_item(msg, mirror_guild_id())


def main() -> None:
    message_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MESSAGE_ID
    expected_slug = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_EXPECTED_SLUG

    if not poster.ENABLE_OCR_FALLBACK:
        print("!! ENABLE_OCR_FALLBACK is not 1 — the OCR path will be skipped.\n")

    item = fetch_item(message_id)
    print("=" * 72)
    print("MIRROR MESSAGE")
    print("=" * 72)
    print(f"  link      = {item['link']}")
    print(f"  title     = {item['title']!r}")
    print(f"  image_url = {(item['image_url'] or '')[:100]}")

    fb_clean = clean_fb_phrase(item)
    fb_full = f"{item.get('title','')} {item.get('description','')}".strip()
    print(f"  fb_clean  = {fb_clean!r}")
    print(f"  fb_full   = {fb_full!r}")
    if not fb_clean:
        print("  -> caption-less: tok/sim/slug all score ~0, OCR decides alone")

    print()
    print("=" * 72)
    print("OCR TEXT")
    print("=" * 72)
    ocr_text = ocr_extract_text_from_image_url(item.get("image_url") or "") or ""
    if not ocr_text:
        raise SystemExit("No OCR text extracted — is tesseract installed?")
    print(f"  {len(ocr_text)} chars (clamped at OCR_MAX_CHARS={poster.OCR_MAX_CHARS})")
    print(f"  {ocr_text}")

    # Mirror what the matcher builds, so the numbers below are the real ones.
    fb_full_ocr = f"{fb_full} {ocr_text}".strip()
    fb_clean_ocr = clean_fb_phrase({"title": fb_clean, "description": ocr_text})
    if len(fb_clean_ocr) < len(fb_full_ocr):
        print()
        print(f"  !! clean_fb_phrase truncated the OCR text for scoring: "
              f"{len(fb_full_ocr)} -> {len(fb_clean_ocr)} chars")
        print(f"  !! scored text = {fb_clean_ocr[:200]!r}")

    ocr_token_set = set(tokens(fb_full_ocr))
    for alias, expansion in OCR_TOKEN_ALIASES.items():
        if alias in ocr_token_set:
            ocr_token_set |= expansion

    print()
    print("=" * 72)
    print("CANDIDATE GATE MATH")
    print("=" * 72)
    metas: List[Dict[str, Any]] = []
    for url in get_latest_news_links():
        try:
            metas.append(parse_article_metadata(url))
        except Exception as ex:
            print(f"  [WARN] {url}: {ex}")

    distinct_by_meta = {
        id(m): set(tokens(m.get("title", ""))) - GENERIC_TITLE_TOKENS for m in metas
    }
    token_df: Dict[str, int] = {}
    for meta_toks in distinct_by_meta.values():
        for t in meta_toks:
            token_df[t] = token_df.get(t, 0) + 1

    rows = []
    for meta in metas:
        s, _ = combined_match_score(fb_clean_ocr, fb_full_ocr, meta)
        distinct_toks = distinct_by_meta[id(meta)]
        matched = distinct_toks & ocr_token_set
        overlap = (len(matched) / len(distinct_toks)) if distinct_toks else 0.0
        unique_hits = {t for t in matched if token_df.get(t) == 1}

        legs = {
            "high_score": s >= OCR_MATCH_THRESHOLD,
            "two_tokens": (s >= MATCH_THRESHOLD and len(matched) >= 2
                           and overlap >= OCR_TITLE_OVERLAP),
            "unique_token": bool(unique_hits and len(distinct_toks) >= 2
                                 and overlap >= OCR_TITLE_OVERLAP
                                 and s >= OCR_UNIQUE_TOKEN_FLOOR),
        }
        rows.append((s, meta, distinct_toks, matched, overlap, unique_hits, legs))

    rows.sort(key=lambda r: r[0], reverse=True)
    shown = [r for r in rows if r[3] or expected_slug in (r[1].get("url") or "")]

    for s, meta, distinct_toks, matched, overlap, unique_hits, legs in shown[:12]:
        star = " <-- EXPECTED" if expected_slug in (meta.get("url") or "") else ""
        passing = [k for k, v in legs.items() if v]
        print(f"\n  score={s:.2f} overlap={overlap:.2f} "
              f"{'ACCEPT via ' + ','.join(passing) if passing else 'REJECT'}{star}")
        print(f"    {meta.get('title','')}")
        print(f"    distinctive={sorted(distinct_toks)}")
        print(f"    matched={sorted(matched)} unique={sorted(unique_hits)}")

    print()
    print("=" * 72)
    print("VERDICT (real match_fb_to_official)")
    print("=" * 72)
    result = match_fb_to_official(item, metas)
    if not result:
        print(f"  NO MATCH (threshold={MATCH_THRESHOLD:.2f})")
        raise SystemExit(1)

    meta, score, dbg = result
    ok = expected_slug in (meta.get("url") or "")
    print(f"  {'CORRECT' if ok else 'WRONG ARTICLE'}: {meta.get('title','')}")
    print(f"  url    = {meta.get('url')}")
    print(f"  score  = {score:.2f}  reason = {dbg.get('reason')}")
    if dbg.get("unique_hits"):
        print(f"  unique = {dbg['unique_hits']}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
