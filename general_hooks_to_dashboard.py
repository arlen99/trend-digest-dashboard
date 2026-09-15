#!/usr/bin/env python3
"""
Inject general_hooks.py's niche-free hook templates into dashboard/data.json under a
SEPARATE key (`generalHookTrends`) from the niche lane's `hookTrends`.

Two keys, not one, on purpose. The existing Hooks table renders an "In our niche"
column sourced from hook_search.py's niche_signal() — a travel/filmmaking regex. A
general row has no meaningful value for that column: everything in a general corpus is
"out of niche" by construction, so merging the two lanes into `hookTrends` would render
a column of zeroes and quietly imply the general hooks all FAILED a check they were
never subject to. Keeping them separate also means a failure or an empty week in
either lane cannot blank the other.

Carries the same empty-run carryover guard every other lane uses: an empty result is
"this run found nothing", which is NOT the same as "there is nothing", and must never
silently erase a good week's committed data.

Usage: python3 general_hooks_to_dashboard.py [--top 25]
Pure transform, no network.
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
    except Exception:  # noqa: BLE001 — a truncated/empty file is "no data", not a crash
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    raw = latest("general_hook_trends_*.json")
    data = json.loads((DASH / "data.json").read_text())

    if not raw:
        kept = data.get("generalHookTrends") or []
        print(f"No general hook templates in this run's output — keeping {len(kept)} "
              f"committed row(s) rather than blanking the section.")
        return

    rows = []
    for h in raw:
        hook = (h.get("hook") or "").strip()
        if not hook:
            continue
        rows.append({
            "hook": hook,
            "creators": h.get("distinct_creators", 0),
            "uses": h.get("uses", 0),
            "templateMatches": h.get("templateMatches", 0),
            "stableReads": h.get("stableReads", 0),
            "results": h.get("results", 0),
            "maxLikes": h.get("maxLikes", 0),
            "medianLikes": h.get("medianLikes", 0),
            "totalViews": h.get("totalViews", 0),
            "verifiedBy": h.get("verifiedBy", ""),
            "confirmed": h.get("confirmed", False),
            "examples": (h.get("examples") or [])[:4],
        })

    rows = rows[: args.top]
    data["generalHookTrends"] = rows
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {len(rows)} general hook templates into dashboard/data.json (generalHookTrends).")


if __name__ == "__main__":
    main()
