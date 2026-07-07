#!/usr/bin/env python3
"""
Automated Trend Radar card writing — the step that used to require a human/Claude
session turning trends.py's raw evidence packs into actual displayed cards (name,
format description, trigger, "Ride it" copy). Runs fully automatically (no review
gate), same pattern as curate_posts.py.

Two evidence sources, matching how the board was always hand-built:
  - AUDIO-anchored: trends.py's "audio" array (output/trends_<date>.json) — proper
    week-over-week momentum already computed there.
  - HOOK-anchored: hook_search.py's TikTok-search-validated output
    (output/hook_trends_<date>.json) — richer signal than trends.py's own raw hook
    clustering (niche relevance + real engagement, not just OCR similarity). Momentum
    isn't computed there, so this script compares against the previous week's
    hook_trends file itself (by normalized hook text) to derive one.

For each evidence pack: fetch a representative sample reel's thumbnail (same
og:image scraping fetch_thumbs.py uses) for visual context, then ask Claude to
name the trend, describe its visual/structural template, tag the emotional
trigger, and write an actionable "Ride it" recipe. Claude can also mark a pack
unusable (e.g. a hook cluster that's really just OCR noise) — facts (creators,
uses, momentum, examples) always come from the evidence, never invented.

Usage:
  set -a && . ./.env && set +a
  python3 curate_trends.py output/trends_<date>.json output/hook_trends_<date>.json
Env: ANTHROPIC_API_KEY (required).
"""
import base64
import glob
import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
DASH = ROOT / "dashboard"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-5"
MAX_AUDIO = 10
MAX_HOOK = 8
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
OG = re.compile(r'<meta property="og:image" content="([^"]+)"')
TRIGGERS = ["Awe", "Desire/Wanderlust", "Ego", "Nostalgia", "Relatability", "Surprise"]

TOOL_SCHEMA = {
    "name": "write_trend_card",
    "description": "Turn a trend evidence pack (a template multiple creators are copying) into a Trend Radar card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "usable": {"type": "boolean", "description": "False if this is actually noise (e.g. a hook cluster that's just OCR garble or too generic/unspecific to be a real template), not a genuine repeatable trend."},
            "skip_reason": {"type": "string", "description": "If usable=false, a one-line reason."},
            "name": {"type": "string", "description": "A short, punchy trend name (not the raw anchor/hook text verbatim) — under 8 words."},
            "format": {"type": "string", "description": "1-2 sentences describing the visual/structural template creators are copying — what the shot/edit/caption actually looks like, judged from the sample image(s) and hook text, not just restating the anchor."},
            "trigger": {"type": "string", "enum": TRIGGERS, "description": "The single dominant emotional trigger driving engagement."},
            "howTo": {"type": "string", "description": "One actionable sentence telling the reader exactly how to execute this trend themselves — a concrete recipe, not generic advice."},
        },
        "required": ["usable"],
    },
}


def img_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def call_claude(images: list, text: str) -> dict:
    body = {
        "model": MODEL, "max_tokens": 400,
        "tools": [TOOL_SCHEMA], "tool_choice": {"type": "tool", "name": "write_trend_card"},
        "messages": [{"role": "user", "content": images + [{"type": "text", "text": text}]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input", {})
    return {"usable": False, "skip_reason": "no tool_use in response"}


def fetch_thumb(url: str, dest: Path) -> bool:
    """Prefer an already-self-hosted thumb (board post/curated), else scrape og:image."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
        m = OG.search(html)
        if not m:
            return False
        img_url = m.group(1).replace("&amp;", "&")
        req2 = urllib.request.Request(img_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req2, timeout=20) as r:
            data = r.read()
        dest.write_bytes(data)
        return dest.stat().st_size > 1000
    except Exception:  # noqa: BLE001
        return False


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (s or "").lower())).strip()


def hook_momentum(cur_hooks):
    """Compare against the previous hook_trends_*.json (by normalized hook text) —
    hook_search.py doesn't track this itself, unlike trends.py's audio detector."""
    files = sorted(glob.glob(str(OUT / "hook_trends_*.json")))
    if len(files) < 2:
        return {norm(h["hook"]): "new" for h in cur_hooks}
    prev = json.loads(Path(files[-2]).read_text())
    prevmap = {norm(p["hook"]): p.get("niche_hits", 0) for p in prev}
    out = {}
    for h in cur_hooks:
        key = norm(h["hook"])
        was = prevmap.get(key)
        out[key] = "new" if was is None else ("rising" if h["niche_hits"] > was else "steady" if h["niche_hits"] == was else "cooling")
    return out


def main() -> None:
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY not set — add it to .env (local) and as a repo secret (CI).")
    trends_path = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob(str(OUT / "trends_*.json")), default="")
    hooks_path = sys.argv[2] if len(sys.argv) > 2 else max(glob.glob(str(OUT / "hook_trends_*.json")), default="")
    if not trends_path:
        sys.exit("No output/trends_<date>.json found. Run trends.py first.")

    audio = json.loads(Path(trends_path).read_text()).get("audio", [])
    audio.sort(key=lambda c: (c["creators"], c["uses"]), reverse=True)
    audio = audio[:MAX_AUDIO]

    hooks = json.loads(Path(hooks_path).read_text()) if hooks_path and Path(hooks_path).exists() else []
    hooks.sort(key=lambda c: (c["niche_hits"], c["max_likes"]), reverse=True)
    hooks = hooks[:MAX_HOOK]
    momentum_map = hook_momentum(hooks)

    cards = []
    tmp = "/tmp/trend_thumb.jpg"
    for c in audio:
        samples = c.get("samples") or []
        if not samples:
            print(f"  [audio] {c['anchor'][:40]} -> no sample reels, skipping"); continue
        images = []
        for s in samples[:2]:
            if fetch_thumb(s, Path(tmp)):
                images.append(img_block(Path(tmp)))
        if not images:
            print(f"  [audio] {c['anchor'][:40]} -> no thumbnail available, skipping"); continue
        prompt = (
            f"Type: audio-anchored trend\nSong/anchor: {c['anchor']}\n"
            f"Used by {c['creators']} niche creators, {c['uses']} times, momentum: {c['momentum']}\n"
            f"Accounts using it: {', '.join(c.get('accounts', [])[:6])}\n\n"
            "This is a candidate for the Trend Radar on a travel & cinematic filmmaking "
            "reference board — a repeatable TEMPLATE creators are copying, anchored to this "
            "song. Judge the visual template from the sample image(s) and write the card."
        )
        try:
            result = call_claude(images, prompt)
        except Exception as e:  # noqa: BLE001
            print(f"  [audio] {c['anchor'][:40]} -> API error: {e}"); continue
        if not result.get("usable", True):
            print(f"  [audio] {c['anchor'][:40]} -> skipped ({result.get('skip_reason','no reason')})"); continue
        cards.append({
            "type": "audio", "name": result.get("name", c["anchor"]), "anchor": c["anchor"],
            "format": result.get("format", ""), "trigger": result.get("trigger") or TRIGGERS[0],
            "momentum": c["momentum"], "creators": c["creators"], "uses": c["uses"],
            "examples": samples[:4], "howTo": result.get("howTo", ""),
        })
        print(f"  [audio] {c['anchor'][:40]} -> {result.get('name','')}")

    for h in hooks:
        samples = h.get("examples") or []
        if not samples:
            print(f"  [hook] {h['hook'][:40]} -> no sample reels, skipping"); continue
        images = []
        for s in samples[:2]:
            if fetch_thumb(s, Path(tmp)):
                images.append(img_block(Path(tmp)))
        if not images:
            print(f"  [hook] {h['hook'][:40]} -> no thumbnail available, skipping"); continue
        prompt = (
            f"Type: hook-anchored trend (on-screen text template, independent of audio)\n"
            f"Representative hook line (OCR'd, may have minor errors): {h['hook']}\n"
            f"Confirmed via live TikTok search: {h['distinct_creators']} distinct creators in the "
            f"top 20 results, {h['niche_hits']} of them niche-relevant, top result {h['max_likes']:,} likes.\n\n"
            "This is a candidate for the Trend Radar on a travel & cinematic filmmaking "
            "reference board — a repeatable on-screen HOOK TEMPLATE creators are copying, "
            "independent of any specific song. Judge the visual template from the sample "
            "image(s) and write the card. Mark unusable if the hook text is really just OCR "
            "noise rather than a genuine repeatable phrase."
        )
        try:
            result = call_claude(images, prompt)
        except Exception as e:  # noqa: BLE001
            print(f"  [hook] {h['hook'][:40]} -> API error: {e}"); continue
        if not result.get("usable", True):
            print(f"  [hook] {h['hook'][:40]} -> skipped ({result.get('skip_reason','no reason')})"); continue
        cards.append({
            "type": "hook", "name": result.get("name", h["hook"][:40]), "anchor": h["hook"],
            "format": result.get("format", ""), "trigger": result.get("trigger") or TRIGGERS[0],
            "momentum": momentum_map.get(norm(h["hook"]), "new"), "creators": h["distinct_creators"], "uses": h["results"],
            "examples": samples[:4], "howTo": result.get("howTo", ""),
            "note": f"Validated via TikTok search: {h['distinct_creators']} creators, "
                    f"♥{h['max_likes']:,} top, {h['niche_hits']} niche-relevant results.",
        })
        print(f"  [hook] {h['hook'][:40]} -> {result.get('name','')}")

    print(f"\n{len(cards)}/{len(audio)+len(hooks)} evidence packs became cards.")
    data = json.loads((DASH / "data.json").read_text())
    data["trends"] = cards
    data["generatedHooks"] = data.get("generatedHooks", [])  # unrelated field, left untouched
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {len(cards)} Trend Radar cards into dashboard/data.json.")


if __name__ == "__main__":
    main()
