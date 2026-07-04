#!/usr/bin/env python3
"""
Inject the niche sound chart + TikTok trending-sounds cross-reference into the
dashboard's data.json so the Audio Trends page can render them.

Sources (latest in output/):
  audio_trends_*.json  — niche sound chart: many accounts scanned, the SAME song
                         consolidated across all its audio IDs via canonical_id.
  tiktok_trends_*.json — TikTok trending sounds, each flagged also_in_ig_niche.

Writes two top-level keys into dashboard/data.json: `soundChart` and `tiktokSounds`.
Pure transform, no network.

Usage: python3 audio_chart_to_dashboard.py [--chart 30] [--tt 20]
"""
import argparse
import glob
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"


def latest(pattern):
    fs = sorted(glob.glob(str(ROOT / "output" / pattern)))
    return json.loads(Path(fs[-1]).read_text()) if fs else []


def deezer(title, artist):
    """Public Deezer search (no key) → (preview mp3 url, deezer page url). Best-effort."""
    if not title or title.lower() == "original audio":
        return "", ""
    q = urllib.parse.quote(f'{title} {artist}'.strip())
    try:
        req = urllib.request.Request(f"https://api.deezer.com/search?q={q}&limit=1", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        hit = (d.get("data") or [{}])[0]
        return hit.get("preview", "") or "", hit.get("link", "") or ""
    except Exception:  # noqa: BLE001
        return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart", type=int, default=30)
    ap.add_argument("--tt", type=int, default=20)
    args = ap.parse_args()

    raw = latest("audio_trends_*.json")
    named = [t for t in raw if (t.get("title") or t.get("artist"))]
    named.sort(key=lambda t: (t.get("niche_creators", 0), t.get("niche_uses", 0)), reverse=True)
    af = ROOT / "output" / "chart_audd.json"
    audd = json.loads(af.read_text()) if af.exists() else {}
    chart = []
    unverified = 0
    for t in named[:args.chart]:
        # AudD-identified real song behind an "original audio" bucket (keyed by its audio_id)
        aid = str((t.get("audio_ids") or [""])[0])
        det = audd.get(aid) if (t.get("original") and aid) else None
        det = det if (det and det.get("song")) else None
        # the player + Deezer link resolve to the DETECTED song when we have one
        verified = False
        if det:
            preview, dz = deezer(det["song"], det.get("artist", ""))
            verified = bool(preview or dz)   # corroborated by a music catalogue? if not → keep but mark unverified
            if not verified:
                unverified += 1
        else:
            preview, dz = deezer(t.get("title"), t.get("artist"))
        chart.append({
            "title": t.get("title") or "Original audio",
            "artist": t.get("artist", ""),
            "creators": t.get("niche_creators", 0),
            "uses": t.get("niche_uses", 0),
            "original": bool(t.get("original")),
            "ids": len(t.get("audio_ids") or []),
            "audioId": aid,  # lets the dashboard build a reels/audio/<id> link → raw-track download, not a reel's mixed audio
            "accounts": (t.get("accounts") or [])[:3],
            "samples": (t.get("samples") or [])[:4],   # reels that used it (within niche)
            "sample": (t.get("samples") or [None])[0],
            "detected": bool(det), "detectedVerified": verified,
            "detectedSong": det["song"] if det else "",
            "detectedArtist": det.get("artist", "") if det else "", "detectedLink": det.get("link", "") if det else "",
            "preview": preview, "deezer": dz,           # 30s inline player + Deezer page
        })
        time.sleep(0.15)
    got = sum(1 for c in chart if c["preview"])
    det_n = sum(1 for c in chart if c["detected"])

    tt_raw = latest("tiktok_trends_*.json")
    # cross-platform sounds first (the valuable signal), then chart order
    tt_raw_sorted = sorted(tt_raw, key=lambda x: (bool(x.get("also_in_ig_niche")), x.get("use_count") or 0), reverse=True)
    tt = [{
        "title": s.get("title") or "Untitled",
        "artist": s.get("artist", ""),
        "useCount": s.get("use_count") or 0,
        "alsoInIgNiche": bool(s.get("also_in_ig_niche")),
        "igNicheCreators": s.get("ig_niche_creators"),
        "link": f"https://www.tiktok.com/music/x-{s.get('music_id')}" if s.get("music_id") else "",
    } for s in tt_raw_sorted[:args.tt]]

    data = json.loads((DASH / "data.json").read_text())
    data["soundChart"] = chart
    data["tiktokSounds"] = tt
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    xref = sum(1 for s in tt if s["alsoInIgNiche"])
    print(f"Injected soundChart ({len(chart)} of {len(named)} named, from {len(raw)} consolidated; "
          f"{got} with Deezer preview, {det_n} AudD-identified original audio ({unverified} unverified — no catalogue match)) "
          f"+ tiktokSounds ({len(tt)}, {xref} cross-platform) into data.json.")


if __name__ == "__main__":
    main()
