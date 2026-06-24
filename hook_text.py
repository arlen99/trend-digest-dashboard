#!/usr/bin/env python3
"""
Harvest the ON-SCREEN HOOK TEXT from each reel's first few seconds, for trend
detection (Phase 2 — hook-anchored trends, independent of audio).

The hook is burned into the video pixels, not in any API field, so we grab 1–2
early frames with ffmpeg and OCR them. OCR is FREE + on-device via macOS Vision
(tools/ocr); set OCR_CMD to a Tesseract wrapper to run portably in the cloud.

Per post: pull its video_url (from the scrape row, else TikHub fetch_post_by_url) →
frame at ~2s and ~4s → OCR both → keep the denser, hook-shaped result. Cached by
shortcode in output/hook_texts.json so re-runs are free.

Usage:
  set -a && . ./.env && set +a
  python3 hook_text.py output/top_posts_<date>.json [--limit 150]
Env: TIKHUB_TOKEN (only for posts lacking a video url). OCR_CMD overrides the engine.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
CACHE = OUT / "hook_texts.json"
OCR_CMD = os.environ.get("OCR_CMD", str(ROOT / "tools" / "ocr"))  # swap for tesseract in cloud
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")


def shortcode(url):
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else (url or "")[-16:]


def video_url_for(row):
    if row.get("video"):
        return row["video"]
    if not KEY or not row.get("url"):
        return ""
    u = f"https://api.tikhub.io/api/v1/instagram/v1/fetch_post_by_url?post_url={urllib.parse.quote(row['url'])}"
    req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (json.loads(r.read().decode()).get("data", {}) or {}).get("video_url", "")
    except Exception:  # noqa: BLE001
        return ""


def frame(video_url, t, dest):
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", video_url, "-frames:v", "1", "-q:v", "3", dest],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    return os.path.exists(dest) and os.path.getsize(dest) > 800


def ocr(path):
    try:
        out = subprocess.run([OCR_CMD, path], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    # drop UI cruft (handles, follow buttons, counts) — keep hook-shaped lines
    lines = []
    for ln in out.split("\n"):
        s = ln.strip().strip('"').strip()
        if len(s) < 3 or s.startswith("@") or re.fullmatch(r"[\d.,KMviews\s]+", s, re.I):
            continue
        lines.append(s)
    return " ".join(lines)[:160]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("posts")
    ap.add_argument("--limit", type=int, default=150)
    args = ap.parse_args()
    rows = json.loads(Path(args.posts).read_text())
    rows = [r for r in rows if (r.get("format") in ("Reel", "TikTok") or r.get("video"))][:args.limit]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    tmp_a, tmp_b = "/tmp/hk_a.jpg", "/tmp/hk_b.jpg"
    new = got = 0
    for r in rows:
        code = shortcode(r.get("url", ""))
        if code in cache:
            continue
        vurl = video_url_for(r)
        if not vurl:
            cache[code] = {"hook": "", "reason": "no video"}; continue
        texts = []
        for t, dest in ((2.0, tmp_a), (4.0, tmp_b)):
            if frame(vurl, t, dest):
                texts.append(ocr(dest))
        hook = max(texts, key=len) if texts else ""
        cache[code] = {"hook": hook, "account": r.get("account", ""), "url": r.get("url", "")}
        new += 1
        if hook:
            got += 1
        time.sleep(0.1)
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"OCR'd {new} new reels, {got} had readable hook text "
          f"({sum(1 for v in cache.values() if v.get('hook'))} total cached) -> output/hook_texts.json")


if __name__ == "__main__":
    main()
