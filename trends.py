#!/usr/bin/env python3
"""
Trend candidate generator — turn raw signals into TREND CANDIDATES for the digest.

Two independent detectors (a candidate is a TEMPLATE ≥N creators copy):
  • AUDIO-anchored  (Phase 1): sounds used by >= --min-creators niche creators,
    with week-over-week momentum (new / rising / steady) vs the previous scan.
  • HOOK-anchored   (Phase 2): on-screen hook lines (from hook_text.py) clustered
    by similarity, surfaced when >= --min-hook distinct creators use the same line —
    INDEPENDENT of audio (catches "What do you dream about?" across different tracks).

Output: output/trends_<date>.json — evidence packs (anchor, creators, sample reels,
momentum). The weekly Claude session then writes the human trend cards from these.

Usage: python3 trends.py [--min-creators 3] [--min-hook 2]
"""
import argparse
import difflib
import glob
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"


def latest_two(pat):
    fs = sorted(glob.glob(str(OUT / pat)), key=os.path.getmtime)
    return (fs[-1] if fs else None), (fs[-2] if len(fs) > 1 else None)


def norm(s):
    s = re.sub(r"[^\w\s]", "", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def audio_trends(min_creators):
    cur, prev = latest_two("audio_trends_*.json")
    chart = json.loads(Path(cur).read_text()) if cur else []
    prevmap = {}
    if prev:
        for t in json.loads(Path(prev).read_text()):
            prevmap[norm(t["title"]) + "|" + norm(t["artist"])] = t["niche_creators"]
    out = []
    for t in chart:
        if t["niche_creators"] < min_creators or not (t["title"] or t["artist"]):
            continue
        was = prevmap.get(norm(t["title"]) + "|" + norm(t["artist"]))
        momentum = "new" if was is None else ("rising" if t["niche_creators"] > was
                                              else "steady" if t["niche_creators"] == was else "cooling")
        out.append({
            "type": "audio", "anchor": f"{t['title']} — {t['artist']}".strip(" —"),
            "original": t.get("original", False), "creators": t["niche_creators"],
            "uses": t["niche_uses"], "accounts": t.get("accounts", [])[:6],
            "momentum": momentum, "prev_creators": was, "samples": t.get("samples", [])[:4],
        })
    out.sort(key=lambda c: (c["creators"], c["uses"]), reverse=True)
    return out


def hook_trends(min_hook):
    f = OUT / "hook_texts.json"
    if not f.exists():
        return []
    rows = [v for v in json.loads(f.read_text()).values() if v.get("hook") and len(norm(v["hook"])) >= 8]
    # greedy similarity clustering on normalized hook text
    clusters = []
    for r in rows:
        n = norm(r["hook"])
        placed = False
        for c in clusters:
            if difflib.SequenceMatcher(None, n, c["key"]).ratio() >= 0.8 or n in c["key"] or c["key"] in n:
                c["members"].append(r); placed = True; break
        if not placed:
            clusters.append({"key": n, "members": [r]})
    out = []
    for c in clusters:
        accts = {m.get("account", "") for m in c["members"] if m.get("account")}
        if len(accts) < min_hook:
            continue
        rep = max(c["members"], key=lambda m: len(m["hook"]))["hook"]
        out.append({
            "type": "hook", "anchor": rep, "creators": len(accts),
            "uses": len(c["members"]), "accounts": sorted(accts)[:6],
            "variants": sorted({m["hook"] for m in c["members"]})[:5],
            "samples": [m["url"] for m in c["members"] if m.get("url")][:4],
        })
    out.sort(key=lambda c: (c["creators"], c["uses"]), reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-creators", type=int, default=3)
    ap.add_argument("--min-hook", type=int, default=2)
    args = ap.parse_args()
    audio = audio_trends(args.min_creators)
    hooks = hook_trends(args.min_hook)
    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = {"generated": stamp, "audio": audio, "hook": hooks}
    (OUT / f"trends_{stamp}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Trend candidates -> output/trends_{stamp}.json")
    print(f"  AUDIO-anchored (>={args.min_creators} creators): {len(audio)}")
    for c in audio[:6]:
        print(f"    [{c['momentum']:>7}] {c['creators']}cr  {c['anchor'][:46]}")
    print(f"  HOOK-anchored  (>={args.min_hook} creators): {len(hooks)}")
    for c in hooks[:6]:
        print(f"    {c['creators']}cr  {c['anchor'][:60]!r}")


if __name__ == "__main__":
    main()
