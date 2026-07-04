#!/usr/bin/env python3
"""
Backfill on-screen text (videoText) for links saved via the "Save to Trend Digest"
iOS Shortcut (dashboard/api/save-link.js).

save-link.js already enriches each saved link with real metrics (views/likes/
comments/thumbnail/video/account/caption) at save time — that's a plain TikHub HTTP
call, so it runs fine in Vercel's Node function. OCR can't run there: no ffmpeg, no
Vision framework. Mirrors tiktok_videotext.py, which OCRs the post's already-fetched
cover thumbnail directly (no frame extraction needed) via on-device macOS Vision.

Note: `videoText` is the raw on-screen OCR read. `hook` (the short curated headline
used elsewhere in the dashboard) is hand-written during curation and intentionally
NOT generated here — same as the chips/outlier score, that's a manual judgment call,
not something this script fabricates. The card falls back to the post's real caption.

Usage:
  set -a && . ./.env && set +a
  python3 enrich_links.py
Env: EDIT_SECRET, DASHBOARD_URL (optional, defaults to the deployed dashboard).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

OCR_CMD = os.environ.get("OCR_CMD", str(__import__("pathlib").Path(__file__).parent / "tools" / "ocr"))
SECRET = os.environ.get("EDIT_SECRET")
BASE = os.environ.get("DASHBOARD_URL", "https://trend-digest-dashboard.vercel.app")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
TMP = "/tmp/lk_thumb.jpg"


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Edit-Secret": SECRET, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception:  # noqa: BLE001
        return False
    with open(dest, "wb") as f:
        f.write(data)
    return len(data) > 800


def ocr(path):
    try:
        out = subprocess.run([OCR_CMD, path], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for ln in out.split("\n"):
        s = ln.strip().strip('"').strip()
        if len(s) < 3 or s.startswith("@") or re.fullmatch(r"[\d.,KMviews\s]+", s, re.I):
            continue
        lines.append(s)
    return " ".join(lines)[:160]


def main():
    if not SECRET:
        sys.exit("EDIT_SECRET not set — run: set -a && . ./.env && set +a")

    links = api("/api/save-link").get("links", [])
    pending = [l for l in links if l.get("thumbnail") and "videoText" not in l]
    if not pending:
        print(f"{len(links)} saved links, nothing pending OCR.")
        return

    done = got = 0
    for l in pending:
        text = ocr(TMP) if download(l["thumbnail"], TMP) else ""
        api("/api/save-link", method="POST", body={"url": l["url"], "videoText": text})
        done += 1
        if text:
            got += 1
        print(f"  {'OK' if text else '--'} {l.get('account', '?')} {l['url']} -> {text[:58]!r}")
        time.sleep(0.2)

    print(f"OCR'd {done} pending link(s), {got} had readable on-screen text.")


if __name__ == "__main__":
    main()
