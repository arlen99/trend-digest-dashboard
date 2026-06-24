#!/usr/bin/env python3
"""
AudD-identify the real song behind the niche sound chart's "original audio" buckets.

Each original-audio bucket in audio_trends output is keyed on Instagram's unique
`audio_asset_id` (verified: one bucket = exactly one real audio, never a title/creator
merge), so ONE sample reel per bucket is representative — fingerprint it once and the
result applies to the whole bucket.

For each candidate bucket: take a sample reel → TikHub fetch_post_by_url → its
`video_url` → AudD → {song, artist, link}. Results cached by audio_id in
output/chart_audd.json so weekly runs never re-pay for an audio already identified.

Scope (cost control): original buckets with niche_creators >= --min-creators
(default 2 — the multi-creator ones that actually rank), capped at --max.

Usage:
  set -a && . ./.env && set +a
  python3 chart_audd.py                 # multi-creator buckets
  python3 chart_audd.py --min-creators 1 --min-uses 4   # also heavily-reused solo
Env: TIKHUB_TOKEN, AUDD_TOKEN.
"""
import argparse
import glob
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
KEY = os.environ.get("TIKHUB_TOKEN")
AUDD = os.environ.get("AUDD_TOKEN", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
CACHE = OUT / "chart_audd.json"


def th_video_url(reel):
    """Reel permalink → its playable video_url (AudD reads audio from the video)."""
    u = f"https://api.tikhub.io/api/v1/instagram/v1/fetch_post_by_url?post_url={urllib.parse.quote(reel)}"
    req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        return (d.get("data", d) or {}).get("video_url", "")
    except Exception:  # noqa: BLE001
        return ""


def audd(video_url):
    body = {"url": video_url, "return": "spotify"}
    if AUDD:
        body["api_token"] = AUDD
    req = urllib.request.Request("https://api.audd.io/", data=urllib.parse.urlencode(body).encode(), method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:  # noqa: BLE001
        return {"reason": str(e)[:60]}
    res = resp.get("result")
    if resp.get("status") == "success" and res:
        return {"song": res.get("title"), "artist": res.get("artist"), "link": res.get("song_link", "")}
    return {"reason": resp.get("status") or "no match"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-creators", type=int, default=2)
    ap.add_argument("--min-uses", type=int, default=99)
    ap.add_argument("--max", type=int, default=40)
    args = ap.parse_args()
    if not KEY:
        raise SystemExit("TIKHUB_TOKEN not set.")

    files = sorted(glob.glob(str(OUT / "audio_trends_*.json")), key=os.path.getmtime)
    chart = json.loads(Path(files[-1]).read_text())
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    cands = [t for t in chart if t.get("original") and t.get("audio_ids")
             and (t["niche_creators"] >= args.min_creators or t["niche_uses"] >= args.min_uses)]
    cands.sort(key=lambda t: (t["niche_creators"], t["niche_uses"]), reverse=True)
    cands = cands[:args.max]

    new = hits = 0
    for t in cands:
        aid = str(t["audio_ids"][0])
        if aid in cache:
            continue
        reel = (t.get("samples") or [None])[0]
        if not reel:
            cache[aid] = {"reason": "no sample"}; continue
        vurl = th_video_url(reel)
        time.sleep(0.3)
        cache[aid] = audd(vurl) if vurl else {"reason": "no video_url"}
        new += 1
        if cache[aid].get("song"):
            hits += 1
            print(f"  ♫ {t['artist']}'s 'original audio' = {cache[aid]['song']} — {cache[aid]['artist']}")
        time.sleep(1.5)
    CACHE.write_text(json.dumps(cache, indent=2))
    print(f"\nAudD on chart: {new} new buckets fingerprinted, {hits} identified "
          f"({sum(1 for v in cache.values() if v.get('song'))} total in cache) → output/chart_audd.json")


if __name__ == "__main__":
    main()
