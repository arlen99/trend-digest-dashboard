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
  original targeted search, but on BOTH TikTok and Instagram (top 8 each platform's
  own relevance order — marginal relevance drops off fast past ~8 in every sample
  checked), OCR every result, and require >=2 to actually match, instead of trusting
  captions. Instagram's search_reels is included even though its search relevance is
  exactly as untrustworthy pre-OCR as TikTok's (checked live 2026-08: no on-screen-text
  field exists in its response either) — it's simply a second raw candidate pool for
  the SAME OCR-then-judge verification, not a shortcut around it.

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
# Grouped so the dashboard can SHOW what "niche-relevant" actually means rather than
# asserting it — provenance.py exports these verbatim to the Hooks method popup.
# The camera/technique terms are ported from tiktok_discover.py's creator-vetting NICHE
# regex: same project, same niche definition, applied to a video instead of a bio.
NICHE_GROUPS = {
    "Travel & destinations": [
        "travel", "adventure", "wander", "landscape", "roadtrip", "backpack", "nomad",
        "explor", "bali", "iceland", "dolomit",
    ],
    "Filmmaking & camera": [
        "cinematic", "cinematograph", "filmmak", "drone", "fpv", "b-roll", "broll",
        "color grading", "colour grading", "colorgrading", "davinci", "gimbal",
        "shot on", "fx3", "fx6", "a7s", "bmpcc",
    ],
    "Underwater": [
        "underwater", "freedive", "freediving", "scuba", "snorkel", "dive", "diving",
    ],
}
NICHE = re.compile(r"travel|cinematic|filmmak|adventure|drone|wander|explor|landscape|"
                   r"roadtrip|backpack|nomad|bali|iceland|dolomit|b-?roll|fpv|"
                   r"underwater|freedivi?|scuba|snorkel|\bdiv(e|ing)\b|"
                   r"colou?r ?grad|davinci|fx3|fx6|a7s|bmpcc|gimbal|shot on|cinematograph",
                   re.I)


def niche_signal(caption, hashtags=()):
    """Is this result actually about OUR niche, not just reusing our phrasing?

    Checks the video's own caption AND its parsed hashtag list. TikTok returns
    hashtags pre-parsed in `text_extra` (name + id + offsets), which is strictly
    better than regexing the raw caption string: a tag buried in an emoji run or
    punctuation soup ("🎨#colorgrading#fyp") is a clean token there, while the raw
    caption can also produce false hits inside ordinary prose ("wandering around"
    reads the same as a deliberate #wander tag). Checked live 2026-08: TikHub
    exposes NO topic/theme classifier on this endpoint — video_labels and
    cover_labels come back empty and video_text is never populated — so caption +
    hashtags is the best structured signal available.
    """
    if NICHE.search(caption or ""):
        return True
    return any(NICHE.search(h or "") for h in hashtags)


def hashtags_of(aweme):
    """Parsed hashtag names from TikTok's own `text_extra` block (see niche_signal)."""
    return [(t.get("hashtag_name") or "") for t in (aweme.get("text_extra") or [])
            if t.get("hashtag_name")]
STOP = {"the","a","an","and","or","but","in","on","at","to","of","for","with","is","are",
        "was","were","you","your","i","my","this","that","it","its","just","how","what",
        "when","where","why","who","pov","did","do","does"}


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?']", "", (s or "").lower())).strip()


# Places deliberately EXCLUDED because they're also ordinary English words and would
# over-merge unrelated hooks: nice, bath, split, turkey, chile, jordan, georgia, mali,
# chad. Under-merging is the safe failure here — a missed merge costs one duplicate
# candidate, a bad merge silently fuses two different templates into one wrong row.
PLACES = {
    "japan", "tokyo", "kyoto", "osaka", "china", "beijing", "shanghai", "vietnam",
    "hanoi", "thailand", "bangkok", "phuket", "korea", "seoul", "singapore", "malaysia",
    "indonesia", "jakarta", "philippines", "manila", "india", "nepal", "bhutan",
    "cambodia", "laos", "taiwan", "mongolia", "maldives", "srilanka", "dubai",
    "abudhabi", "qatar", "doha", "oman", "saudi", "jordan's", "egypt", "cairo",
    "morocco", "marrakech", "tunisia", "kenya", "tanzania", "zanzibar", "uganda",
    "rwanda", "namibia", "botswana", "madagascar", "seychelles", "mauritius",
    "iceland", "norway", "sweden", "finland", "denmark", "scotland", "ireland",
    "england", "london", "wales", "france", "paris", "spain", "barcelona", "madrid",
    "portugal", "lisbon", "porto", "italy", "rome", "milan", "venice", "florence",
    "tuscany", "sicily", "greece", "athens", "santorini", "mykonos", "croatia",
    "dubrovnik", "slovenia", "austria", "vienna", "switzerland", "zurich", "germany",
    "berlin", "munich", "netherlands", "amsterdam", "belgium", "poland", "czechia",
    "prague", "hungary", "budapest", "romania", "bulgaria", "albania", "montenegro",
    "dolomites", "alps", "patagonia", "peru", "cusco", "bolivia", "brazil", "rio",
    "argentina", "colombia", "ecuador", "uruguay", "mexico", "tulum", "oaxaca",
    "guatemala", "belize", "panama", "costarica", "cuba", "jamaica", "bahamas",
    "canada", "vancouver", "toronto", "banff", "alaska", "hawaii", "california",
    "colorado", "utah", "arizona", "florida", "texas", "seattle", "portland",
    "denver", "chicago", "boston", "miami", "vegas", "australia", "sydney",
    "melbourne", "brisbane", "perth", "tasmania", "zealand", "queenstown",
    "auckland", "fiji", "tahiti", "bali", "lombok", "sumatra", "java", "borneo",
}
_MULTI_PLACES = ["new zealand", "new york", "south africa", "cape town", "costa rica",
                 "sri lanka", "hong kong", "abu dhabi", "las vegas", "los angeles",
                 "san francisco", "united states", "saudi arabia", "south korea",
                 "faroe islands", "canary islands", "machu picchu"]


def skeletonize(s):
    """Strip the SPECIFICS out of a hook so the reusable template underneath is what
    gets compared.

    "$35 hotel room in China" and "$12 hotel room in Vietnam" are one hook template
    used twice, but compared literally they share almost nothing — the price and the
    country are exactly the parts that change on every reuse. Replacing them with
    placeholders makes both read "<price> hotel room in <loc>", so clustering merges
    them and the lexical pre-filter stops treating "35"/"china" as required words.

    Used for MATCHING only — the displayed hook text stays the real, specific line.
    """
    t = (s or "").lower()
    # Numbers first, while their commas/decimals are still intact — stripping
    # punctuation earlier would split "8,000" into two separate numbers.
    t = re.sub(r"[$€£¥]\s?\d[\d,.]*\s?[km]?\b", " <price> ", t)
    t = re.sub(r"\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars?|euros?|pounds?|bucks?)\b", " <price> ", t)
    t = re.sub(r"\b\d[\d,.]*\b", " <num> ", t)
    t = re.sub(r"[^\w\s?'<>]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for p in _MULTI_PLACES:  # before single-word pass, so "new zealand" isn't half-matched
        t = t.replace(p, " <loc> ")
    # Match places on the bare word — "bali??" and "bali" are the same place.
    out = []
    for w in t.split():
        bare = w.strip("?'")
        out.append("<loc>" if bare in PLACES else w)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def sig_words(s):
    """Significant words for the cheap lexical pre-filter — drop stopwords/short words
    so two hooks sharing only "the"/"you"/etc. don't look related. Runs on the
    SKELETON so a differing price/place can't stop two uses of one template matching."""
    return {w for w in skeletonize(s).split() if len(w) >= 4 and w not in STOP
            and not w.startswith("<")}


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
        out.append({"hook": h, "account": v.get("account", ""), "url": v.get("url", ""),
                    "stable": v.get("stable")})
    # Stable reads (the same text persisted across two separate frames — see
    # hook_text.py's STABLE_RATIO) go first, so a cluster's REPRESENTATIVE text —
    # whichever row arrives first in cluster_internal() — prefers a confirmed
    # overlay over a rolling caption that happened to read as coherent once.
    # Ported from general_hooks.py's identical ordering for the same reason.
    out.sort(key=lambda r: r["stable"] is True, reverse=True)
    return out


def cluster_internal(raw, threshold=0.82):
    """Incremental near-duplicate clustering: compare each new hook only against
    canonical cluster representatives found SO FAR (O(n*k), not O(n^2)) — the same
    "compare against known clusters, not every prior item" shape hook_momentum's
    online-clustering cousins use. difflib.SequenceMatcher is stdlib, no new
    dependency, and plenty precise for "same OCR pass, minor read variance"."""
    clusters = []  # each: {"hook": canonical text, "examples": [{"account","url"}], "accounts": set}
    for r in raw:
        n = skeletonize(r["hook"])  # compare templates, not the specifics (see skeletonize)
        best, best_score = None, 0.0
        for c in clusters:
            score = difflib.SequenceMatcher(None, n, skeletonize(c["hook"])).ratio()
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
            corpus.append({"text": text, "account": p.get("account", ""), "url": p.get("url", ""),
                            "caption": p.get("caption", "")})
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


def search_ig(phrase):
    """Instagram's search_reels — same TIKHUB_TOKEN, no separate credential.

    Its own search relevance is caption-driven, same as TikTok's (checked live 2026-08:
    no on-screen-text field exists in the response — media_overlay_info is null on
    every result, no accessibility_caption, nothing like TikTok's search_highlight).
    So this is NOT a shortcut around OCR — it's a second raw candidate pool, verified
    exactly like TikTok's: OCR'd, then judged same-template + niche by the code below.
    Worth including anyway since it's genuinely independent evidence from a platform
    your own Stage-0 corpus doesn't already live on... except it DOES: your internal
    hooks are sourced from Instagram, so a hit here is weaker proof of a real trend
    than a TikTok hit (could just be your own network's neighborhood), which is why
    this stays a same-tier add to Stage 2 rather than a replacement for TikTok.
    """
    global th_calls
    q = urllib.parse.quote(phrase)
    u = f"https://api.tikhub.io/api/v1/instagram/v2/search_reels?keyword={q}"
    for _ in range(4):
        try:
            req = urllib.request.Request(u, headers={"Authorization": "Bearer " + KEY, "User-Agent": UA, "accept": "application/json"})
            th_calls += 1
            with urllib.request.urlopen(req, timeout=60) as r:
                # TikHub double-wraps this endpoint's payload (data.data.items), unlike
                # the single data.search_item_list on the TikTok endpoint above — verified
                # live 2026-08 by inspecting the raw response after this silently returned
                # empty on every query.
                outer = (json.loads(r.read().decode()).get("data") or {})
                return (outer.get("data") or {}).get("items") or []
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return []


def _ocr_tiktok_results(phrase):
    items = [it.get("aweme_info") or {} for it in search(phrase)][:8]
    out = []
    for a in items:
        vurl = ((a.get("video") or {}).get("play_addr") or {}).get("url_list", [""])[0]
        text = ocr_post(vurl)
        if text:
            author = (a.get("author") or {}).get("unique_id", "")
            aweme_id = a.get("aweme_id", "")
            url = f"https://www.tiktok.com/@{author}/video/{aweme_id}" if author and aweme_id else ""
            out.append({"text": text, "account": author, "url": url,
                       "likes": (a.get("statistics") or {}).get("digg_count", 0),
                       "caption": a.get("desc", ""), "hashtags": hashtags_of(a),
                       "platform": "tiktok"})
        time.sleep(0.2)
    return out


def _ocr_ig_results(phrase):
    items = search_ig(phrase)[:8]
    out = []
    for it in items:
        vurl = it.get("video_url", "")
        text = ocr_post(vurl)
        if text:
            cap = it.get("caption") or {}
            author = ((it.get("user") or {}).get("username")
                      or (cap.get("user") or {}).get("username", ""))
            code = it.get("code", "")
            out.append({"text": text, "account": author,
                       "url": f"https://www.instagram.com/reel/{code}/" if code else "",
                       "likes": it.get("like_count", 0),
                       "caption": cap.get("text", ""), "hashtags": cap.get("hashtags") or [],
                       "platform": "instagram"})
        time.sleep(0.2)
    return out


def stage2_verify(candidate):
    """Top-8 (each platform's own relevance order) from TikTok AND Instagram search,
    OCR'd and Claude-judged — not the raw results trusted on caption keywords."""
    ocrd = _ocr_tiktok_results(candidate["hook"]) + _ocr_ig_results(candidate["hook"])
    if len(ocrd) < 2:
        return [], [], ocrd
    texts = [o["text"] for o in ocrd]
    matched_texts = judge_matches(candidate["hook"], texts)
    # Two INDEPENDENT questions, tracked separately so the dashboard can show both:
    #   template — is this actually the same reusable hook? (Claude's judgment)
    #   niche    — is it about OUR subject? (caption + hashtags)
    # A search on the raw hook phrase pulls in whoever else used those words regardless
    # of subject, so a dating-advice or reaction-meme account can genuinely reuse a
    # template without it being a trend worth surfacing here.
    template = [o for o in ocrd if o["text"] in matched_texts]
    niche = [o for o in template if niche_signal(o.get("caption"), o.get("hashtags"))]
    return niche, template, ocrd


def main():
    ap = argparse.ArgumentParser()
    # Lower than the old design's 40 — each candidate can now cost a real OCR+search
    # pass (Stage 2), not a cheap regex check, and only 8 ever get displayed anyway
    # (curate_trends.py's MAX_HOOK). Ranked by internal reuse first, so the ones worth
    # spending budget on go first regardless of the cap.
    ap.add_argument("--max", type=int, default=20)
    # Always report at least this many candidates, even ones that didn't clear
    # verification. A hook can stay genuinely useful while scoring 0 external niche
    # matches — TikTok search is non-deterministic (two calls for one phrase return
    # different sets, confirmed live 2026-08), the niche gate is deliberately strict,
    # and last week's still-relevant hooks shouldn't vanish because this week's draw
    # was unlucky. Unconfirmed rows ship with their real counts and an explicit
    # "unconfirmed" badge rather than being silently dropped.
    ap.add_argument("--min-rows", type=int, default=10)
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

    rows = []
    stage1_hits = stage2_hits = 0
    for c in clusters:
        sw = sig_words(c["hook"])
        shortlist = [t for t in corpus if sw & sig_words(t["text"])][:10]
        s1_niche, s1_template = [], []
        if shortlist:
            matched_texts = judge_matches(c["hook"], [t["text"] for t in shortlist])
            s1_template = [t for t in shortlist if t["text"] in matched_texts]
            # The keyword scan is niche-sourced BY CONSTRUCTION (keyword_posts.py only
            # searches this niche's terms), so a template match there is already a niche
            # match — no caption gate needed, unlike the raw-phrase search in Stage 2.
            s1_niche = s1_template

        s2_niche, s2_template, s2_checked = [], [], []
        verified_by = None
        if s1_niche:
            verified_by = "keyword_scan"; stage1_hits += 1
        elif ANTHROPIC_KEY:
            # Without a key, judge_matches() always returns [] — Stage 2's 8 OCR
            # downloads per candidate would be pure waste (confirmed nothing local
            # testing: ~15min for 15 candidates that could never be confirmed anyway).
            s2_niche, s2_template, s2_checked = stage2_verify(c)
            if len(s2_niche) >= 2:
                verified_by = "targeted_search"; stage2_hits += 1
        if not verified_by and len(c["accounts"]) >= 3:
            verified_by = "internal_only"

        niche_ext = s1_niche + s2_niche
        template_ext = s1_template + s2_template
        ext_urls = [e["url"] for e in niche_ext if e.get("url")]
        int_urls = [e["url"] for e in c["examples"] if e.get("url")]
        examples = (ext_urls + int_urls)[:4]  # external proof first — it's the real evidence
        likes_pool = [e.get("likes", 0) for e in s2_checked] or [0]
        rows.append({
            "hook": c["hook"],
            "internal_creators": len(c["accounts"]),
            "distinct_creators": len(c["accounts"] | {e.get("account", "") for e in niche_ext}),
            "external_matches": len(niche_ext),
            # How many checked results reused the TEMPLATE, regardless of subject.
            "template_matches": len(template_ext),
            "results": len(shortlist) + len(s2_checked),
            # Of those, how many were also about THIS niche. Always <= template_matches
            # and <= results, so the dashboard ratio can never exceed 100%.
            "niche_hits": len(niche_ext),
            "max_likes": max(likes_pool),
            "median_likes": sorted(likes_pool)[len(likes_pool) // 2],
            "verified_by": verified_by or "unconfirmed",
            "confirmed": bool(verified_by),
            "examples": examples,
        })
        print(f"  [{verified_by or 'unconfirmed':15}] {c['hook'][:44]!r}  "
              f"(internal={len(c['accounts'])}, template={len(template_ext)}, niche={len(niche_ext)})")

    # Confirmed first, then best-evidenced unconfirmed to fill out --min-rows.
    rows.sort(key=lambda t: (t["confirmed"], t["niche_hits"], t["template_matches"],
                             t["internal_creators"]), reverse=True)
    n_conf = sum(1 for r in rows if r["confirmed"])
    kept = rows[: max(n_conf, min(args.min_rows, len(rows)))]
    stamp = datetime.now().strftime("%Y-%m-%d")
    (OUT / f"hook_trends_{stamp}.json").write_text(json.dumps(kept, indent=2, ensure_ascii=False))
    print(f"\n{n_conf} confirmed hook trends ({stage1_hits} via keyword-scan, "
          f"{stage2_hits} via targeted-search fallback); writing {len(kept)} rows "
          f"(min-rows={args.min_rows}) -> output/hook_trends_{stamp}.json")
    import cost_tracker
    cost_tracker.record("hook_search", tikhub_calls=th_calls, claude_calls=claude_calls,
                        claude_input_tokens=claude_input_tokens, claude_output_tokens=claude_output_tokens)


if __name__ == "__main__":
    main()
