#!/usr/bin/env python3
"""
Niche discovery via Instagram's RELATED-PROFILES graph.

Stage 1 of the funnel: for each seed account in accounts.json, ask Instagram for
its algorithmically "similar creators" (the relatedProfiles list), then rank
every candidate by HOW MANY different seeds point to it. An account related to
many seeds is central to the travel/cinematic niche; a one-off is likely noise.

This replaced the earlier hashtag approach, which returned only low-engagement
photos with no view counts and noisy accounts. Related-profiles is clean,
cheap (~1 result per seed), and compounds: each week's promoted accounts seed
next week's discovery.

Usage:
  set -a && . ./.env && set +a
  python3 discover.py                 # all seeds
  SEED_LIMIT=20 python3 discover.py   # cheap test on first 20 seeds

Env:
  APIFY_TOKEN   required
  SEED_LIMIT    optional cap on how many seeds to expand from
  MIN_SEEDS     default 2 — a candidate must be related to >= this many seeds
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
SEED_LIMIT = int(os.environ.get("SEED_LIMIT", "0")) or None
MIN_SEEDS = int(os.environ.get("MIN_SEEDS", "2"))
ACTOR = "apify~instagram-scraper"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def seeds() -> list[str]:
    data = json.loads((ROOT / "accounts.json").read_text())
    return [a.strip().lstrip("@") for a in data["accounts"] if a.strip()]


def run_apify(usernames: list[str]) -> list[dict]:
    if not APIFY_TOKEN:
        die("APIFY_TOKEN not set. `set -a && . ./.env && set +a` first.")
    url = (f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
           f"?token={APIFY_TOKEN}")
    payload = {
        "directUrls": [f"https://www.instagram.com/{u}/" for u in usernames],
        "resultsType": "details",
        "resultsLimit": 1,
        "addParentData": True,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    print(f"Expanding related-profiles for {len(usernames)} seeds...")
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    seed_list = seeds()
    if SEED_LIMIT:
        seed_list = seed_list[:SEED_LIMIT]
    seed_set = {s.lower() for s in seed_list}
    items = run_apify(seed_list)
    if not items:
        die("No profile details returned. Check token/credits.")

    # candidate -> set of seeds that list it as related, plus its follower count
    related_by_seed: dict[str, set[str]] = defaultdict(set)
    followers: dict[str, int] = {}
    seeds_with_data = 0
    for it in items:
        src = (it.get("username") or "").lower()
        rp = it.get("relatedProfiles") or []
        if rp:
            seeds_with_data += 1
        for r in rp:
            u = (r.get("username") or "").lower()
            if not u or u in seed_set:
                continue
            related_by_seed[u].add(src)
            if r.get("followersCount"):
                followers[u] = r["followersCount"]

    ranked = sorted(related_by_seed.items(),
                    key=lambda kv: len(kv[1]), reverse=True)
    ranked = [(u, srcs) for u, srcs in ranked if len(srcs) >= MIN_SEEDS]

    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = [{"account": u, "seed_overlap": len(srcs),
                "related_to": sorted(srcs),
                "followers": followers.get(u)} for u, srcs in ranked]
    (OUT / f"discovered_{stamp}.json").write_text(json.dumps(payload, indent=2))

    lines = [f"# Discovered candidate accounts — {stamp}",
             f"_{seeds_with_data}/{len(items)} seeds returned a similar-creators "
             f"list · {len(ranked)} candidates related to >= {MIN_SEEDS} seeds_\n",
             "| Candidate | # seeds related | Followers | Related to (sample) |",
             "|--|--|--|--|"]
    for u, srcs in ranked:
        f = followers.get(u)
        fc = f"{f:,}" if f else "?"
        lines.append(f"| @{u} | {len(srcs)} | {fc} | "
                     f"{', '.join('@'+s for s in sorted(srcs)[:5])} |")
    (OUT / f"discovered_{stamp}.md").write_text("\n".join(lines))
    print(f"Wrote output/discovered_{stamp}.json/.md — {len(ranked)} candidates "
          f"(related to >= {MIN_SEEDS} seeds).")
    print("Review the .md, promote the strong ones into accounts.json, then the "
          "weekly scrape.py picks them up with full engagement data.")


if __name__ == "__main__":
    main()
