#!/usr/bin/env python3
"""
Filter the latest scrape outputs (top_posts_*.json) to exclude any post URL ever
curated in a prior week. Guarantees the same post isn't shortlisted twice across
weeks — the curator only sees genuinely fresh candidates.

Reads dashboard/data.json's `weeks` archive + current top-level `posts` to build
the set of all-time-curated URLs. Then for each top_posts_<today>.json file in
output/, writes a sibling top_posts_<today>_fresh.json with prior URLs removed.

Run AFTER scrape.py / tiktok_scrape.py and BEFORE the curation/judgment step.

Usage: python3 scrape_dedupe.py
"""
import glob
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
OUT = ROOT / "output"


def prior_urls():
    """Every post URL ever curated, across the archive + current top-level."""
    if not (DASH / "data.json").exists():
        return set()
    data = json.loads((DASH / "data.json").read_text())
    urls = {p["url"] for p in (data.get("posts") or []) if p.get("url")}
    for week, slice_ in (data.get("weeks") or {}).items():
        for p in (slice_.get("posts") or []):
            if p.get("url"):
                urls.add(p["url"])
    return urls


def dedupe_file(path: Path, prior: set):
    rows = json.loads(path.read_text())
    fresh = [r for r in rows if r.get("url") not in prior]
    out = path.with_name(path.stem + "_fresh.json")
    out.write_text(json.dumps(fresh, indent=2))
    excluded = len(rows) - len(fresh)
    print(f"  {path.name}: {len(rows)} → {len(fresh)} fresh ({excluded} prior-week dupes removed) → {out.name}")
    return fresh


def main():
    prior = prior_urls()
    if not prior:
        print("No prior-week URLs to filter against — no-op (probably the first run).")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    targets = [OUT / f"top_posts_{today}.json", OUT / f"top_posts_tiktok_{today}.json"]
    for f in targets:
        if f.exists():
            dedupe_file(f, prior)
        else:
            print(f"  {f.name}: not found, skipping")
    print(f"Prior-week curated URLs in registry: {len(prior)}")


if __name__ == "__main__":
    main()
