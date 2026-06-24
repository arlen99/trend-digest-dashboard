#!/usr/bin/env python3
"""
Keyword-post lane — catch TRENDING posts directly, independent of the account list.

The account funnel only ever sees what the seed creators posted. This lane queries
TikTok by niche KEYWORD (recent + popular, via fetch_video_search_result's sort +
publish_time), so a breakout reel from a creator you don't track can still surface.

Ranking is built for *trend-catching, size-neutral* (per the brief — we WANT small
accounts):
  trendScore = engagement rate = (likes+comments+shares) / views
…which measures resonance, not reach, so it favours breakouts over big-account
baseline traffic. Guarded by a RECENCY window (current, not an old hit) and a VIEW
FLOOR (genuinely distributed, not a flukey ratio on 200 views). No per-author
baseline fetch needed.

Each post is tagged: source ("keyword:<kw>") and isNewAccount (author NOT in the
seed list) → on the dashboard these drop into the same grid with a "⚡ new find" chip.

Usage:
  set -a && . ./.env && set +a
  python3 keyword_posts.py --pilot      # 3 keywords
  python3 keyword_posts.py
Env: TIKHUB_TOKEN. Knobs: KW_RECENCY_DAYS, KW_VIEW_FLOOR, KW_PER_KEYWORD.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tiktok_scrape import normalize  # reuse the aweme→row mapping

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
KEYWORDS = ["cinematic travel", "travel film", "cinematic photography",
            "travel filmmaker", "cinematic b roll", "travel cinematography"]
RECENCY_DAYS = int(os.environ.get("KW_RECENCY_DAYS", "21"))
VIEW_FLOOR = int(os.environ.get("KW_VIEW_FLOOR", "20000"))
PER_KEYWORD = int(os.environ.get("KW_PER_KEYWORD", "20"))
EXCLUDE = re.compile(r"giveaway|crypto|forex|onlyfans|promo code|dropship|weight ?loss|\bnsfw\b|"
                     r"link in bio to (buy|shop)|telegram", re.I)


def th(path):
    if not KEY:
        sys.exit("TIKHUB_TOKEN not set.")
    for _ in range(4):
        try:
            req = urllib.request.Request("https://api.tikhub.io" + path,
                                         headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 — TikHub 502s transiently
            last = str(e)[:80]; time.sleep(1.5)
    return {"_error": last}


def seed_handles():
    s = set()
    f = ROOT / "tiktok_accounts.json"
    if f.exists():
        d = json.loads(f.read_text())
        s |= {a.lower() for a in d.get("accounts", [])}
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--keywords")
    args = ap.parse_args()
    kws = [k.strip() for k in args.keywords.split(",")] if args.keywords else KEYWORDS
    if args.pilot:
        kws = kws[:3]
    seeds = seed_handles()
    now = datetime.now(timezone.utc)

    rows, seen, calls = {}, set(), 0
    for kw in kws:
        q = urllib.parse.quote(kw)
        d = th(f"/api/v1/tiktok/app/v3/fetch_video_search_result?keyword={q}&count={PER_KEYWORD}&sort_type=1&publish_time=30")
        calls += 1
        items = (d.get("data") or {}).get("search_item_list") or []
        kept = 0
        for it in items:
            aw = it.get("aweme_info") or it.get("item") or it
            n = normalize(aw)
            if not n or n["url"] in seen:
                continue
            seen.add(n["url"])
            if EXCLUDE.search(n["caption"]):
                continue
            views = n["views"] or 0
            if views < VIEW_FLOOR:
                continue
            try:
                age = (now - datetime.fromisoformat(n["timestamp"])).total_seconds() / 86400 if n["timestamp"] else 999
            except ValueError:
                age = 999
            if age > RECENCY_DAYS:
                continue
            eng = n["likes"] + n["comments"] + n.get("shares", 0)
            n["trendScore"] = round(eng / views * 100, 1)        # engagement rate %
            n["velocity"] = int(views / max(age, 0.5))            # views/day, for display
            n["ageDays"] = round(age, 1)
            n["source"] = f"keyword:{kw}"
            n["isNewAccount"] = n["account"].lower() not in seeds
            rows[n["url"]] = n
            kept += 1
        print(f"  '{kw}': {kept} kept (recent ≤{RECENCY_DAYS}d, ≥{VIEW_FLOOR:,} views)")
        time.sleep(0.3)

    ranked = sorted(rows.values(), key=lambda r: (r["trendScore"], r["views"]), reverse=True)
    new = sum(1 for r in ranked if r["isNewAccount"])
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"keyword_posts_{stamp}.json").write_text(json.dumps(ranked, indent=2))
    lines = [f"# Keyword-post lane (trending) — {stamp}",
             f"_{len(ranked)} posts · {new} from NEW accounts (off-seed) · ranked by engagement rate · {calls} calls_\n",
             "| # | Creator | New? | ER | Views | Views/day | Age | Hook |", "|--|--|--|--|--|--|--|--|"]
    for i, r in enumerate(ranked, 1):
        lines.append(f"| {i} | @{r['account']} | {'⚡ new' if r['isNewAccount'] else 'seed'} | {r['trendScore']}% | "
                     f"{r['views']:,} | {r['velocity']:,} | {r['ageDays']}d | {r['caption'][:42].replace(chr(10),' ').replace('|','/')} |")
    (OUT / f"keyword_posts_{stamp}.md").write_text("\n".join(lines))
    print(f"\nWrote output/keyword_posts_{stamp}.json/.md — {len(ranked)} trending posts "
          f"({new} from off-seed accounts), {calls} calls (~${calls*0.001:.3f})")


if __name__ == "__main__":
    main()
