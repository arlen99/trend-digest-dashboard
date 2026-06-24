#!/usr/bin/env python3
"""
Merge the TikTok top posts (output/top_posts_tiktok_*.json) into the dashboard's
data.json so they show up behind the platform toggle.

- Tags every existing (Instagram) post with platform:"instagram".
- Takes the top N TikTok posts by outlier, downloads their covers into
  dashboard/thumbs/ (TikTok CDN needs a browser UA; falls back to the remote URL),
  maps them to the dashboard post shape with platform:"tiktok", and replaces any
  previously-merged TikTok rows.

Tag arrays (hookTypes/triggers/visualStyles) are left empty — those are curated by
hand in the weekly session; the toggle, metrics, audio and links all work without them.

Usage: python3 tiktok_to_dashboard.py [--top 12]
"""
import argparse
import glob
import json
import urllib.request
from datetime import datetime
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
    # drop prior account-lane tiktok rows only (preserve keyword-lane rows)
    data["posts"] = [p for p in data["posts"]
                     if not (p.get("platform") == "tiktok" and p.get("lane") != "keyword")]
    week = max(p.get("week", "") for p in data["posts"]) or datetime.now().strftime("%Y-%m-%d")

    files = sorted(glob.glob(str(ROOT / "output" / "top_posts_tiktok_*.json")))
    if not files:
        raise SystemExit("No output/top_posts_tiktok_*.json — run tiktok_scrape.py first.")
    tt = json.loads(Path(files[-1]).read_text())[:args.top]
    THUMBS.mkdir(parents=True, exist_ok=True)

    rows, got = [], 0
    for i, t in enumerate(tt, 1):
        rel = f"thumbs/tt_{i:02d}_{t['account']}.jpg"
        thumb = t.get("thumbnail") or ""
        if t.get("thumbnail") and download(t["thumbnail"], DASH / rel):
            thumb = rel; got += 1
        eng = (t.get("likes", 0) + t.get("comments", 0) + t.get("shares", 0))
        er = round(eng / t["views"] * 100, 1) if t.get("views") else None
        audio = t.get("audio_song") or "original sound"
        if t.get("audio_artist") and t["audio_artist"].lower() not in audio.lower():
            audio = f"{audio} · {t['audio_artist']}"
        date = (t.get("timestamp") or "")[:10]
        rows.append({
            "account": t["account"], "url": t["url"], "platform": "tiktok",
            "hook": t.get("caption", "") or "(no caption)", "format": "TikTok",
            "views": t.get("views", 0), "likes": t.get("likes", 0), "comments": t.get("comments", 0),
            "shares": t.get("shares", 0), "engRate": er, "outlier": t.get("outlier_score", 0),
            "hookTypes": [], "triggers": [], "visualStyles": [],
            "audio": audio, "audioDetected": False, "notes": "",
            "week": week, "date": date, "thumb": thumb, "video": "", "carousel": [],
        })
    data["posts"].extend(rows)
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    ig = sum(1 for p in data["posts"] if p.get("platform") == "instagram")
    print(f"Merged {len(rows)} TikTok posts ({got} covers downloaded locally) + {ig} Instagram. "
          f"data.json now {len(data['posts'])} posts.")


if __name__ == "__main__":
    main()
