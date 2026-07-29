#!/usr/bin/env python3
"""
Niche discovery via Instagram's RELATED-PROFILES graph.

Stage 1 of the funnel: for each seed account in accounts.json, ask Instagram for
its algorithmically "similar creators" (the related-profiles list), then rank
every candidate by HOW MANY different seeds point to it. An account related to
many seeds is central to the travel/cinematic niche; a one-off is likely noise.

This replaced the earlier hashtag approach, which returned only low-engagement
photos with no view counts and noisy accounts. Related-profiles is clean,
cheap (~$0.001/seed), and compounds: each week's promoted accounts seed next
week's discovery.

Migrated 2026-07-29 from Apify (dead token, and its anonymous session only got
populated relatedProfiles for ~5% of seeds) to TikHub's fetch_related_profiles,
which uses the same authenticated session as the rest of the IG pipeline and
returns real data reliably (verified: 79 related profiles for a seed that
Apify returned 0 for, matching what a logged-in browser sees).

Usage:
  set -a && . ./.env && set +a
  python3 discover.py                 # all seeds
  SEED_LIMIT=20 python3 discover.py   # cheap test on first 20 seeds

Env:
  TIKHUB_TOKEN  required
  SEED_LIMIT    optional cap on how many seeds to expand from
  MIN_SEEDS     default 2 — a candidate must be related to >= this many seeds
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
BASE = "https://api.tikhub.io"
SEED_LIMIT = int(os.environ.get("SEED_LIMIT", "0")) or None
MIN_SEEDS = int(os.environ.get("MIN_SEEDS", "2"))
ID_CACHE = OUT / "user_ids.json"  # shared with scrape.py — same accounts, same ids
th_calls = 0


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def th(path):
    global th_calls
    if not KEY:
        die("TIKHUB_TOKEN not set. `set -a && . ./.env && set +a` first.")
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    th_calls += 1
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:90]}


def deep(d, *path, default=None):
    cur = d
    for p in path:
        if isinstance(p, int):
            cur = cur[p] if isinstance(cur, list) and len(cur) > p else None
        else:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return default
    return cur


def seeds() -> list[str]:
    data = json.loads((ROOT / "accounts.json").read_text())
    return [a.strip().lstrip("@") for a in data["accounts"] if a.strip()]


def user_info(username: str) -> dict:
    """fetch_user_info_by_username's response has been observed both as
    data.user.* and data.data.user.* (TikHub sometimes wraps it in an extra
    status/attempts retry envelope) — try both nesting depths."""
    d = th(f"/api/v1/instagram/v1/fetch_user_info_by_username?username={urllib.parse.quote(username)}")
    return deep(d, "data", "data", "user", default=None) or deep(d, "data", "user", default={}) or {}


def resolve_ids(usernames: list[str]) -> dict[str, str]:
    """username -> numeric user_id, via the same cache scrape.py maintains."""
    cache = json.loads(ID_CACHE.read_text()) if ID_CACHE.exists() else {}
    out, new = {}, 0
    for u in usernames:
        if cache.get(u):
            out[u] = cache[u]
            continue
        pk = user_info(u).get("id")
        if pk:
            cache[u] = str(pk)
            out[u] = str(pk)
            new += 1
        time.sleep(0.2)
    if new:
        ID_CACHE.write_text(json.dumps(cache, indent=2))
    return out


def related_profiles(user_id: str) -> list[dict]:
    d = th(f"/api/v1/instagram/v1/fetch_related_profiles?user_id={user_id}")
    edges = deep(d, "data", "data", "user", "edge_related_profiles", "edges", default=[]) or []
    return [e["node"] for e in edges if e.get("node")]


def follower_count(username: str) -> int | None:
    return deep(user_info(username), "edge_followed_by", "count")


def main() -> None:
    seed_list = seeds()
    if SEED_LIMIT:
        seed_list = seed_list[:SEED_LIMIT]
    seed_set = {s.lower() for s in seed_list}

    print(f"Resolving user_ids for {len(seed_list)} seeds...")
    ids = resolve_ids(seed_list)
    print(f"  {len(ids)}/{len(seed_list)} resolved (cached + fresh)")

    print(f"Expanding related-profiles for {len(ids)} seeds...")
    related_by_seed: dict[str, set[str]] = defaultdict(set)
    full_names: dict[str, str] = {}
    seeds_with_data = 0
    for u, uid in ids.items():
        nodes = related_profiles(uid)
        if nodes:
            seeds_with_data += 1
        for n in nodes:
            cand = (n.get("username") or "").lower()
            if not cand or cand in seed_set or n.get("is_private"):
                continue
            related_by_seed[cand].add(u)
            if n.get("full_name"):
                full_names[cand] = n["full_name"]
        time.sleep(0.2)

    ranked = sorted(related_by_seed.items(), key=lambda kv: len(kv[1]), reverse=True)
    ranked = [(u, srcs) for u, srcs in ranked if len(srcs) >= MIN_SEEDS]

    print(f"Fetching follower counts for {len(ranked)} surfaced candidates...")
    followers: dict[str, int] = {}
    for u, _ in ranked:
        fc = follower_count(u)
        if fc:
            followers[u] = fc
        time.sleep(0.2)

    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = [{"account": u, "seed_overlap": len(srcs), "related_to": sorted(srcs),
                "followers": followers.get(u), "full_name": full_names.get(u, "")}
               for u, srcs in ranked]
    (OUT / f"discovered_{stamp}.json").write_text(json.dumps(payload, indent=2))

    lines = [f"# Discovered candidate accounts — {stamp}",
             f"_{seeds_with_data}/{len(ids)} seeds returned a similar-creators "
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
    print("Next: python3 discovery_posts.py && python3 discovery_to_dashboard.py — surfaces "
          "each candidate's actual posts on the dashboard (⚡ new find). Nothing is added to "
          "accounts.json until you save one of those posts and confirm 'add to watchlist?' "
          "— no bulk/blind merge.")
    import cost_tracker
    cost_tracker.record("discover", tikhub_calls=th_calls)


if __name__ == "__main__":
    main()
