#!/usr/bin/env python3
"""
Self-host each niche sound-chart track's REAL audio on Vercel Blob so the Audio
Trends previews play the ACTUAL track from Instagram's audio page — replacing the
Deezer catalogue-guess previews (which sometimes matched the wrong song, and
sometimes had no preview at all). Same durable-Blob pattern as fetch_videos.py.

Per chart track (keyed by IG audio_id):
  - Licensed/registered track: fetch_music_posts(reels/audio/<id>) → the track's
    `progressive_download_url` (the clean original — the same audio the IG audio
    page plays). That IG-internal lookup is genuinely flaky (~1-in-3..1-in-9), so
    retry persistently, exactly like dashboard/api/download-audio.js does.
  - Creator-original audio (no licensed URL): extract the sound from a
    representative sample reel's video (fetch_post_by_url → video_url → ffmpeg
    -vn). That's the actual sound as used, not a catalogue guess.

Trim to a short preview clip, upload to Blob at a STABLE path audio/<audio_id>.m4a
(skip if already there — Blob URLs never expire), and point the chart row's
`preview` at it. Then PRUNE audio blobs not referenced by the current chart (it
fully refreshes weekly, so old clips would just accumulate).

Pure stdlib + a real ffmpeg binary (already required by the pipeline).
Usage: set -a && . ./.env && set +a && python3 fetch_audio.py
Env: TIKHUB_TOKEN, BLOB_READ_WRITE_TOKEN. Knobs: AUDIO_PREVIEW_SECS (default 45).
"""
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
TIKHUB = os.environ.get("TIKHUB_TOKEN")
BLOB = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://blob.vercel-storage.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
PREVIEW_SECS = int(os.environ.get("AUDIO_PREVIEW_SECS", "45"))
# Flaky IG-internal lookup — retry like download-audio.js (well within CI budget,
# and only NEW tracks are resolved each week since existing ones are on Blob).
LOOKUP_ATTEMPTS = 8


th_calls = 0


def th(path):
    global th_calls
    for _ in range(3):
        try:
            req = urllib.request.Request("https://api.tikhub.io" + path,
                                         headers={"Authorization": "Bearer " + TIKHUB, "User-Agent": UA, "accept": "application/json"})
            th_calls += 1
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    return {}


def deep(o, *path, default=None):
    cur = o
    for p in path:
        if isinstance(p, int):
            cur = cur[p] if isinstance(cur, list) and len(cur) > p else None
        else:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return default
    return cur


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def licensed_track_url(audio_id):
    """The clean registered track behind an IG audio page, if IG exposes one."""
    page = f"https://www.instagram.com/reels/audio/{audio_id}/"
    for attempt in range(LOOKUP_ATTEMPTS):
        j = th("/api/v1/instagram/v1/fetch_music_posts?music_url=" + urllib.parse.quote(page))
        ai = deep(j, "data", "metadata", "music_info", "music_asset_info", default={}) or {}
        raw = ai.get("progressive_download_url") or ai.get("fast_start_progressive_download_url")
        if raw:
            return raw
        if attempt < LOOKUP_ATTEMPTS - 1:
            time.sleep(2)
    return ""


def reel_audio_bytes(sample_url):
    """Fallback: the sound as actually used, extracted from a sample reel's video."""
    d = th("/api/v1/instagram/v1/fetch_post_by_url?post_url=" + urllib.parse.quote(sample_url))
    vurl = deep(d, "data", "video_url", default="") or ""
    return download(vurl) if vurl else b""


def to_preview_clip(raw_bytes):
    """Trim to a short AAC preview clip (lossless stream copy — no re-encode).
    Works for both a bare .m4a (licensed track) and an .mp4 (reel video, -vn drops
    the video). Returns b'' if ffmpeg can't produce audio."""
    with tempfile.TemporaryDirectory() as td:
        src, dst = f"{td}/in", f"{td}/out.m4a"
        Path(src).write_bytes(raw_bytes)
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-t", str(PREVIEW_SECS), "-c:a", "copy", dst],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if r.returncode != 0 or not Path(dst).exists() or Path(dst).stat().st_size < 2000:
            # some sources won't stream-copy cleanly (odd container) — re-encode
            r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-t", str(PREVIEW_SECS),
                                "-c:a", "aac", "-b:a", "128k", dst],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode != 0 or not Path(dst).exists():
                return b""
        return Path(dst).read_bytes()


def blob_put(pathname, data):
    req = urllib.request.Request(f"{BLOB_API}/{pathname}", data=data, method="PUT", headers={
        "authorization": "Bearer " + BLOB, "x-content-type": "audio/mp4",
        "x-add-random-suffix": "0", "x-allow-overwrite": "1", "x-api-version": "7"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())["url"]


def blob_list(prefix="audio/"):
    req = urllib.request.Request(f"{BLOB_API}?prefix={prefix}&limit=1000", headers={"authorization": "Bearer " + BLOB})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("blobs", [])


def blob_delete(urls):
    if not urls:
        return
    req = urllib.request.Request(f"{BLOB_API}/delete", method="POST",
                                 data=json.dumps({"urls": urls}).encode(),
                                 headers={"authorization": "Bearer " + BLOB, "content-type": "application/json", "x-api-version": "7"})
    urllib.request.urlopen(req, timeout=60).read()


def resolve_and_host(audio_id, original, sample_url, existing):
    """Return a durable Blob URL for this track's preview audio, or '' on failure."""
    pathname = f"audio/{audio_id}.m4a"
    if pathname in existing:
        return existing[pathname]  # already self-hosted — Blob URLs never expire
    raw = b""
    if not original:
        src = licensed_track_url(audio_id)
        if src:
            try:
                raw = download(src)
            except Exception:  # noqa: BLE001
                raw = b""
    if len(raw) < 5000 and sample_url:  # original audio, or licensed lookup failed → use the reel
        try:
            raw = reel_audio_bytes(sample_url)
        except Exception:  # noqa: BLE001
            raw = b""
    if len(raw) < 5000:
        return ""
    clip = to_preview_clip(raw)
    if len(clip) < 2000:
        return ""
    try:
        return blob_put(pathname, clip)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ blob put {audio_id}: {str(e)[:60]}")
        return ""


def main():
    if not (TIKHUB and BLOB):
        raise SystemExit("Need TIKHUB_TOKEN and BLOB_READ_WRITE_TOKEN in env.")
    data = json.loads((DASH / "data.json").read_text())
    chart = data.get("soundChart") or []
    if not chart:
        print("No soundChart in data.json — run audio_chart_to_dashboard.py first."); return

    existing = {b["pathname"]: b["url"] for b in blob_list("audio/")}
    hosted = {}  # audio_id -> blob url
    ok = kept = fail = 0
    for c in chart:
        aid = str(c.get("audioId") or "")
        if not aid:
            c["preview"] = ""
            continue
        pathname = f"audio/{aid}.m4a"
        already = pathname in existing
        url = resolve_and_host(aid, bool(c.get("original")), c.get("sample") or (c.get("samples") or [None])[0], existing)
        c["preview"] = url
        c.pop("deezer", None)  # Deezer deprecated — real audio-page track now
        if url and already:
            kept += 1
        elif url:
            ok += 1; hosted[aid] = url
            print(f"  ✓ {(c.get('title') or '')[:34]:36} audio/{aid}.m4a")
        else:
            fail += 1
            print(f"  ✗ {(c.get('title') or '')[:34]:36} (no audio resolved)")
        time.sleep(0.1)

    # also fill per-post track previews for any curated post whose audio we hosted
    # (free — reuses the same clip; posts not in the chart just stay preview-less)
    id_to_url = {p.split("/")[1].split(".")[0]: u for p, u in existing.items()}
    id_to_url.update(hosted)
    for p in data.get("posts", []):
        aid = str(p.get("audioId") or "")
        if aid and aid in id_to_url:
            p["audioPreview"] = id_to_url[aid]

    # prune audio blobs no longer referenced by the current chart
    keep = {f"audio/{str(c.get('audioId'))}.m4a" for c in chart if c.get("audioId")}
    stale = [b["url"] for b in blob_list("audio/") if b["pathname"] not in keep]
    if stale:
        blob_delete(stale)

    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nAudio previews: {ok} new, {kept} kept, {fail} unresolved. "
          f"Pruned {len(stale)} stale clips. Chart now self-hosts real audio (Deezer deprecated).")
    import cost_tracker
    cost_tracker.record("fetch_audio", tikhub_calls=th_calls)


if __name__ == "__main__":
    main()
