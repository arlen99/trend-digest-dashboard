#!/usr/bin/env python3
"""
Niche audio chart — rank the sounds the niche is using, CONSOLIDATED so the same
song counts once across its many Instagram audio IDs.

Source: TikHub (api.tikhub.io, pay-per-request, replaces HikerAPI — covers IG +
TikTok with one key). Each IG reel carries `music_canonical_id` (Instagram's own
"same song across all audio IDs" key), so consolidation is direct: group by
canonical_id, falling back to normalized title+artist.

Cost control:
  - username→IG-id cached in output/user_ids.json (IG ids are universal & never
    change — the cache built under HikerAPI carries over). Repeat runs skip lookups.
  - run MONTHLY, not weekly.

Usage:
  set -a && . ./.env && set +a
  python3 audio_trends.py --pilot 20
  python3 audio_trends.py
Env: TIKHUB_TOKEN required.
"""
import argparse
import json
import os
import re
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
CLIPS_PER_ACCOUNT = int(os.environ.get("CLIPS_PER_ACCOUNT", "12"))
ID_CACHE = OUT / "user_ids.json"
calls = 0


def die(m): print(f"ERROR: {m}", file=sys.stderr); sys.exit(1)


def th(path: str):
    """GET TikHub. Bearer auth + browser UA (Cloudflare)."""
    global calls
    if not KEY:
        die("TIKHUB_TOKEN not set. `set -a && . ./.env && set +a` first.")
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    calls += 1
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:90]}


VERSION = re.compile(r"\s*[\(\[][^\)\]]*("
                     r"version|remix|slowed|sped\s*up|edit|instrumental|acoustic|"
                     r"strings?|live|cover|reverb|mix|extended|short|loop|audio)"
                     r"[^\)\]]*[\)\]]", re.I)
FEAT = re.compile(r"\s*(feat\.?|ft\.?|featuring|with)\s+.*$", re.I)


def canon_key(title, artist):
    t = (title or "").lower()
    t = FEAT.sub("", VERSION.sub("", t))
    t = re.sub(r"[^a-z0-9 ]", "", t).strip()
    a = re.sub(r"[^a-z0-9 ]", "", (artist or "").lower()).strip()
    return f"{t}|{a}"


def load_accounts(pilot):
    accts = [a.strip().lstrip("@") for a in json.loads((ROOT / "accounts.json").read_text())["accounts"] if a.strip()]
    return accts[:pilot] if pilot else accts


def deep_find_id(d):
    for k in ("user_id", "pk", "id"):
        v = d.get(k) if isinstance(d, dict) else None
        if v and str(v).isdigit():
            return str(v)
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, dict):
                r = deep_find_id(v)
                if r:
                    return r
    return None


def resolve_ids(accounts):
    cache = json.loads(ID_CACHE.read_text()) if ID_CACHE.exists() else {}
    out, new = {}, 0
    for u in accounts:
        if cache.get(u):
            out[u] = cache[u]; continue
        d = th(f"/api/v1/instagram/v1/fetch_user_info_by_username?username={urllib.parse.quote(u)}")
        pk = deep_find_id(d) if isinstance(d, dict) else None
        if pk:
            cache[u] = pk; out[u] = pk; new += 1
        time.sleep(0.25)
    ID_CACHE.write_text(json.dumps(cache, indent=2))
    print(f"  user IDs: {len(out)}/{len(accounts)} ({new} new, {len(out)-new} cached)")
    return out


def reels_of(pk):
    d = th(f"/api/v1/instagram/v1/fetch_user_reels?user_id={pk}&count={CLIPS_PER_ACCOUNT}")
    items = (((d or {}).get("data") or {}).get("items")) if isinstance(d, dict) else None
    out = []
    for it in (items or []):
        out.append(it.get("media") or it)
    return out


def sound_of(media):
    cm = (media.get("clips_metadata") or {})
    canonical = cm.get("music_canonical_id")
    mi = cm.get("music_info") or {}
    if mi:
        ai = mi.get("music_asset_info") or {}
        return {"title": ai.get("title"), "artist": ai.get("display_artist"),
                "audio_id": ai.get("audio_cluster_id"), "canonical": canonical or mi.get("music_canonical_id"),
                "original": False}
    osi = cm.get("original_sound_info") or {}
    aid = osi.get("audio_asset_id") or osi.get("audio_cluster_id")
    if osi and aid:
        return {"title": osi.get("original_audio_title") or "Original audio",
                "artist": (osi.get("ig_artist") or {}).get("username") or "",
                "audio_id": aid, "canonical": canonical, "original": True}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", type=int, default=0)
    args = ap.parse_args()
    accounts = load_accounts(args.pilot)
    print(f"Niche audio chart (TikHub) — {len(accounts)} accounts, {CLIPS_PER_ACCOUNT} reels each")
    ids = resolve_ids(accounts)

    groups = defaultdict(lambda: {"title": "", "artist": "", "uses": 0, "accts": set(),
                                  "audio_ids": set(), "canonical": None, "original": False, "samples": []})
    scanned = 0
    for u, pk in ids.items():
        for media in reels_of(pk):
            s = sound_of(media)
            if not s or not (s["title"] or s["audio_id"]):
                continue
            if s["canonical"]:
                key = f"canon:{s['canonical']}"
            elif s["title"] and s["title"] != "Original audio":
                key = canon_key(s["title"], s["artist"])
            else:
                key = f"orig:{s['audio_id']}"
            g = groups[key]
            g["title"] = g["title"] or (s["title"] or "")
            g["artist"] = g["artist"] or (s["artist"] or "")
            g["canonical"] = g["canonical"] or s["canonical"]
            g["original"] = g["original"] or s["original"]
            g["uses"] += 1; g["accts"].add(u)
            if s["audio_id"]:
                g["audio_ids"].add(str(s["audio_id"]))
            code = media.get("code")
            if code and len(g["samples"]) < 4:
                g["samples"].append(f"https://www.instagram.com/reel/{code}/")
        scanned += 1
        time.sleep(0.15)

    ranked = sorted(groups.values(), key=lambda g: (len(g["accts"]), g["uses"]), reverse=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = [{"title": g["title"], "artist": g["artist"], "niche_creators": len(g["accts"]),
                "niche_uses": g["uses"], "canonical_id": g["canonical"], "audio_ids": sorted(g["audio_ids"]),
                "original": g["original"], "accounts": sorted(g["accts"]), "samples": g["samples"]}
               for g in ranked]
    (OUT / f"audio_trends_{stamp}.json").write_text(json.dumps(payload, indent=2))
    multi = sum(1 for g in ranked if len(g["accts"]) >= 2)
    lines = [f"# Niche audio chart — {stamp} (TikHub)",
             f"_{scanned} accounts · {len(ranked)} distinct tracks · {multi} used by ≥2 creators · {calls} API calls_\n",
             "| # | Track | Artist | Niche creators | Niche uses | Audio IDs merged |",
             "|--|--|--|--|--|--|"]
    for i, g in enumerate(r for r in ranked if len(r["accts"]) >= 2):
        lines.append(f"| {i+1} | {g['title'] or '(original)'} | {g['artist'] or '—'} | "
                     f"{len(g['accts'])} | {g['uses']} | {len(g['audio_ids'])} |")
    (OUT / f"audio_trends_{stamp}.md").write_text("\n".join(lines))
    print(f"\nWrote output/audio_trends_{stamp}.json/.md")
    print(f"TOTAL billable TikHub calls: {calls}")
    import cost_tracker
    cost_tracker.record("audio_trends", tikhub_calls=calls)


if __name__ == "__main__":
    main()
