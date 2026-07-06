#!/usr/bin/env python3
"""
Automated weekly IG curation — the step that used to require a human/Claude
session looking at thumbnails by hand. Reviews the freshly-scraped, deduped
candidates (Reels via fetch_thumbs.py, Carousels via fetch_carousels.py),
asks Claude (vision) to include/exclude each and tag the included ones, runs
AudD fingerprinting per included Reel for real audio ID (fetch_user_posts no
longer returns clips_metadata.music_info/original_sound_info at all — verified
against a post independently confirmed to use a licensed track, so this is an
external API loss, not something fixable by re-parsing our own response), and
writes the result into dashboard/data.json's IG lane.

Runs fully automatically (no review gate) — the prompt itself is the exclusion
filter for monetization bait, sponsored/branded content, and off-niche posts.

Usage:
  set -a && . ./.env && set +a
  python3 curate_posts.py output/top_posts_<date>_fresh.json
Env: ANTHROPIC_API_KEY (required), AUDD_TOKEN (optional — free tier used if unset,
     rate-limited to ~10 calls/day per AudD's own limits).
"""
import base64
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
DASH = ROOT / "dashboard"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUDD_TOKEN = os.environ.get("AUDD_TOKEN", "")
MODEL = "claude-sonnet-5"
MAX_CANDIDATES = 35  # bound API cost; ranked by outlier score, best first
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")

HOOK_TYPES = ["Bold claim", "List/Number", "Location reveal", "Question", "Relatable confession", "Storytime", "Tutorial/How-to"]
TRIGGERS = ["Awe", "Desire/Wanderlust", "Ego", "Nostalgia", "Relatability", "Surprise"]
VISUAL_STYLES = ["Bright/airy", "Cinematic/teal-orange", "Drone/aerial", "Film grain", "Moody/dark"]

TOOL_SCHEMA = {
    "name": "curate",
    "description": "Decide whether a candidate post belongs on a travel & cinematic content Swipe File, and tag it if so.",
    "input_schema": {
        "type": "object",
        "properties": {
            "include": {"type": "boolean", "description": "True only if this is genuine travel/cinematic content worth studying. False for monetization bait ('DM me to learn how'), sponsored/branded/ad content, giveaways, or content that isn't actually about travel/cinematic filmmaking."},
            "exclude_reason": {"type": "string", "description": "If include=false, a one-line reason."},
            "hook": {"type": "string", "description": "A short punchy headline describing the post's hook (not the raw caption) — under 12 words."},
            "hookTypes": {"type": "array", "items": {"type": "string", "enum": HOOK_TYPES}, "description": "0-2 that genuinely apply."},
            "triggers": {"type": "array", "items": {"type": "string", "enum": TRIGGERS}, "description": "0-2 emotional triggers driving engagement."},
            "visualStyles": {"type": "array", "items": {"type": "string", "enum": VISUAL_STYLES}, "description": "0-2 visual style tags, judged from the image(s)."},
            "rotate": {"type": "integer", "enum": [0, 90, 180, 270], "description": "Some source videos have their content baked in sideways (e.g. a landscape-shot scene stored in a portrait-shaped frame — horizon lines vertical instead of horizontal, people lying on their side instead of standing). Look at the image(s): if upright, 0. If the content needs rotating CLOCKWISE by this many degrees to look correctly oriented, specify that (90, 180, or 270). This is about the actual scene orientation, not the container's width/height."},
            "notes": {"type": "string", "description": "1-2 sentences: why this performed / what's notable about the hook or execution. Curatorial voice, not a caption restatement."},
        },
        "required": ["include"],
    },
}


def img_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    media_type = "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def call_claude(images: list, text: str) -> dict:
    body = {
        "model": MODEL,
        "max_tokens": 500,
        "tools": [TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": "curate"},
        "messages": [{"role": "user", "content": images + [{"type": "text", "text": text}]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input", {})
    return {"include": False, "exclude_reason": "no tool_use in response"}


def audd_recognize(audio_url: str) -> dict:
    body = {"url": audio_url, "return": "spotify"}
    if AUDD_TOKEN:
        body["api_token"] = AUDD_TOKEN
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request("https://api.audd.io/", data=data, method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception:  # noqa: BLE001
        return {}
    res = resp.get("result")
    if resp.get("status") == "success" and res:
        return {"song": res.get("title", ""), "artist": res.get("artist", ""), "link": res.get("song_link", "")}
    return {}


def hook_text_for(url: str) -> str:
    cache_path = OUT / "hook_texts.json"
    if not cache_path.exists():
        return ""
    cache = json.loads(cache_path.read_text())
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url or "")
    code = m.group(1) if m else ""
    entry = cache.get(code) or {}
    hook = entry.get("hook", "")
    # drop obviously garbled OCR reads (short on real words, heavy on symbol noise)
    words = re.findall(r"[A-Za-z']{3,}", hook)
    return hook if len(" ".join(words)) >= max(6, len(hook) * 0.4) else ""


def main() -> None:
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY not set — add it to .env (local) and as a repo secret (CI).")
    path = sys.argv[1] if len(sys.argv) > 1 else max(
        glob.glob(str(OUT / "top_posts_*_fresh.json")), default="")
    if not path:
        sys.exit("No top_posts_*_fresh.json found. Run scrape.py + scrape_dedupe.py first.")
    rows = json.loads(Path(path).read_text())
    rows.sort(key=lambda r: r.get("outlier_score", 0), reverse=True)
    rows = rows[:MAX_CANDIDATES]

    curated = []
    for i, r in enumerate(rows, 1):
        fmt = r.get("format", "Reel")
        acct = re.sub(r"[^a-z0-9_.]", "", r["account"].lower())
        if fmt == "Carousel":
            paths = r.get("carousel_paths") or []
            image_paths = [OUT / p for p in paths[:4]]
        else:
            thumb = OUT / "thumbs" / f"{i:02d}_{acct}.jpg"
            image_paths = [thumb] if thumb.exists() else []
        images = [img_block(p) for p in image_paths if p.exists()]
        if not images:
            print(f"  {i:02d} @{acct} -> no image available, skipping")
            continue

        htext = hook_text_for(r["url"]) if fmt != "Carousel" else ""
        prompt = (
            f"Account: @{r['account']}\nFormat: {fmt}\nCaption: {r.get('caption','')[:300]}\n"
            f"On-screen text (OCR, may be empty/imperfect): {htext or '(none)'}\n"
            f"Metrics: {r.get('views',0):,} views, {r.get('likes',0):,} likes, {r.get('comments',0):,} comments, "
            f"outlier score {r.get('outlier_score')}x\n\n"
            "This is a candidate for a travel & cinematic filmmaking content Swipe File — a reference board of "
            "genuinely well-executed travel/cinematic posts for creators to study hooks and visual style from. "
            "Decide whether to include it, and if so, tag it."
        )
        try:
            result = call_claude(images, prompt)
        except Exception as e:  # noqa: BLE001
            print(f"  {i:02d} @{acct} -> API error: {e}")
            continue

        if not result.get("include"):
            print(f"  {i:02d} @{acct} -> excluded ({result.get('exclude_reason','no reason given')})")
            continue

        # scrape.py now overlays real audio_id/audio_song/audio_artist onto Reels
        # from fetch_user_reels (fetch_user_posts itself no longer returns it).
        # AudD fingerprinting is still needed when audio_is_original is True — IG's
        # own metadata just says "original audio" there even when a real (often
        # licensed-under-a-voiceover) song is playing underneath; a canonical ID
        # existing doesn't mean IG revealed what the track actually is. It's also
        # the fallback when the overlay found nothing at all.
        audio_id = r.get("audio_id") or ""
        audio_song = r.get("audio_song") or ""
        audio_artist = r.get("audio_artist") or ""
        audio_is_original = r.get("audio_is_original", True)
        audd = {}
        if fmt != "Carousel" and r.get("video") and (audio_is_original or not audio_id):
            audd = audd_recognize(r["video"])
            time.sleep(0.3)
            if audd:
                audio_song, audio_artist = audd.get("song", ""), audd.get("artist", "")

        real_song = bool(audio_song) and audio_song != "Original audio"
        views = r.get("views") or 0
        engRate = round(r["engagement"] / views * 100, 1) if views else None
        post = {
            "account": r["account"],
            "url": r["url"],
            "hook": result.get("hook", ""),
            "format": fmt,
            "views": views,
            "likes": r.get("likes", 0),
            "comments": r.get("comments", 0),
            "engRate": engRate,
            "outlier": r.get("outlier_score"),
            "hookTypes": [h for h in result.get("hookTypes", []) if h in HOOK_TYPES],
            "triggers": [t for t in result.get("triggers", []) if t in TRIGGERS],
            "visualStyles": [v for v in result.get("visualStyles", []) if v in VISUAL_STYLES],
            "audio": f"{audio_song} · {audio_artist}" if real_song else f"Original audio · @{r['account']}",
            "notes": result.get("notes", ""),
            "week": date.today().isoformat(),
            "video": r.get("video", "") if fmt != "Carousel" else "",
            "audioId": audio_id,
            "audioLink": f"https://www.instagram.com/reels/audio/{audio_id}/" if audio_id else "",
            "audioSongLink": audd.get("link", ""),
            "audioDetected": real_song and bool(audd),  # "detected" = AudD fingerprint match specifically, matches existing dashboard semantics
            "audioSong": audio_song or "Original audio",
            "audioArtist": audio_artist or r["account"],
            "date": (r.get("timestamp") or "")[:10],
            "videoText": htext,
            "audioPreview": "",
            "audioDeezer": "",
            "platform": "instagram",
            # Some source videos have their content baked in sideways (not a
            # container rotation flag ffmpeg/browsers would auto-correct — the
            # actual pixels are rotated). Claude judges this from the same
            # thumbnail it already reviewed for tagging; fetch_videos.py applies
            # the correction when self-hosting.
            "rotate": (result.get("rotate") or 0) if fmt != "Carousel" else 0,
        }
        if fmt == "Carousel":
            post["thumb"] = (r.get("carousel_paths") or [""])[0]
            post["carousel"] = r.get("carousel_paths") or []
        else:
            post["thumb"] = f"thumbs/{i:02d}_{acct}.jpg"
        curated.append(post)
        print(f"  {i:02d} @{acct} -> included: {result.get('hook','')[:60]}")

    print(f"\n{len(curated)}/{len(rows)} candidates included.")
    data = json.loads((DASH / "data.json").read_text())
    kept = [p for p in data["posts"] if p.get("platform") == "tiktok" or p.get("lane") == "keyword"]
    curated.sort(key=lambda p: p["outlier"] or 0, reverse=True)
    data["posts"] = curated + kept
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {len(curated)} curated IG posts + kept {len(kept)} TikTok/keyword posts into dashboard/data.json.")


if __name__ == "__main__":
    main()
