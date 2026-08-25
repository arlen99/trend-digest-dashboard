#!/usr/bin/env python3
"""
Harvest the ON-SCREEN HOOK TEXT from each post's opening moments, for trend
detection (Phase 2 — hook-anchored trends, independent of audio).

The hook is burned into the pixels, not in any API field, so we grab an early
frame (video) or the image itself (Photo/Carousel) and OCR it. OCR is FREE +
on-device via macOS Vision (tools/ocr); set OCR_CMD to a Tesseract wrapper to
run portably in the cloud.

RETRY ON GIBBERISH (2026-08): a single fixed frame can land on a transition,
motion blur, or a busy background and OCR to noise even when the hook is
perfectly readable a moment earlier or later — confirmed live on a real case
(instagram.com/reel/DaM-kZ0vatB/): the original 2s/4s frames read
"t's x "PR, a ee (c) ng: ..." while 10 of 12 other timestamps on the SAME
video read the hook cleanly as "risk is always better than regret". ffmpeg's
`-ss` seek over a remote stream isn't always frame-exact, so even the same
nominal timestamp can land differently between runs. Fix: try more candidate
frames, keep the first one that reads as coherent text (is_gibberish() below,
calibrated against the real 402-entry corpus: 15.2% flagged, spot-checked as
accurate — short real hooks like "more work. more life." or "WHEN?" do NOT
trip it). If every attempt is gibberish, fall back to the longest one rather
than losing the post entirely — a queryable low-confidence hook beats none.

Per video post: pull its video_url (from the scrape row, else TikHub
fetch_post_by_url) -> try frames at increasing timestamps -> OCR each -> keep
the first coherent read (or the fallback). Per Photo/Carousel post: download
the image(s) directly (first 2 carousel slides — the hook is almost always on
the opening slide, same convention as the early-video-frame assumption) and
OCR those the same way. Cached by shortcode in output/hook_texts.json so
re-runs are free.

Usage:
  set -a && . ./.env && set +a
  python3 hook_text.py output/top_posts_<date>.json [--limit 150]
  python3 hook_text.py --retrofit-gibberish 3   # re-OCR gibberish cache entries
                                                  # from the last N runs (data.json's
                                                  # current + archived weeks)
Env: TIKHUB_TOKEN (only for posts lacking a video url). OCR_CMD overrides the engine.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
DASH = ROOT / "dashboard"
CACHE = OUT / "hook_texts.json"
OCR_CMD = os.environ.get("OCR_CMD", str(ROOT / "tools" / "ocr"))  # swap for tesseract in cloud
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")

# Frame timestamps to try in order, video posts only. First two match the
# original single-shot design (cheapest, usually enough); the rest are the
# retry pool, only spent when needed. Widened based on the live case above,
# where nearly every timestamp from 0.5s-6.0s read clean except the original 2s.
FRAME_TIMES = [2.0, 4.0, 1.0, 3.0, 5.0, 0.5, 5.5, 6.0]

# A hook overlay stays on screen; a rolling auto-caption changes every beat as
# someone speaks. If the SAME text turns up at two separate timestamps, that's a
# persistent overlay — the strongest signal available that it's a real hook, not a
# caption fragment. Ported from general_hooks.py, which hit this exact problem on
# a general (non-niche) corpus, where most videos carry burned-in captions and the
# old "keep the longer read" rule just picked whatever sentence the speaker was
# mid-way through. Same fix, same threshold, applied here too — a persistent
# overlay is the norm for this niche's title-card-style hooks, but nothing stops a
# talking-head reel from carrying the same rolling-caption problem.
STABLE_RATIO = 0.60


def _norm(s):
    """Local copy of hook_search.norm() — hook_search.py imports FROM this module,
    so importing back would be circular. Tiny enough to duplicate."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?']", "", (s or "").lower())).strip()


_COMMON_WORDS = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "of", "for",
                 "with", "is", "are", "was", "were", "you", "your", "i", "my", "this", "that",
                 "it", "its", "just", "how", "what", "when", "where", "why", "who", "did", "do",
                 "does", "me", "we", "he", "she", "they", "not", "be"}
_ALLOWED_PUNCT = set(".,!?'\"‘’“”-:;…()&%$#@/")


def _word_ok(w):
    core = w.strip(".,!?\"‘’“”;:()[]{}*#@%&-…")
    return core.replace("'", "").isalpha() and len(core) >= 2


def is_gibberish(text):
    """True if `text` reads as OCR noise rather than coherent hook text.
    Calibrated against the real corpus (see module docstring) — tightened twice
    after false-positiving on genuine short hooks ("more work. more life.",
    "WHEN?", "plan A") before landing on this word/symbol-ratio combination."""
    if not text or not text.strip():
        return True
    words = text.split()
    if not words:
        return True
    special = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in _ALLOWED_PUNCT))
    if special / len(text) > 0.15:
        return True
    ok_words = [w for w in words if _word_ok(w)]
    ratio = len(ok_words) / len(words)
    if len(words) <= 4:
        has_common = any(w.strip(".,!?\"‘’“”;:()[]{}").lower() in _COMMON_WORDS
                         for w in words)
        return ratio < 0.5 and not has_common
    return ratio < 0.6


def shortcode(url):
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else (url or "")[-16:]


def video_url_for(row):
    if row.get("video"):
        return row["video"]
    if not KEY or not row.get("url"):
        return ""
    u = f"https://api.tikhub.io/api/v1/instagram/v1/fetch_post_by_url?post_url={urllib.parse.quote(row['url'])}"
    req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (json.loads(r.read().decode()).get("data", {}) or {}).get("video_url", "")
    except Exception:  # noqa: BLE001
        return ""


def frame(video_url, t, dest):
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", video_url, "-frames:v", "1", "-q:v", "3", dest],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return os.path.exists(dest) and os.path.getsize(dest) > 800


def download_image(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception:  # noqa: BLE001
        return False
    if len(data) < 800:
        return False
    Path(dest).write_bytes(data)
    return True


def ocr(path):
    try:
        out = subprocess.run([OCR_CMD, path], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    # drop UI cruft (handles, follow buttons, counts) — keep hook-shaped lines
    lines = []
    for ln in out.split("\n"):
        s = ln.strip().strip('"').strip()
        if len(s) < 3 or s.startswith("@") or re.fullmatch(r"[\d.,KMviews\s]+", s, re.I):
            continue
        lines.append(s)
    return " ".join(lines)[:160]


def best_hook_from_video(vurl):
    """Try FRAME_TIMES in order, stop at the first coherent read. Falls back to
    the longest gibberish read if nothing coherent turns up (never worse than
    the old single-best-of-2 behavior, just tries harder first).

    Prefers a STABLE read (recurs across two separate frames — see STABLE_RATIO)
    over a merely-coherent one: a repeating chunk of noise (e.g. a watermark) isn't
    a hook either, so stability only counts once coherence has already passed.
    Returns (hook, stable, attempts) — stable=False covers both "coherent but only
    seen once" (still returned — a hook shown once and gone is real, just less
    certain) and "nothing coherent at all" (the old length-only fallback)."""
    tmp = "/tmp/hk_retry.jpg"
    seen = []          # every {"text", "coherent"} attempt, in order
    fallback = None    # first coherent-but-unconfirmed read, kept in case nothing stabilizes
    for t in FRAME_TIMES:
        if not frame(vurl, t, tmp):
            continue
        text = ocr(tmp)
        if not text:
            continue
        coherent = not is_gibberish(text)
        if coherent:
            for prev in seen:
                if prev["coherent"] and difflib.SequenceMatcher(None, _norm(text), _norm(prev["text"])).ratio() >= STABLE_RATIO:
                    return max((text, prev["text"]), key=len), True, len(seen) + 1
            if fallback is None:
                fallback = text
        seen.append({"text": text, "coherent": coherent})
    if fallback is not None:
        return fallback, False, len(seen)
    all_texts = [a["text"] for a in seen]
    return (max(all_texts, key=len) if all_texts else ""), False, len(seen)


def best_hook_from_images(urls, max_images=2):
    """Same coherent-first logic, for Photo/Carousel posts — the hook is almost
    always on the opening slide, so only the first `max_images` are fetched."""
    tmp = "/tmp/hk_img.jpg"
    attempts = []
    for i, url in enumerate(urls[:max_images]):
        if not download_image(url, tmp):
            continue
        text = ocr(tmp)
        if not text:
            continue
        attempts.append(text)
        if not is_gibberish(text):
            return text, i + 1
    return (max(attempts, key=len) if attempts else ""), len(attempts)


def process_row(r, cache):
    """OCR one post (any format), write its cache entry. Returns True if a
    (possibly gibberish) hook was written, False if there was nothing to OCR."""
    code = shortcode(r.get("url", ""))
    fmt = r.get("format", "")
    stable = None  # not applicable to a single image or unrelated carousel slides — video-only signal
    if fmt == "Carousel" and r.get("carousel_urls"):
        hook, tries = best_hook_from_images(r["carousel_urls"])
    elif fmt == "Photo" and r.get("thumbnail"):
        hook, tries = best_hook_from_images([r["thumbnail"]], max_images=1)
    else:
        vurl = video_url_for(r)
        if not vurl:
            cache[code] = {"hook": "", "reason": "no video"}
            return False
        hook, stable, tries = best_hook_from_video(vurl)
    cache[code] = {"hook": hook, "account": r.get("account", ""), "url": r.get("url", ""),
                   "gibberish": is_gibberish(hook) if hook else None, "stable": stable}
    return True


def cmd_scan(args):
    rows = json.loads(Path(args.posts).read_text())
    rows = [r for r in rows
            if (r.get("format") in ("Reel", "TikTok", "Photo", "Carousel") or r.get("video"))][:args.limit]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    new = got = 0
    for r in rows:
        code = shortcode(r.get("url", ""))
        if code in cache:
            continue
        wrote = process_row(r, cache)
        new += 1
        if wrote and cache[code].get("hook"):
            got += 1
        time.sleep(0.1)
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"OCR'd {new} new posts, {got} had readable hook text "
          f"({sum(1 for v in cache.values() if v.get('hook'))} total cached) -> output/hook_texts.json")


def _recent_run_urls(n_runs):
    """URLs from the current top-level posts + the (n_runs - 1) most recent
    archived weeks in dashboard/data.json — "the last N runs" the way a human
    would mean it, not a fixed lookback window (weeks aren't guaranteed to be
    exactly 7 days apart if a run was skipped)."""
    data = json.loads((DASH / "data.json").read_text())
    urls = {p["url"] for p in (data.get("posts") or []) if p.get("url")}
    weeks = sorted((data.get("weeks") or {}).keys(), reverse=True)[: max(n_runs - 1, 0)]
    for wk in weeks:
        urls |= {p["url"] for p in (data["weeks"][wk].get("posts") or []) if p.get("url")}
    return urls


def cmd_retrofit(args):
    if not CACHE.exists():
        sys.exit("No output/hook_texts.json cache to retrofit.")
    cache = json.loads(CACHE.read_text())
    recent_urls = _recent_run_urls(args.retrofit_gibberish)
    recent_codes = {shortcode(u) for u in recent_urls}
    url_by_code = {shortcode(u): u for u in recent_urls}

    # Gibberish (non-empty but noise) OR emptied-out by a failed OCR attempt that
    # wasn't a genuine "no video" skip — both are worth another try. Excludes rows
    # explicitly marked reason=="no video" (nothing was ever found to OCR, retrying
    # won't help) from the empty-hook case.
    stale = [code for code, v in cache.items() if code in recent_codes and (
        (v.get("hook") and is_gibberish(v["hook"]))
        or (not v.get("hook") and v.get("reason") != "no video")
    )]
    print(f"{len(recent_codes)} posts across the last {args.retrofit_gibberish} run(s); "
          f"{len(stale)} have a gibberish-flagged cached hook -> retrying.")
    if not stale:
        return

    # Need each row's full data (video/carousel_urls/thumbnail/account), not just the
    # URL — pull from data.json's posts/weeks the same places _recent_run_urls did.
    # NOTE: this only recovers video posts correctly. A built row's carousel/photo
    # images are LOCAL paths by this point (curate_posts.py already ran; the raw
    # remote carousel_urls scrape.py originally saw aren't preserved anywhere in
    # data.json), so a gibberish Carousel/Photo hook can't be retrofitted this way
    # yet — moot today (the pre-fix scanner never OCR'd those formats at all, so
    # none exist in the cache to find), but will need a real fix once carousel/photo
    # hooks start accumulating from cmd_scan.
    data = json.loads((DASH / "data.json").read_text())
    all_rows = list(data.get("posts") or [])
    for wk in (data.get("weeks") or {}).values():
        all_rows.extend(wk.get("posts") or [])
    row_by_code = {shortcode(r["url"]): r for r in all_rows if r.get("url")}

    fixed = still_bad = missing = 0
    for code in stale:
        r = row_by_code.get(code)
        if not r:
            missing += 1
            continue
        # data.json's `video` field is whatever CDN URL was live when that week's
        # row was written — by retrofit time (days to weeks later) it's expired.
        # video_url_for() trusts row["video"] unconditionally when present, so
        # without this it silently returns a dead link instead of refetching a
        # live one. Caught live: retrofitting DaM-kZ0vatB (the case that started
        # this fix) failed with an empty result until this line was added, despite
        # a fresh fetch_post_by_url() call for the same post working immediately.
        r = {k: v for k, v in r.items() if k != "video"}
        old = cache[code]["hook"]
        del cache[code]
        process_row(r, cache)
        new_hook = cache[code].get("hook", "")
        if new_hook and not is_gibberish(new_hook):
            fixed += 1
            print(f"  FIXED  @{r.get('account',''):<22} {old[:40]!r} -> {new_hook[:60]!r}")
        else:
            still_bad += 1
            print(f"  no fix @{r.get('account',''):<22} {old[:40]!r} -> {new_hook[:60]!r}")
        time.sleep(0.1)
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"\n{fixed} fixed, {still_bad} still gibberish after retry, "
          f"{missing} skipped (row no longer in data.json) -> output/hook_texts.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("posts", nargs="?", help="output/top_posts_<date>.json")
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--retrofit-gibberish", type=int, metavar="N",
                    help="re-OCR gibberish-flagged cache entries from the last N runs, "
                         "instead of scanning a fresh posts file")
    args = ap.parse_args()
    if args.retrofit_gibberish:
        cmd_retrofit(args)
    elif args.posts:
        cmd_scan(args)
    else:
        ap.error("posts file required unless --retrofit-gibberish is given")


if __name__ == "__main__":
    main()
