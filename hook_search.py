#!/usr/bin/env python3
"""
Validate trending hooks — by independent-source clustering, not search-relevance trust.

REPLACES the earlier design (kept a targeted TikTok search per candidate, then trusted
a static niche-keyword regex against the RESULTS' captions as "validation"). Verified
live 2026-08: TikTok's own search_highlight field showed every "confirmed" result for
"Nice have fun in Bali...Bali??" matched on the single word "Bali" in the caption, not
the hook. Searching a phrase and trusting what comes back conflates "topically related"
with "actually reused" — precisely the bug. Real trend/meme-detection research (e.g.
Aiello et al., "Clustering memes in social media") validates the other way: cluster
independently-sourced text by similarity and let cluster size BE the signal, rather
than asking one search engine's relevance ranking to also serve as the verifier.

Three stages, cheapest/most-certain first — a candidate only pays for the next stage
if the previous one found nothing:

  STAGE 0 (free) — cluster OUR OWN OCR'd hooks (hook_text.py's output, ~480 posts/wk
  from the tracked niche) into canonical candidates, merging near-identical OCR
  variants. A hook used by >=2 DISTINCT creators in our own scan is itself a real
  signal, at zero extra cost.

  STAGE 1 (~50/wk, already-fetched, independent of our tracked accounts) — OCR
  keyword_posts.py's existing broad TikTok keyword-scan output (same infra, no new
  scraping) and check each Stage-0 candidate against it. This is the "found in the
  wild, unbiased by search ranking" signal.

  STAGE 2 (bounded fallback, only for candidates Stage 0-1 didn't confirm) — the
  original targeted TikTok search, but OCR the top 8 results (TikTok's own top-ranked,
  not all 20 — marginal relevance drops off fast past ~8 in every sample checked) and
  require >=2 of those 8 to actually match, instead of trusting captions.

"Match" in stages 1-2 means Claude judges the OCR'd/captioned text as the SAME
reusable hook TEMPLATE as the candidate (paraphrase-tolerant: different price, city,
or wording but the same structural format) — not just topically similar. Generic
text-embedding similarity was considered and rejected for this: embeddings are
calibrated for topical/meaning closeness, which is the exact same conflation that
caused the original bug, just moved into the similarity metric instead of the search
engine. Claude, already used elsewhere in this pipeline for genuinely judgment-shaped
tasks (curate_posts.py, curate_trends.py), makes the "same template, not just same
topic" call directly, batched (one call per candidate against its lexically-shortlisted
Stage-1 texts, one per candidate still unconfirmed against its Stage-2 OCR batch).

Usage:
  set -a && . ./.env && set +a
  python3 hook_search.py [--max 20]
Output: output/hook_trends_<date>.json (confirmed hook trends, ranked).
Env: TIKHUB_TOKEN, ANTHROPIC_API_KEY.
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hook_text import video_url_for, frame as ocr_frame, ocr as run_ocr  # reuse, don't duplicate

ROOT = Path(__file__).parent
OUT = ROOT / "output"
KEY = os.environ.get("TIKHUB_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-5"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
NICHE = re.compile(r"travel|cinematic|filmmak|adventure|drone|wander|explore|landscape|"
                   r"roadtrip|backpack|nomad|bali|iceland|dolomit|b-?roll|fpv", re.I)
STOP = {"the","a","an","and","or","but","in","on","at","to","of","for","with","is","are",
        "was","were","you","your","i","my","this","that","it","its","just","how","what",
        "when","where","why","who","pov","did","do","does"}


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?']", "", (s or "").lower())).strip()


def sig_words(s):
    """Significant words for the cheap lexical pre-filter — drop stopwords/short words
    so two hooks sharing only "the"/"you"/etc. don't look related."""
    return {w for w in norm(s).split() if len(w) >= 4 and w not in STOP}


# ---------------------------------------------------------------------------
# STAGE 0 — incremental fuzzy clustering of our own OCR corpus (no API calls)
# ---------------------------------------------------------------------------
def raw_candidates():
    f = OUT / "hook_texts.json"
    if not f.exists():
        return []
    cache = json.loads(f.read_text())
    out = []
    for v in cache.values():
        h = (v.get("hook") or "").strip()
        if not h:
            continue
        n = norm(h)
        words = n.split()
        if not (3 <= len(words) <= 12) or len(n) < 12 or not n.isascii():
            continue
        realish = sum(1 for w in words if len(w) >= 2 and w.isalpha())
        if realish / len(words) < 0.7:
            continue
        out.append({"hook": h, "account": v.get("account", ""), "url": v.get("url", "")})
    return out


def cluster_internal(raw, threshold=0.82):
    """Incremental near-duplicate clustering: compare each new hook only against
    canonical cluster representatives found SO FAR (O(n*k), not O(n^2)) — the same
    "compare against known clusters, not every prior item" shape hook_momentum's
    online-clustering cousins use. difflib.SequenceMatcher is stdlib, no new
    dependency, and plenty precise for "same OCR pass, minor read variance"."""
    clusters = []  # each: {"hook": canonical text, "examples": [{"account","url"}], "accounts": set}
    for r in raw:
        n = norm(r["hook"])
        best, best_score = None, 0.0
        for c in clusters:
            score = difflib.SequenceMatcher(None, n, norm(c["hook"])).ratio()
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= threshold:
            best["examples"].append(r)
            best["accounts"].add(r["account"])
        else:
            clusters.append({"hook": r["hook"], "examples": [r], "accounts": {r["account"]}})
    return clusters


# ---------------------------------------------------------------------------
# Shared OCR helper for arbitrary (non-our-own) posts — Stage 1 + Stage 2
# ---------------------------------------------------------------------------
def ocr_post(video_url):
    """Two-frame OCR of an arbitrary video URL, same approach hook_text.py uses for
    our own posts. Best-effort: "" on any failure, never raises."""
    if not video_url:
        return ""
    tmp_a, tmp_b = "/tmp/hs_a.jpg", "/tmp/hs_b.jpg"
    texts = []
    for t, dest in ((2.0, tmp_a), (4.0, tmp_b)):
        if ocr_frame(video_url, t, dest):
            texts.append(run_ocr(dest))
    return max(texts, key=len) if texts else ""


# ---------------------------------------------------------------------------
# Claude semantic judgment — "same hook TEMPLATE", not "same topic"
# ---------------------------------------------------------------------------
JUDGE_SCHEMA = {
    "name": "judge_hook_matches",
    "description": "Identify which candidate texts are genuinely the SAME reusable hook "
                    "template as the anchor hook.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array", "items": {"type": "integer"},
                "description": "0-based indices into the candidate list that are the "
                                "SAME structural template as the anchor — paraphrases "
                                "with a different price/city/specific wording still "
                                "count. Do NOT include text that's merely on the same "
                                "topic/destination without sharing the anchor's actual "
                                "structure (e.g. two different Bali videos are not a "
                                "match just because both mention Bali).",
            },
        },
        "required": ["matches"],
    },
}

claude_calls = 0
claude_input_tokens = 0
claude_output_tokens = 0


def judge_matches(anchor, texts):
    """texts: list of candidate strings (already lexically pre-filtered/shortlisted).
    Returns the subset of `texts` Claude judges as the same template as `anchor`.
    Best-effort: [] on any failure (a missed match just falls through to the next
    stage, or the candidate stays unconfirmed — never crashes the run)."""
    global claude_calls, claude_input_tokens, claude_output_tokens
    if not texts or not ANTHROPIC_KEY:
        return []
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
    prompt = (f"Anchor hook: {anchor!r}\n\nCandidate texts:\n{numbered}\n\n"
              "Which candidates are the same reusable hook template as the anchor?")
    body = {
        "model": MODEL, "max_tokens": 200,
        "tools": [JUDGE_SCHEMA], "tool_choice": {"type": "tool", "name": "judge_hook_matches"},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return []
    claude_calls += 1
    usage = resp.get("usage") or {}
    claude_input_tokens += usage.get("input_tokens", 0)
    claude_output_tokens += usage.get("output_tokens", 0)
    idx = []
    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            idx = block.get("input", {}).get("matches", [])
    return [texts[i] for i in idx if isinstance(i, int) and 0 <= i < len(texts)]


# ---------------------------------------------------------------------------
# STAGE 1 — independent TikTok keyword-scan corpus (already fetched, ~50/wk)
# ---------------------------------------------------------------------------
def latest_keyword_posts():
    fs = sorted(glob.glob(str(OUT / "keyword_posts_*.json")), key=os.path.getmtime)
    if not fs:
        return []
    try:
        return json.loads(Path(fs[-1]).read_text()) or []
    except Exception:  # noqa: BLE001
        return []


def stage1_corpus():
    """OCR every post in the latest keyword scan once, up front — shared across all
    candidates, not re-fetched per-candidate (that's what made the old design
    expensive at scale)."""
    posts = latest_keyword_posts()
    corpus = []
    for p in posts:
        vurl = p.get("video") or video_url_for(p)
        text = ocr_post(vurl)
        if text:
            corpus.append({"text": text, "account": p.get("account", ""), "url": p.get("url", "")})
        time.sleep(0.1)
    return corpus


# ---------------------------------------------------------------------------
# STAGE 2 — bounded targeted-search fallback, OCR-verified
# ---------------------------------------------------------------------------
th_calls = 0


def search(phrase):
    global th_calls
    q = urllib.parse.quote(phrase)
    u = f"https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_video_search_result?keyword={q}&count=20&sort_type=0&publish_time=0"
    for _ in range(4):
        try:
            req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
            th_calls += 1
            with urllib.request.urlopen(req, timeout=60) as r:
                return (json.loads(r.read().decode()).get("data") or {}).get("search_item_list") or []
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return []


def stage2_verify(candidate):
    """Top-8 (TikTok's own relevance order) of the targeted search, OCR'd and
    Claude-judged — not the raw 20 trusted on caption keywords."""
    items = [it.get("aweme_info") or {} for it in search(candidate["hook"])][:8]
    ocrd = []
    for a in items:
        vurl = ((a.get("video") or {}).get("play_addr") or {}).get("url_list", [""])[0]
        text = ocr_post(vurl)
        if text:
            author = (a.get("author") or {}).get("unique_id", "")
            aweme_id = a.get("aweme_id", "")
            url = f"https://www.tiktok.com/@{author}/video/{aweme_id}" if author and aweme_id else ""
            likes = (a.get("statistics") or {}).get("digg_count", 0)
            ocrd.append({"text": text, "account": author, "url": url, "likes": likes})
        time.sleep(0.2)
    if len(ocrd) < 2:
        return [], ocrd
    texts = [o["text"] for o in ocrd]
    matched_texts = judge_matches(candidate["hook"], texts)
    matched = [o for o in ocrd if o["text"] in matched_texts]
    return matched, ocrd


def main():
    ap = argparse.ArgumentParser()
    # Lower than the old design's 40 — each candidate can now cost a real OCR+search
    # pass (Stage 2), not a cheap regex check, and only 8 ever get displayed anyway
    # (curate_trends.py's MAX_HOOK). Ranked by internal reuse first, so the ones worth
    # spending budget on go first regardless of the cap.
    ap.add_argument("--max", type=int, default=20)
    args = ap.parse_args()
    if not KEY:
        raise SystemExit("TIKHUB_TOKEN not set.")
    if not ANTHROPIC_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set — stages 1-2 semantic matching will "
              "find nothing (Claude judgment is required); only Stage-0-internal "
              "candidates can be confirmed this run.", file=sys.stderr)

    raw = raw_candidates()
    clusters = cluster_internal(raw)
    # Rank by internal reuse first (free signal) so the most-promising candidates
    # spend the external-verification budget first if --max caps the run short.
    clusters.sort(key=lambda c: len(c["accounts"]), reverse=True)
    clusters = clusters[: args.max]
    print(f"Stage 0: {len(raw)} OCR'd hooks -> {len(clusters)} canonical candidates "
          f"({sum(1 for c in clusters if len(c['accounts']) >= 2)} used by >=2 of our own creators)")

    print("Stage 1: OCR'ing the latest keyword-scan corpus...")
    corpus = stage1_corpus()
    print(f"  {len(corpus)} independently-sourced texts OCR'd")

    confirmed = []
    stage1_hits = stage2_hits = 0
    for c in clusters:
        sw = sig_words(c["hook"])
        shortlist = [t for t in corpus if sw & sig_words(t["text"])][:10]
        s1_matches = []
        if shortlist:
            matched_texts = judge_matches(c["hook"], [t["text"] for t in shortlist])
            s1_matches = [t for t in shortlist if t["text"] in matched_texts]

        s2_matches, s2_checked = [], []
        verified_by = None
        if s1_matches:
            verified_by = "keyword_scan"; stage1_hits += 1
        elif ANTHROPIC_KEY:
            # Without a key, judge_matches() always returns [] — Stage 2's 8 OCR
            # downloads per candidate would be pure waste (confirmed nothing local
            # testing: ~15min for 15 candidates that could never be confirmed anyway).
            s2_matches, s2_checked = stage2_verify(c)
            if len(s2_matches) >= 2:
                verified_by = "targeted_search"; stage2_hits += 1

        external = s1_matches + s2_matches
        if not (verified_by or len(c["accounts"]) >= 3):
            continue  # neither externally confirmed nor a strong-enough internal-only signal

        ext_urls = [e["url"] for e in external if e.get("url")]
        int_urls = [e["url"] for e in c["examples"] if e.get("url")]
        examples = (ext_urls + int_urls)[:4]  # external proof first — it's the real evidence
        likes_pool = [e.get("likes", 0) for e in s2_checked] or [0]
        confirmed.append({
            "hook": c["hook"],
            "internal_creators": len(c["accounts"]),
            "distinct_creators": len(c["accounts"] | {e.get("account", "") for e in external}),
            "external_matches": len(external),
            "results": len(shortlist) + len(s2_checked),
            # Confirmed EXTERNAL matches only — mixing internal_creators in here would
            # let niche_hits exceed results (an internal_only-confirmed hook has
            # accounts >= 3 but 0 external matches), producing a nonsensical >100%
            # "niche match" ratio on the dashboard table. internal_creators is its own
            # field for exactly that case.
            "niche_hits": len(external),
            "max_likes": max(likes_pool),
            "median_likes": sorted(likes_pool)[len(likes_pool) // 2],
            "verified_by": verified_by or "internal_only",
            "examples": examples,
        })
        print(f"  [{verified_by or 'internal_only':15}] {c['hook'][:48]!r}  "
              f"(internal={len(c['accounts'])}, external={len(external)})")

    confirmed.sort(key=lambda t: (t["external_matches"], t["internal_creators"]), reverse=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"hook_trends_{stamp}.json").write_text(json.dumps(confirmed, indent=2, ensure_ascii=False))
    print(f"\n{len(confirmed)} confirmed hook trends ({stage1_hits} via keyword-scan, "
          f"{stage2_hits} via targeted-search fallback) -> output/hook_trends_{stamp}.json")
    import cost_tracker
    cost_tracker.record("hook_search", tikhub_calls=th_calls, claude_calls=claude_calls,
                        claude_input_tokens=claude_input_tokens, claude_output_tokens=claude_output_tokens)


if __name__ == "__main__":
    main()
