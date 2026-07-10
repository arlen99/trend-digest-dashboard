#!/usr/bin/env python3
"""
TikTok trending sounds — pull TikTok's live trending-sound chart and flag which
ones are ALSO showing up in the Instagram niche (the cross-platform early-trend
signal: a sound blowing up on TikTok that's starting to appear on IG).

Source: TikHub `/api/v1/tiktok/app/v3/fetch_music_chart_list` (the live chart;
note the older `get_sound_rank_list` is deprecated). Cross-references the latest
`output/audio_trends_<date>.json` (the IG niche audio chart) by normalized title.

Usage:
  set -a && . ./.env && set +a
  python3 tiktok_trends.py
Env: TIKHUB_TOKEN required.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
PAGES = int(os.environ.get("PAGES", "3"))   # ~ PAGES x chart page of sounds


th_calls = 0


def th(path):
    global th_calls
    if not KEY:
        sys.exit("TIKHUB_TOKEN not set.")
    req = urllib.request.Request("https://api.tikhub.io" + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    th_calls += 1
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def norm(s):
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", (s or "").lower())  # drop (feat..)/(remix)
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def ig_niche_titles():
    files = sorted(glob.glob(str(OUT / "audio_trends_*.json")))
    if not files:
        return {}
    rows = json.loads(Path(files[-1]).read_text())
    return {norm(r["title"]): r for r in rows if r.get("title")}


def main():
    ig = ig_niche_titles()
    sounds, cursor = [], 0
    for _ in range(PAGES):
        d = th(f"/api/v1/tiktok/app/v3/fetch_music_chart_list?cursor={cursor}&count=20")
        data = d.get("data") or {}
        for it in (data.get("music_list") or []):
            mi = it.get("music_info") or {}
            ms = mi.get("matched_song") or {}
            title = mi.get("title") or ms.get("title")
            artist = mi.get("author") or ms.get("author")
            if not title:
                continue
            hit = ig.get(norm(title))
            sounds.append({"title": title, "artist": artist, "music_id": str(mi.get("id") or mi.get("mid") or ""),
                           "use_count": mi.get("music_ugid_use_count"),
                           "also_in_ig_niche": bool(hit),
                           "ig_niche_creators": (hit or {}).get("niche_creators")})
        cursor = data.get("cursor") or cursor + 20
        if not data.get("has_more", True):
            break
        time.sleep(0.2)

    # dedupe by music_id, keep order (rank)
    seen, ranked = set(), []
    for s in sounds:
        if s["music_id"] in seen:
            continue
        seen.add(s["music_id"]); ranked.append(s)

    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"tiktok_trends_{stamp}.json").write_text(json.dumps(ranked, indent=2))
    cross = [s for s in ranked if s["also_in_ig_niche"]]
    lines = [f"# TikTok trending sounds — {stamp}",
             f"_{len(ranked)} trending sounds · {len(cross)} also appearing in your IG niche_\n",
             "| Rank | Sound | Artist | Also in IG niche? |", "|--|--|--|--|"]
    for i, s in enumerate(ranked, 1):
        flag = f"✓ ({s['ig_niche_creators']} creators)" if s["also_in_ig_niche"] else "—"
        lines.append(f"| {i} | {s['title']} | {s['artist'] or '—'} | {flag} |")
    (OUT / f"tiktok_trends_{stamp}.md").write_text("\n".join(lines))
    print(f"Wrote output/tiktok_trends_{stamp}.json/.md — {len(ranked)} sounds, {len(cross)} cross-platform with IG niche")
    import cost_tracker
    cost_tracker.record("tiktok_trends", tikhub_calls=th_calls)


if __name__ == "__main__":
    main()
