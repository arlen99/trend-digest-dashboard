#!/usr/bin/env python3
"""
Merge output/discovery_candidates_*.json (untracked discover.py/bootstrap.py/
tiktok_discover.py candidates' best post each) into dashboard/data.json, tagged
lane:"discovered" + newFind:true — same "⚡ new find" treatment the keyword lane
already gets, so these surface on the Swipe File for review. Saving one triggers
the dashboard's existing saveWithConfirm() prompt ("add @account to your
watchlist?") — that is the ONLY path an account joins accounts.json /
tiktok_accounts.json now. This script never writes to either file.

Idempotent on lane=="discovered" (re-running drops prior rows first).

Usage: python3 discovery_to_dashboard.py
"""
import glob
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
THUMBS = DASH / "thumbs"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")


def download(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if len(data) < 600:
            return False
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    data = json.loads((DASH / "data.json").read_text())
    for p in data["posts"]:
        p.setdefault("platform", "instagram")
    data["posts"] = [p for p in data["posts"] if p.get("lane") != "discovered"]
    week = max((p.get("week", "") for p in data["posts"]), default="")

    files = sorted(glob.glob(str(ROOT / "output" / "discovery_candidates_*.json")))
    if not files:
        print("No output/discovery_candidates_*.json — run discovery_posts.py first.")
        return
    cands = json.loads(Path(files[-1]).read_text())
    THUMBS.mkdir(parents=True, exist_ok=True)

    rows, got = [], 0
    for i, t in enumerate(cands, 1):
        plat = t.get("platform", "instagram")
        rel = f"thumbs/disc_{i:02d}_{t['account']}.jpg"
        thumb = t.get("thumbnail") or ""
        if thumb and download(thumb, DASH / rel):
            thumb = rel; got += 1
        audio = t.get("audio_song") or "Original audio"
        if t.get("audio_artist") and t["audio_artist"].lower() not in audio.lower():
            audio = f"{audio} · {t['audio_artist']}"
        fmt = t.get("format") or ("TikTok" if plat == "tiktok" else "Reel")
        views = t.get("views") or 0
        eng = (t.get("likes", 0) or 0) + (t.get("comments", 0) or 0) + (t.get("shares", 0) or 0)
        trend_score = round(eng / views * 100, 1) if views else 0
        rows.append({
            "account": t["account"], "url": t["url"], "platform": plat, "lane": "discovered",
            "newFind": True, "source": t.get("discoverySource", ""),
            "trendScore": trend_score,
            "hook": (t.get("caption") or "")[:200] or "(no caption)", "format": fmt,
            "views": views, "likes": t.get("likes", 0), "comments": t.get("comments", 0),
            "shares": t.get("shares", 0), "engRate": None, "outlier": t.get("outlier_score"),
            "hookTypes": [], "triggers": [], "visualStyles": [],
            "audio": audio, "audioDetected": False, "notes": "",
            "week": week, "date": (t.get("timestamp") or "")[:10], "thumb": thumb,
            "video": t.get("video", ""), "carousel": t.get("carousel_urls", []),
        })
    data["posts"].extend(rows)
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Merged {len(rows)} discovery-candidate posts ({got} covers downloaded). "
          f"data.json now {len(data['posts'])} posts. Save one on the dashboard to "
          f"approve its account onto the watchlist.")


if __name__ == "__main__":
    main()
