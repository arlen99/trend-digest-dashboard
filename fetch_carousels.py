#!/usr/bin/env python3
"""
Download every image in each Carousel-format candidate's carousel_urls, so
carousel posts can be curated (and displayed) alongside Reels — previously
carousels were skipped entirely because nothing downloaded their images.

Mirrors fetch_thumbs.py's pattern but per-image, matching the naming convention
dashboard/data.json's `carousel` field already expects (carousels/<account>_NN.jpg),
which build_dashboard.py's copy_thumbs() already knows how to pull from
output/carousels/ into dashboard/carousels/ — no changes needed there.

Usage:
  python3 fetch_carousels.py [output/top_posts_<date>_fresh.json]  # defaults to newest top_posts_*_fresh.json, else top_posts_*.json
Writes output/carousels/<account>_NN.jpg (1-indexed per post) and prints an index.
"""
import glob
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
CAROUSELS = OUT / "carousels"
CAROUSELS.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15")


def download(img_url: str, dest: Path) -> bool:
    req = urllib.request.Request(img_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        dest.write_bytes(r.read())
    return dest.stat().st_size > 1000


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        fresh = glob.glob(str(OUT / "top_posts_*_fresh.json"))
        path = max(fresh, default="") or max(glob.glob(str(OUT / "top_posts_*.json")), default="")
    if not path:
        sys.exit("No top_posts json found. Run scrape.py first.")
    rows = json.loads(Path(path).read_text())
    carousels = [r for r in rows if r.get("format") == "Carousel" and r.get("carousel_urls")]
    print(f"Fetching images for {len(carousels)} carousel posts from {Path(path).name}\n")

    saved = 0
    for r in carousels:
        acct = re.sub(r"[^a-z0-9_.]", "", r["account"].lower())
        paths = []
        for n, img in enumerate(r["carousel_urls"], 1):
            dest = CAROUSELS / f"{acct}_{n:02d}.jpg"
            try:
                if download(img, dest):
                    paths.append(f"carousels/{dest.name}")
                    saved += 1
                else:
                    print(f"  @{r['account']:<22} img {n} -> NO IMAGE")
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"  @{r['account']:<22} img {n} -> ERROR {e}")
        r["carousel_paths"] = paths  # so curate_posts.py can pick these up directly
        print(f"  @{r['account']:<22} -> {len(paths)}/{len(r['carousel_urls'])} images saved")

    Path(path).write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"\nSaved {saved} images to {CAROUSELS}/ — carousel_paths written back into {Path(path).name}.")


if __name__ == "__main__":
    main()
