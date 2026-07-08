#!/usr/bin/env python3
"""
Inject the niche sound chart + TikTok trending-sounds cross-reference into the
dashboard's data.json so the Audio Trends page can render them.

Sources (latest in output/):
  audio_trends_*.json  — niche sound chart: many accounts scanned, the SAME song
                         consolidated across all its audio IDs via canonical_id.
  tiktok_trends_*.json — TikTok trending sounds, each flagged also_in_ig_niche.

Writes two top-level keys into dashboard/data.json: `soundChart` and `tiktokSounds`.
Pure transform, no network. Preview audio is NOT set here — fetch_audio.py fills
each row's `preview` with a durable Blob URL of the REAL track from the IG audio
page (Deezer catalogue previews were deprecated: they sometimes matched the wrong
song and sometimes wouldn't play at all).

Usage: python3 audio_chart_to_dashboard.py [--chart 30] [--tt 20]
"""
import argparse
import glob
import json
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"


def latest(pattern):
    fs = sorted(glob.glob(str(ROOT / "output" / pattern)))
    return json.loads(Path(fs[-1]).read_text()) if fs else []


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
    for t in named[:args.chart]:
        # AudD-identified real song behind an "original audio" bucket (keyed by its
        # audio_id) — an informational label only. The preview PLAYBACK is always the
        # real audio-page/reel track (self-hosted by fetch_audio.py), so it's ground
        # truth regardless of whether AudD's name guess is right.
        aid = str((t.get("audio_ids") or [""])[0])
        det = audd.get(aid) if (t.get("original") and aid) else None
        det = det if (det and det.get("song")) else None
        chart.append({
            "title": t.get("title") or "Original audio",
            "artist": t.get("artist", ""),
            "creators": t.get("niche_creators", 0),
            "uses": t.get("niche_uses", 0),
            "original": bool(t.get("original")),
            "ids": len(t.get("audio_ids") or []),
            "audioId": aid,  # → reels/audio/<id> link + fetch_audio.py's self-hosted preview clip
            "accounts": (t.get("accounts") or [])[:3],
            "samples": (t.get("samples") or [])[:4],   # reels that used it (within niche)
            "sample": (t.get("samples") or [None])[0],
            "detected": bool(det),
            "detectedSong": det["song"] if det else "",
            "detectedArtist": det.get("artist", "") if det else "", "detectedLink": det.get("link", "") if det else "",
            "preview": "",  # filled by fetch_audio.py with a durable Blob URL (real track)
        })
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
          f"{det_n} AudD-identified original audio; previews filled next by fetch_audio.py) "
          f"+ tiktokSounds ({len(tt)}, {xref} cross-platform) into data.json.")


if __name__ == "__main__":
    main()
