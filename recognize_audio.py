#!/usr/bin/env python3
"""
Identify the real background song behind Instagram "original audio" labels by
audio-fingerprinting each reel's audio (AudD.io).

Instagram only exposes the real song+artist when the creator used a *tagged*
track. When they pick "original audio" (often a licensed song under a voiceover),
IG just says "Original audio". This sends those clips' audio_url to AudD, which
returns the matched song.

Usage:
  python3 recognize_audio.py output/top_posts_<date>.json
Writes output/audio_detect.json  -> { post_url: {song, artist, link} | {reason} }

Notes:
- Works on AudD's free tier WITHOUT a token but it's rate-limited (~10 calls);
  set AUDD_TOKEN in .env (free account at https://dashboard.audd.io) to clear all
  posts each week and get a confidence-friendly response.
- Matches under heavy voiceover can be approximate / occasionally wrong, so the
  dashboard marks these "detected" and links out to verify.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
TOKEN = os.environ.get("AUDD_TOKEN", "")


def recognize(audio_url: str) -> dict:
    body = {"url": audio_url, "return": "spotify"}
    if TOKEN:
        body["api_token"] = TOKEN
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request("https://api.audd.io/", data=data, method="POST")
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    res = resp.get("result")
    if resp.get("status") == "success" and res:
        return {"song": res.get("title"), "artist": res.get("artist"),
                "link": res.get("song_link", "")}
    return {"reason": resp.get("status") or "no match"}


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        sys.exit("Usage: python3 recognize_audio.py output/top_posts_<date>.json")
    rows = json.loads(Path(path).read_text())
    out, n = {}, 0
    for r in rows:
        if not r.get("audio_is_original"):
            continue  # tagged songs already carry the real title from IG
        au = r.get("audio_url")
        if not au:
            out[r["url"]] = {"reason": "no audio_url"}
            continue
        try:
            out[r["url"]] = recognize(au)
        except Exception as e:  # noqa: BLE001
            out[r["url"]] = {"reason": str(e)[:80]}
        n += 1
        time.sleep(1.5)
    (ROOT / "output" / "audio_detect.json").write_text(json.dumps(out, indent=2))
    hits = sum(1 for v in out.values() if v.get("song"))
    print(f"recognized {hits}/{n} 'original audio' posts -> output/audio_detect.json")


if __name__ == "__main__":
    main()
