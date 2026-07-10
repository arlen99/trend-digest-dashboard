#!/usr/bin/env python3
"""
TikTok scraper — pull recent posts for every creator in tiktok_accounts.json via
TikHub and rank by OUTLIER SCORE (engagement vs. each creator's own median), same
metric as the IG scraper.

Output schema mirrors the IG top_posts (plus platform:"tiktok") so the dashboard
can merge both feeds behind a platform toggle.

Usage:
  set -a && . ./.env && set +a
  python3 tiktok_scrape.py --pilot 8
  python3 tiktok_scrape.py
Env: TIKHUB_TOKEN. Knobs: TT_POSTS_PER, DAYS_BACK, TT_TOP_N.
"""
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
POSTS_PER = int(os.environ.get("TT_POSTS_PER", "10"))
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))
TOP_N = int(os.environ.get("TT_TOP_N", "40"))


def die(m): print(f"ERROR: {m}", file=sys.stderr); sys.exit(1)


th_calls = 0


def th(path):
    global th_calls
    if not KEY:
        die("TIKHUB_TOKEN not set.")
    req = urllib.request.Request("https://api.tikhub.io" + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    th_calls += 1
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:90]}


def deep(d, *path, default=None):
    cur = d
    for p in path:
        if isinstance(p, int):
            cur = cur[p] if isinstance(cur, list) and len(cur) > p else None
        else:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return default
    return cur


def find_aweme_list(o):
    if isinstance(o, dict):
        for k in ("aweme_list", "itemList", "items", "videos"):
            if isinstance(o.get(k), list):
                return o[k]
        for v in o.values():
            r = find_aweme_list(v)
            if r is not None:
                return r
    return None


def normalize(a):
    aid = a.get("aweme_id") or a.get("id")
    uid = deep(a, "author", "unique_id") or deep(a, "author", "uniqueId")
    if not aid or not uid:
        return None
    st = a.get("statistics") or a.get("stats") or {}
    likes = st.get("digg_count") or st.get("diggCount") or 0
    comments = st.get("comment_count") or st.get("commentCount") or 0
    shares = st.get("share_count") or st.get("shareCount") or 0
    views = st.get("play_count") or st.get("playCount") or 0
    saves = st.get("collect_count") or st.get("collectCount") or 0
    ts = a.get("create_time") or a.get("createTime")
    iso = datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts else ""
    thumb = deep(a, "video", "cover", "url_list", 0) or deep(a, "video", "origin_cover", "url_list", 0) \
        or deep(a, "video", "cover", default="")
    video = deep(a, "video", "play_addr", "url_list", 0, default="")
    music_title = deep(a, "music", "title", default="")
    music_author = deep(a, "music", "author", default="")
    return {
        "platform": "tiktok", "account": uid,
        "url": f"https://www.tiktok.com/@{uid}/video/{aid}",
        "thumbnail": thumb, "video": video, "carousel_urls": [],
        "format": "TikTok", "caption": (a.get("desc") or "").strip(),
        "timestamp": iso, "likes": likes, "comments": comments, "shares": shares,
        "views": views, "saves": saves,
        "music": music_title, "audio_song": music_title, "audio_artist": music_author,
        "audio_id": str(deep(a, "music", "id", default="") or ""), "audio_is_original": bool(deep(a, "music", "is_original_sound")),
        "engagement": likes + comments + shares,
    }


def load_creators(pilot):
    f = ROOT / "tiktok_accounts.json"
    if not f.exists():
        die("tiktok_accounts.json not found — run tiktok_discover.py --write first.")
    data = json.loads(f.read_text())
    secs = data.get("sec_uids", {})
    pairs = [(u, secs.get(u)) for u in data["accounts"] if secs.get(u)]
    return pairs[:pilot] if pilot else pairs


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", type=int, default=0)
    args = ap.parse_args()
    creators = load_creators(args.pilot)
    print(f"Scraping {len(creators)} TikTok creators ({POSTS_PER} posts each)...")
    rows, calls = [], 0
    for uid, sec in creators:
        d = th(f"/api/v1/tiktok/web/fetch_user_post?secUid={sec}&count={POSTS_PER}")
        calls += 1
        for a in (find_aweme_list(d) or []):
            try:
                n = normalize(a)
            except Exception:  # noqa: BLE001 - one malformed post shouldn't kill the whole scrape
                continue
            if n:
                rows.append(n)
        time.sleep(0.2)
    if not rows:
        die("No TikTok posts returned. Check token/balance and sec_uids.")
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    fresh = []
    for r in rows:
        try:
            if r["timestamp"] and datetime.fromisoformat(r["timestamp"]) < cutoff:
                continue
        except ValueError:
            pass
        fresh.append(r)
    by = {}
    for r in fresh:
        by.setdefault(r["account"], []).append(r["engagement"])
    med = {a: statistics.median(v) for a, v in by.items() if v}
    for r in fresh:
        r["outlier_score"] = round(r["engagement"] / (med.get(r["account"], 0) or 1), 2)
    fresh.sort(key=lambda r: (r["outlier_score"], r["engagement"]), reverse=True)
    top = fresh[:TOP_N]
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"top_posts_tiktok_{stamp}.json").write_text(json.dumps(top, indent=2))
    lines = [f"# TikTok top posts — {stamp}",
             f"_{len(rows)} posts from {len(creators)} creators · top {len(top)} by outlier · {calls} calls_\n",
             "| # | Creator | Outlier | Views | Likes | Comments | Shares | Hook |", "|--|--|--|--|--|--|--|--|"]
    for i, r in enumerate(top, 1):
        lines.append(f"| {i} | @{r['account']} | {r['outlier_score']}x | {r['views']:,} | "
                     f"{r['likes']:,} | {r['comments']:,} | {r['shares']:,} | {r['caption'][:46].replace(chr(10),' ').replace('|','/')} |")
    (OUT / f"top_posts_tiktok_{stamp}.md").write_text("\n".join(lines))
    print(f"Wrote output/top_posts_tiktok_{stamp}.json/.md — {len(top)} ranked ({calls} calls)")
    import cost_tracker
    cost_tracker.record("tiktok_scrape", tikhub_calls=th_calls)


if __name__ == "__main__":
    main()
