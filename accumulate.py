#!/usr/bin/env python3
"""
Maintain the rolling week-history that powers the dashboard's Week filter.

Run as the FIRST step of the deterministic weekly job (before scrape). It owns the
`weekOf` marker (ISO date of the current week's Monday) so week-tracking is reliable
EVEN IF the Claude judgment task never runs — it keys off the run date, not off any
field that only curation sets.

On a week rollover (run-date Monday differs from the stored `weekOf`):
  1. Snapshot the current top-level slices into `weeks[<old weekOf>]` (frozen copy).
  2. Cap `weeks` to the most-recent MAX_WEEKS.
  3. Stamp the new `weekOf`.
It does NOT wipe the top-level — curated IG posts + Claude-written trend cards carry
forward until they're refreshed (the bot refreshes TikTok/keyword/sound; Claude
refreshes IG curation + cards). So the live board never goes blank.

First run (no `weekOf` yet): just stamps `weekOf` = this Monday; real archiving begins
at the next rollover.

Usage: python3 accumulate.py
Env: MAX_WEEKS_KEPT (default 12).
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
MAX_WEEKS = int(os.environ.get("MAX_WEEKS_KEPT", "12"))
SLICES = ("posts", "soundChart", "trends", "tiktokSounds", "generatedHooks")


def main():
    f = DASH / "data.json"
    data = json.loads(f.read_text())

    today = date.today()
    this_monday = (today - timedelta(days=today.weekday())).isoformat()
    data["generated"] = today.isoformat()  # sidebar "Generated" stamp = latest run (was only set by curation before)
    prev = data.get("weekOf")

    if not prev:
        data["weekOf"] = this_monday
        print(f"Bootstrapped weekOf = {this_monday} (no archive on first run).")
    elif prev == this_monday:
        print(f"Still the same week ({this_monday}) — no rollover.")
    else:
        # ---- week rollover: snapshot the prior week, then advance the marker ----
        if data.get("posts"):
            weeks = data.get("weeks") or {}
            weeks[prev] = {k: data.get(k) or [] for k in SLICES}
            keep = sorted(weeks.keys(), reverse=True)[:MAX_WEEKS]
            dropped = [w for w in weeks if w not in keep]
            data["weeks"] = {k: weeks[k] for k in keep}
            sizes = "  ".join(f"{k}={len(weeks[prev][k])}" for k in SLICES)
            print(f"Archived week {prev}: {sizes} | retained {len(keep)}" + (f" (dropped {dropped})" if dropped else ""))
        else:
            print(f"Rollover {prev} → {this_monday}, but no posts to archive.")
        data["weekOf"] = this_monday
        # top-level slices intentionally NOT cleared — they carry forward and get refreshed in place.
        print(f"weekOf advanced → {this_monday}")

    f.write_text(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
