#!/usr/bin/env python3
"""
Trend Digest scraper (Instagram) — now on TikHub.

Pulls recent posts for every account in accounts.json via TikHub's IG API, then
ranks by OUTLIER SCORE (a post's engagement vs. that account's own median — what's
*over-performing*, the trend signal, not just whoever has the most followers).

One TikHub `fetch_user_posts` call per account returns metrics + video URL +
thumbnail + music + carousel together, so there are no separate enrichment fetches.
Replaces the old Apify actor (the Apify version is kept dormant in git history).

Outputs (unchanged schema, so the rest of the pipeline still works):
  output/top_posts_<date>.json / .md

Requires TIKHUB_TOKEN in .env. Knobs: POSTS_PER_ACCOUNT, DAYS_BACK, TOP_N.
Verification: metrics are Instagram's own counts via TikHub; spot-check a couple
of rows against the live posts before trusting the digest.
"""
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
BASE = "https://api.tikhub.io"
POSTS_PER_ACCOUNT = int(os.environ.get("POSTS_PER_ACCOUNT", "8"))
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))
TOP_N = int(os.environ.get("TOP_N", "50"))
ID_CACHE = OUT / "user_ids.json"


def die(msg): print(f"ERROR: {msg}", file=sys.stderr); sys.exit(1)


def th(path):
    if not KEY:
        die("TIKHUB_TOKEN not set. `set -a && . ./.env && set +a` first.")
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:90]}


def load_accounts():
    data = json.loads((ROOT / "accounts.json").read_text())
    return [a.strip().lstrip("@") for a in data["accounts"] if a.strip()]


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


def first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    return None


def resolve_ids(accounts):
    cache = json.loads(ID_CACHE.read_text()) if ID_CACHE.exists() else {}
    out, new = {}, 0
    for u in accounts:
        if cache.get(u):
            out[u] = cache[u]; continue
        d = th(f"/api/v1/instagram/v1/fetch_user_info_by_username?username={urllib.parse.quote(u)}")
        pk = None
        if isinstance(d, dict):
            for v in [deep(d, "data", "user", "pk"), deep(d, "data", "pk"), deep(d, "data", "id")]:
                if v and str(v).isdigit():
                    pk = str(v); break
        if pk:
            cache[u] = pk; out[u] = pk; new += 1
        time.sleep(0.2)
    ID_CACHE.write_text(json.dumps(cache, indent=2))
    print(f"  user IDs: {len(out)}/{len(accounts)} ({new} new, {len(out)-new} cached)")
    return out


def normalize(media, fallback_acct):
    code = media.get("code")
    if not code:
        return None
    mt, pt = media.get("media_type"), media.get("product_type")
    fmt = ("Carousel" if (mt == 8 or media.get("carousel_media"))
           else "Reel" if (pt == "clips" or mt == 2) else "Photo")
    likes = media.get("like_count") or 0
    likes = max(likes, 0)
    comments = media.get("comment_count") or 0
    views = first(media, "play_count", "ig_play_count", "view_count", "fb_play_count") or 0
    ts = media.get("taken_at")
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""
    thumb = deep(media, "image_versions2", "candidates", 0, "url", default="")
    video = deep(media, "video_versions", 0, "url", default="")
    carousel = [deep(c, "image_versions2", "candidates", 0, "url", default="")
                for c in (media.get("carousel_media") or [])]
    carousel = [c for c in carousel if c]
    cm = media.get("clips_metadata") or {}
    mi = cm.get("music_info") or {}
    ai = mi.get("music_asset_info") or {}
    osi = cm.get("original_sound_info") or {}
    is_original = bool(osi) and not mi
    audio_song = ai.get("title") or (osi.get("original_audio_title") if osi else "") or ""
    audio_artist = ai.get("display_artist") or deep(osi, "ig_artist", "username", default="") or ""
    audio_id = ai.get("audio_cluster_id") or cm.get("music_canonical_id") or (osi.get("audio_asset_id") if osi else "")
    return {
        "account": deep(media, "user", "username", default=fallback_acct),
        "url": f"https://www.instagram.com/{'reel' if fmt == 'Reel' else 'p'}/{code}/",
        "thumbnail": thumb, "video": video, "carousel_urls": carousel,
        "format": fmt, "caption": (deep(media, "caption", "text", default="") or "").strip(),
        "timestamp": iso, "likes": likes, "comments": comments, "views": views,
        "music": audio_song, "audio_song": audio_song, "audio_artist": audio_artist,
        "audio_id": str(audio_id or ""), "audio_is_original": is_original,
        "audio_url": video,  # AudD extracts audio from the video URL
        "engagement": likes + comments,
    }


def fetch_account(pk, username):
    d = th(f"/api/v1/instagram/v1/fetch_user_posts?user_id={pk}&count={POSTS_PER_ACCOUNT}")
    items = deep(d, "data", "items", default=[]) or []
    out = []
    for it in items:
        media = it.get("media") or it
        n = normalize(media, username)
        if n:
            out.append(n)
    return out


def rank(rows):
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    fresh = []
    for r in rows:
        try:
            if r["timestamp"] and datetime.fromisoformat(r["timestamp"]) < cutoff:
                continue
        except ValueError:
            pass
        fresh.append(r)
    by_acct = {}
    for r in fresh:
        by_acct.setdefault(r["account"], []).append(r["engagement"])
    medians = {a: statistics.median(v) for a, v in by_acct.items() if v}
    for r in fresh:
        base = medians.get(r["account"], 0) or 1
        r["outlier_score"] = round(r["engagement"] / base, 2)
    fresh.sort(key=lambda r: (r["outlier_score"], r["engagement"]), reverse=True)
    return fresh[:TOP_N]


def write_outputs(rows):
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"top_posts_{stamp}.json").write_text(json.dumps(rows, indent=2))
    lines = [f"# Top posts — week of {stamp} (TikHub)",
             f"_Lookback {DAYS_BACK}d · {len(rows)} posts · ranked by outlier score_\n",
             "| # | Account | Fmt | Outlier | Views | Likes | Comments | Hook | URL |",
             "|--|--|--|--|--|--|--|--|--|"]
    for i, r in enumerate(rows, 1):
        hook = r["caption"].replace("\n", " ")[:60].replace("|", "/")
        lines.append(f"| {i} | @{r['account']} | {r['format']} | {r['outlier_score']}x | "
                     f"{r['views']:,} | {r['likes']:,} | {r['comments']:,} | {hook} | {r['url']} |")
    (OUT / f"top_posts_{stamp}.md").write_text("\n".join(lines))
    print(f"Wrote output/top_posts_{stamp}.json/.md ({len(rows)} rows)")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", type=int, default=0, help="limit to first N accounts")
    args = ap.parse_args()
    accounts = load_accounts()
    if args.pilot:
        accounts = accounts[:args.pilot]
    print(f"Scraping {len(accounts)} accounts via TikHub ({POSTS_PER_ACCOUNT} posts each)...")
    ids = resolve_ids(accounts)
    rows, calls = [], 0
    for u, pk in ids.items():
        rows.extend(fetch_account(pk, u)); calls += 1
        time.sleep(0.15)
    if not rows:
        die("No posts returned. Check token/balance and account handles.")
    ranked = rank(rows)
    write_outputs(ranked)
    print(f"Scraped {len(rows)} posts from {len(ids)} accounts ({calls} fetch calls). "
          "Next: review the .md, then build the digest.")


if __name__ == "__main__":
    main()
