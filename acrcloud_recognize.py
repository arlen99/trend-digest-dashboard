#!/usr/bin/env python3
"""
ACRCloud audio fingerprinting — identifies the real song behind an "original audio"
clip. PRIMARY recognizer as of 2026-07-28, replacing AcoustID: real testing showed
AcoustID/Chromaprint at a 0% hit rate on this project's actual content (0/8, including
one commercially-released track AudD identified with no trouble) — Chromaprint matches
close to the exact original master, but Reels/TikTok "trending audio" is almost always
a sped-up, pitch-shifted, or trimmed edit of that master. ACRCloud is the recognition
engine TikTok's own SoundOn platform licenses specifically to detect that kind of
"derivative" clip, so it should handle this project's content far better. AudD stays
as a second-opinion fallback (see audd_recognize() in curate_posts.py/chart_audd.py)
for whatever ACRCloud still misses.

Free plan: 100 recognition requests (per account dashboard 2026-07-28 — ACRCloud
doesn't publish this anywhere, so treat it as a hard, unconfirmed-reset budget until
a real week's usage shows whether/when it renews).

Requires ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET in env — from an
"Audio & Video Recognition" project's dashboard at console.acrcloud.com. No `requests`
dependency in this project, so the multipart upload is built by hand. Uses `ffmpeg`
(already a pipeline dependency) to strip video before upload — see _extract_audio().

Usage:
  import acrcloud_recognize
  result = acrcloud_recognize.recognize(video_url)  # -> {"song","artist","link"} or {}
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import uuid

HOST = os.environ.get("ACRCLOUD_HOST", "")
ACCESS_KEY = os.environ.get("ACRCLOUD_ACCESS_KEY", "")
ACCESS_SECRET = os.environ.get("ACRCLOUD_ACCESS_SECRET", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
MIN_SCORE = 50  # ACRCloud's own match-confidence score (0-100) — below this, treat as no match.
# Set conservatively low: a real test case scored 76 for a match confirmed correct
# (artist name literally matched the creator's own handle), so 80 was already
# producing false negatives on genuine hits. No observed false-positive score to
# calibrate a tighter floor against — this just guards against near-zero noise.

calls = 0  # every real identify() call — free plan is capped at 100 total, track it


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data) > 5000


def _extract_audio(video_path, audio_path):
    """Strips the video stream and caps at 20s — full Reels/TikTok MP4s (video +
    audio) can exceed ACRCloud's upload size limit (confirmed live: a 6.9MB/20s
    clip was rejected with status code 3016, "file too large" — silently read as
    a plain no-match until this fix, since audio-only files ACRCloud's own error
    message recommends 10-20s of audio, which is also all the identify API needs).
    Returns True if ffmpeg produced a usable file, False if ffmpeg is unavailable
    or the clip has no audio track (caller then falls back to the raw video)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-t", "20", "-c:a", "copy", audio_path],
            capture_output=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 - ffmpeg missing/failed shouldn't crash the caller
        return False
    return r.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000


def _sign(timestamp):
    """ACRCloud's documented Identify API signing scheme: HMAC-SHA1 over a fixed-
    order string (method, URI, access key, data type, signature version, timestamp),
    base64-encoded."""
    string_to_sign = "\n".join(["POST", "/v1/identify", ACCESS_KEY, "audio", "1", timestamp])
    digest = hmac.new(ACCESS_SECRET.encode("ascii"), string_to_sign.encode("ascii"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _identify(file_path):
    timestamp = str(int(time.time()))
    fields = {
        "access_key": ACCESS_KEY,
        "sample_bytes": str(os.path.getsize(file_path)),
        "timestamp": timestamp,
        "signature": _sign(timestamp),
        "data_type": "audio",
        "signature_version": "1",
    }
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    with open(file_path, "rb") as f:
        file_data = f.read()
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sample\"; filename=\"sample.mp4\"\r\n"
         f"Content-Type: application/octet-stream\r\n\r\n").encode() + file_data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"https://{HOST}/v1/identify", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def recognize(video_url: str) -> dict:
    """Downloads the clip, sends it to ACRCloud's Identify API. Returns
    {"song", "artist", "link"} on a match, else {} (never raises — every failure
    mode, including missing credentials, is a clean miss so callers can fall back
    to AudD the same way a genuine no-match would)."""
    global calls
    if not (HOST and ACCESS_KEY and ACCESS_SECRET) or not video_url:
        return {}
    video_path = audio_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            video_path = tmp.name
        if not _download(video_url, video_path):
            return {}
        audio_path = video_path + ".m4a"
        upload_path = audio_path if _extract_audio(video_path, audio_path) else video_path
        calls += 1
        resp = _identify(upload_path)
        if (resp.get("status") or {}).get("code") != 0:
            return {}
        music = (resp.get("metadata") or {}).get("music") or []
        for m in music:
            if (m.get("score") or 0) < MIN_SCORE:
                continue
            title = m.get("title")
            if not title:
                continue
            artists = m.get("artists") or []
            artist = artists[0].get("name") if artists else ""
            spotify_id = (((m.get("external_metadata") or {}).get("spotify") or {}).get("track") or {}).get("id")
            link = f"https://open.spotify.com/track/{spotify_id}" if spotify_id else ""
            return {"song": title, "artist": artist, "link": link}
        return {}
    except Exception:  # noqa: BLE001 - network/parse errors are a clean miss, not a crash
        return {}
    finally:
        for p in (video_path, audio_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 acrcloud_recognize.py <video_url>")
    print(json.dumps(recognize(sys.argv[1]), indent=2, ensure_ascii=False))
