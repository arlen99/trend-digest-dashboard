#!/usr/bin/env python3
"""
Merge the keyword-post lane (output/keyword_posts_*.json) into the dashboard's
data.json so trending off-seed posts drop into the SAME grid, tagged "⚡ new find".

Idempotent on lane=="keyword" (re-running drops prior keyword rows first). Covers
download locally like the other lanes. These are TikTok posts (platform:"tiktok"),
so the platform toggle already filters them; the chip + the "⚡ New finds" Show
filter mark them as discovered-beyond-your-accounts.

Usage: python3 keyword_posts_to_dashboard.py [--top 12]
"""
import argparse
import glob
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
THUMBS = DASH / "thumbs"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")


def download(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if len(data) < 600:
            return False
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    data = json.loads((DASH / "data.json").read_text())
    for p in data["posts"]:
        p.setdefault("platform", "instagram")
    # remember each post's last-known-good LOCAL thumb (by url) — see
    # tiktok_to_dashboard.py for why this matters (signed CDN URLs expire in
    # ~2 weeks; without this fallback a failed re-download clobbers a good local
    # thumb with a dead remote one).
    prior_thumbs = {p["url"]: p["thumb"] for p in data["posts"]
                    if p.get("lane") == "keyword" and (p.get("thumb") or "").startswith("thumbs/")}
    # same for a previously Blob-hosted video — see tiktok_to_dashboard.py for why.
    prior_videos = {p["url"]: p["video"] for p in data["posts"]
                    if p.get("lane") == "keyword" and "blob.vercel-storage" in (p.get("video") or "")}
    # idempotent: drop prior keyword-lane rows only
    data["posts"] = [p for p in data["posts"] if p.get("lane") != "keyword"]
    week = max((p.get("week", "") for p in data["posts"]), default="")

    files = sorted(glob.glob(str(ROOT / "output" / "keyword_posts_*.json")))
    if not files:
        raise SystemExit("No output/keyword_posts_*.json — run keyword_posts.py first.")
    kw = json.loads(Path(files[-1]).read_text())[:args.top]
    THUMBS.mkdir(parents=True, exist_ok=True)

    rows, got, reused = [], 0, 0
    for i, t in enumerate(kw, 1):
        # keyed by video id (stable across weeks), not rank index i
        vid = (re.search(r"/video/(\d+)", t.get("url", "")) or [None, None])[1]
        rel = f"thumbs/kw_{t['account']}_{vid}.jpg" if vid else f"thumbs/kw_{i:02d}_{t['account']}.jpg"
        dest = DASH / rel
        thumb = t.get("thumbnail") or ""
        if dest.exists():
            thumb = rel; reused += 1
        elif t.get("thumbnail") and download(t["thumbnail"], dest):
            thumb = rel; got += 1
        elif prior_thumbs.get(t.get("url")):
            thumb = prior_thumbs[t["url"]]; reused += 1
        audio = t.get("audio_song") or "original sound"
        if t.get("audio_artist") and t["audio_artist"].lower() not in audio.lower():
            audio = f"{audio} · {t['audio_artist']}"
        rows.append({
            "account": t["account"], "url": t["url"], "platform": "tiktok", "lane": "keyword",
            "newFind": bool(t.get("isNewAccount")), "source": t.get("source", ""),
            "trendScore": t.get("trendScore"),
            "hook": t.get("caption", "") or "(no caption)", "format": "TikTok",
            "views": t.get("views", 0), "likes": t.get("likes", 0), "comments": t.get("comments", 0),
            "shares": t.get("shares", 0), "engRate": t.get("trendScore"), "outlier": None,
            "hookTypes": [], "triggers": [], "visualStyles": [],
            "audio": audio, "audioDetected": False, "notes": "",
            "week": week, "date": (t.get("timestamp") or "")[:10], "thumb": thumb,
            "video": prior_videos.get(t["url"], ""), "carousel": [],
        })
    data["posts"].extend(rows)
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    nf = sum(1 for r in rows if r["newFind"])
    print(f"Merged {len(rows)} keyword-lane posts ({nf} off-seed ⚡, {got} freshly downloaded, {reused} reused). "
          f"data.json now {len(data['posts'])} posts.")


if __name__ == "__main__":
    main()
