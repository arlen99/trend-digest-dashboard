#!/usr/bin/env python3
"""
Inject hook_search.py's TikTok-search-validated hook trends into dashboard/data.json
so the Hooks page can render them as a ranked table.

This is the RAW validated evidence — distinct creators, engagement, niche-match count
— straight from hook_search.py, with no Claude curation step in between. That's
deliberate: the Trend Radar's hook CARDS (curate_trends.py) are a separate, Claude-
written interpretation layer that can fail independently, and did — every hook card
silently 400'd from 2026-08-10 onward (TikTok's HEIC covers, see curate_trends.py),
leaving the board audio-only even though hook_search.py had validated 14 real hooks
that week. This section shows the underlying measurement regardless, so a failure in
the card-writing layer can't make a whole week's hook research invisible again.

Source (latest in output/): hook_trends_*.json
Writes one top-level key into dashboard/data.json: `hookTrends`.
Pure transform, no network.

Usage: python3 hooks_to_dashboard.py [--top 20]
"""
import argparse
import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"


def latest(pattern):
    fs = sorted(glob.glob(str(ROOT / "output" / pattern)), key=os.path.getmtime)
    if not fs:
        return []
    try:
        return json.loads(Path(fs[-1]).read_text()) or []
    except Exception:  # noqa: BLE001 - a truncated/empty file is "no data", not a crash
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    raw = latest("hook_trends_*.json")
    data = json.loads((DASH / "data.json").read_text())

    # Same carryover guard every other lane uses: hook_search.py legitimately writes an
    # EMPTY file when its TikHub searches can't run (confirmed 2026-08-17 — the account
    # balance was exhausted, so 0 of 40 candidates could be validated). Blindly writing
    # that through would silently erase a good week's data and look identical to "nothing
    # is trending". Keep what's committed instead, and let the next real run replace it.
    if not raw:
        kept = data.get("hookTrends") or []
        print(f"No validated hooks in this run's output — keeping {len(kept)} committed "
              f"hook trend(s) rather than blanking the section.")
        return

    rows = []
    for h in raw:
        hook = (h.get("hook") or "").strip()
        if not hook:
            continue
        rows.append({
            "hook": hook,
            "nicheHits": h.get("niche_hits", 0),
            "results": h.get("results", 0),
            "creators": h.get("distinct_creators", 0),
            "maxLikes": h.get("max_likes", 0),
            "medianLikes": h.get("median_likes", 0),
            "examples": (h.get("examples") or [])[:4],
        })

    # hook_search.py already ranks by its own score; keep that order but cap for display.
    rows = rows[:args.top]
    data["hookTrends"] = rows
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {len(rows)} validated hook trends into dashboard/data.json (hookTrends).")


if __name__ == "__main__":
    main()
