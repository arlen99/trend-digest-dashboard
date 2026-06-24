#!/usr/bin/env python3
"""
Download cover thumbnails for the week's top posts so a Claude session can
*look* at them and assign the Visual Style tags (grade, drone/aerial, film
grain) that captions alone can't reveal.

Source of the image, in order of preference:
  1. the "thumbnail" (displayUrl) field if scrape.py captured it (free, no request)
  2. else the post page's public og:image meta tag (logged-out, no Apify cost)

Usage:
  python3 fetch_thumbs.py [output/top_posts_<date>.json]   # defaults to newest
Writes output/thumbs/NN_account.jpg and prints an index the session can read.

Note: a still frame reveals COLOR GRADE, aerial/drone perspective, and film
grain reliably. It cannot reveal motion-based styles (fast cuts, transitions,
one-continuous-shot) — those still need the video. Tag accordingly.
"""
import glob
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
THUMBS = OUT / "thumbs"
THUMBS.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15")
OG = re.compile(r'<meta property="og:image" content="([^"]+)"')


def og_image(post_url: str) -> str:
    req = urllib.request.Request(post_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "ignore")
    m = OG.search(html)
    return m.group(1).replace("&amp;", "&") if m else ""


def download(img_url: str, dest: Path) -> bool:
    req = urllib.request.Request(img_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        dest.write_bytes(r.read())
    return dest.stat().st_size > 1000


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else max(
        glob.glob(str(OUT / "top_posts_*.json")), default="")
    if not path:
        sys.exit("No top_posts json found. Run scrape.py first.")
    rows = json.loads(Path(path).read_text())
    print(f"Fetching {len(rows)} thumbnails from {Path(path).name}\n")
    for i, r in enumerate(rows, 1):
        acct = re.sub(r"[^a-z0-9_.]", "", r["account"].lower())
        dest = THUMBS / f"{i:02d}_{acct}.jpg"
        try:
            img = r.get("thumbnail") or og_image(r["url"])
            if img and download(img, dest):
                print(f"  {i:02d} @{r['account']:<22} -> {dest.name}")
            else:
                print(f"  {i:02d} @{r['account']:<22} -> NO IMAGE ({r['url']})")
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"  {i:02d} @{r['account']:<22} -> ERROR {e}")
    print(f"\nSaved to {THUMBS}/ — a Claude session can now Read these and tag Visual Style.")


if __name__ == "__main__":
    main()
