#!/usr/bin/env python3
"""
Archive the existing week's data BEFORE the new weekly build overwrites it, so
the dashboard preserves a rolling history across weeks (filterable by week).

Reads dashboard/data.json. If its top-level `generated` is from a prior week,
copies the current top-level snapshot (posts, soundChart, trends, tiktokSounds,
generatedHooks) into `data.json["weeks"][<that week>]`. Then RESETS the top-level
slices so the next weekly build can write fresh data without leaking last week's.

Capped at MAX_WEEKS most-recent (default 12) to keep data.json under ~3MB.

Idempotent: if `generated` already matches today (i.e. we already archived) or
no posts exist, it's a no-op.

Usage: python3 accumulate.py        # run as the FIRST step of each Monday's job
"""
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
MAX_WEEKS = int(os.environ.get("MAX_WEEKS_KEPT", "12"))


def main():
    data = json.loads((DASH / "data.json").read_text())
    prior_week = data.get("generated")
    if not prior_week or not data.get("posts"):
        print("No prior week to archive — no-op.")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if prior_week == today:
        print(f"data.json already generated today ({today}) — no-op.")
        return

    weeks = data.get("weeks") or {}
    weeks[prior_week] = {
        "posts": data.get("posts") or [],
        "soundChart": data.get("soundChart") or [],
        "trends": data.get("trends") or [],
        "tiktokSounds": data.get("tiktokSounds") or [],
        "generatedHooks": data.get("generatedHooks") or [],
    }
    # cap to most-recent MAX_WEEKS (ISO date strings sort lexicographically)
    keep = sorted(weeks.keys(), reverse=True)[:MAX_WEEKS]
    dropped = [w for w in weeks if w not in keep]
    data["weeks"] = {k: weeks[k] for k in keep}

    # Clear the top-level slices so the next build writes a clean current-week dataset.
    # `provenance`, `pools`, `videos` carry over unchanged (these get rewritten anyway).
    for k in ("posts", "soundChart", "trends", "tiktokSounds", "generatedHooks"):
        data[k] = []

    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    summary = "  ".join(f"{k}={len(v) if isinstance(v,list) else '?'}" for k, v in weeks[prior_week].items())
    print(f"Archived week {prior_week}: {summary}")
    print(f"Total weeks retained: {len(keep)}" + (f" (dropped {dropped})" if dropped else ""))


if __name__ == "__main__":
    main()
