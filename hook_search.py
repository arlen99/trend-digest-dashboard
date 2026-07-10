#!/usr/bin/env python3
"""
Validate hook trends via TikTok SEARCH (solves the copycat-recall gap).

TikTok's search indexes on-screen text, so searching a hook phrase returns everyone
using it. We OCR one instance from our scan (hook_text.py), then search each distinct
hook here to measure its REAL breadth — distinct creators + engagement + recency —
without having to scrape the copycats.

Per candidate hook: one TikHub TikTok search → score by distinct authors, top/median
likes, and how niche-relevant the results look. Hooks with broad, high-engagement,
niche-tinged results are confirmed trends.

Usage:
  set -a && . ./.env && set +a
  python3 hook_search.py [--max 40] [--min-authors 6]
Output: output/hook_trends_<date>.json (validated hook trends, ranked).
Env: TIKHUB_TOKEN.
"""
import argparse
import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
NICHE = re.compile(r"travel|cinematic|filmmak|adventure|drone|wander|explore|landscape|"
                   r"roadtrip|backpack|nomad|bali|iceland|dolomit|b-?roll|fpv", re.I)


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?']", "", (s or "").lower())).strip()


def candidates():
    f = OUT / "hook_texts.json"
    raw = [v["hook"] for v in json.loads(f.read_text()).values() if v.get("hook")] if f.exists() else []
    seen, out = set(), []
    for h in raw:
        n = norm(h)
        words = n.split()
        # keep hook-shaped phrases: 3–12 words, mostly letters, not a watermark/handle
        if not (3 <= len(words) <= 12) or len(n) < 12:
            continue
        if not n.isascii():  # drop OCR garble in other scripts
            continue
        realish = sum(1 for w in words if len(w) >= 2 and w.isalpha())
        if realish / len(words) < 0.7:  # mostly real words, not "F F FILM" garble
            continue
        if n in seen:
            continue
        seen.add(n); out.append(h.strip())
    return out


th_calls = 0


def search(phrase):
    global th_calls
    q = urllib.parse.quote(phrase)
    u = f"https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_video_search_result?keyword={q}&count=20&sort_type=0&publish_time=0"
    for _ in range(4):
        try:
            req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
            th_calls += 1
            with urllib.request.urlopen(req, timeout=60) as r:
                return (json.loads(r.read().decode()).get("data") or {}).get("search_item_list") or []
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--min-niche", type=int, default=2)
    args = ap.parse_args()
    if not KEY:
        raise SystemExit("TIKHUB_TOKEN not set.")
    cands = candidates()[:args.max]
    print(f"Validating {len(cands)} distinct candidate hooks via TikTok search...")
    trends = []
    for h in cands:
        items = [it.get("aweme_info") or {} for it in search(h)]
        if not items:
            continue
        authors = {(a.get("author") or {}).get("unique_id") for a in items if a.get("author")}
        likes = sorted((a.get("statistics") or {}).get("digg_count", 0) for a in items)
        niche = sum(1 for a in items if NICHE.search(a.get("desc", "") or ""))
        examples = [f"https://www.tiktok.com/@{(a.get('author') or {}).get('unique_id')}/video/{a.get('aweme_id')}"
                    for a in items[:3] if a.get("aweme_id")]
        trends.append({
            "hook": h, "results": len(items), "distinct_creators": len(authors),
            "max_likes": likes[-1] if likes else 0, "median_likes": likes[len(likes) // 2] if likes else 0,
            "niche_hits": niche, "examples": examples,
        })
        time.sleep(0.3)
    # TikTok returns ~20 results for ANY query, so creator-count doesn't discriminate.
    # The real signal is NICHE RELEVANCE of the results + engagement.
    confirmed = [t for t in trends if t["niche_hits"] >= args.min_niche and t["max_likes"] >= 10000]
    confirmed.sort(key=lambda t: (t["niche_hits"], t["max_likes"]), reverse=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"hook_trends_{stamp}.json").write_text(json.dumps(confirmed, indent=2, ensure_ascii=False))
    print(f"\n{len(confirmed)} confirmed hook trends -> output/hook_trends_{stamp}.json")
    for t in confirmed[:10]:
        print(f"  {t['niche_hits']:>2}niche · {t['distinct_creators']}cr · ♥{t['max_likes']:,} max  {t['hook'][:48]!r}")
    import cost_tracker
    cost_tracker.record("hook_search", tikhub_calls=th_calls)


if __name__ == "__main__":
    main()
