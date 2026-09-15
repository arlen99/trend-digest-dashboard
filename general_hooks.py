#!/usr/bin/env python3
"""
GENERAL trending hook templates — OCR the on-screen hook out of general_corpus.py's
videos, cluster them into reusable TEMPLATES, and rank by independent reuse.

Relationship to hook_search.py: the clustering/judging machinery there is already
subject-agnostic (skeletonize, cluster_internal, judge_matches never mention travel),
so this imports it rather than reimplementing it. What differs is the two places
hook_search.py IS niche-bound:
  · its candidate corpus comes from niche-scoped scrapes -> here it's general_corpus.py
  · confirmation requires niche_signal() (a travel/filmmaking regex on the caption)
    -> here confirmation is STRUCTURAL REUSE ACROSS DISTINCT CREATORS. "Is this about
    travel" is not a meaningful question of a general corpus; "did unrelated creators
    independently reuse this structure" is exactly the trending signal we want.

ROLLING-CAPTION PROBLEM (the reason this doesn't just call hook_text.py):
hook_text.py samples 2s + 4s and keeps the LONGER read. That is tuned for a niche
where the on-screen text IS the hook. In a general corpus, most videos carry burned-in
auto-captions, and the longer read is then just whatever sentence the speaker happened
to be mid-way through — validated live: the top US billboard video OCR'd at 2s as
"certainties in life.", a caption fragment, not a hook.
A hook overlay PERSISTS across the opening seconds; rolling captions change every beat.
So sample three early frames and prefer text that is STABLE across them, recording
`stable` per row so an unstable (probably-caption) read can be down-weighted rather
than silently treated as a hook.

Usage:
  set -a && . ./.env && set +a
  python3 general_hooks.py                            # newest general_corpus_*.json
  python3 general_hooks.py --corpus output/general_corpus_2026-08-22.json --limit 50
Output: output/general_hook_trends_<date>.json  (same row shape hooks_to_dashboard.py reads)
Env: TIKHUB_TOKEN (only for the per-video URL fallback), ANTHROPIC_API_KEY (template judging).
"""
import argparse
import concurrent.futures as cf
import difflib
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hook_embed                                                  # semantic recall (optional tier)
from hook_text import frame as ocr_frame, ocr as run_ocr          # reuse, don't duplicate
from hook_search import skeletonize, sig_words, judge_matches, norm  # subject-agnostic already

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
CACHE = OUT / "general_hook_texts.json"   # video id -> OCR read, so re-runs are free
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")

# Hooks live in the opening beat. 4s (hook_text.py's second sample) is well past it in
# a general corpus and mostly lands on captions, so sample tighter and earlier.
FRAME_TIMES = (0.8, 1.6, 2.6)
STABLE_RATIO = 0.60      # two frames' text this similar => a persistent overlay, not a caption
CLUSTER_RATIO = 0.82     # same threshold hook_search.cluster_internal uses

# Evidence bar. A match only counts as proof the hook is TRENDING if the post carrying
# it is both current and actually got traction. Without this, search returns a long tail
# of dead reposts and the count reads as reuse when it is nothing of the kind — measured
# on the phrase "where are you going dressed like that": the top 5 results were 1, 1, 21,
# 64 and 15 likes, one of them 10 months old. Three near-identical low-engagement reposts
# of one video previously produced a "3 creators reused this" row.
# Applied to the EVIDENCE, not just the displayed examples: filtering only the display
# would leave the headline count propped up by posts too weak to show.
#
# The floor was first set at 10,000, inferred from a sample showing only two bands:
# qualifying posts at 13k+ and dead reposts at 1-64 likes. But the 100-10,000 band was
# never actually observed, so treating the distribution as bimodal read more into that
# sample than it supported. 1,000 still excludes every repost that motivated the bar
# while admitting mid-tier reuse that 10,000 discarded unseen. This is the one threshold
# here not backed by direct measurement — check what it admits before trusting it.
MIN_LIKES = int(os.environ.get("GH_MIN_LIKES", "1000"))
MAX_AGE_DAYS = int(os.environ.get("GH_MAX_AGE_DAYS", "180"))
# OCR is I/O-bound (ffmpeg frame-grabs over the network), so threads help despite the GIL.
WORKERS = int(os.environ.get("GH_WORKERS", "8"))

th_calls = 0


def fresh_video_url(item_id):
    """app/v3 fetch_one_video — the endpoint that actually yields a fetchable URL.

    fetch_videos.py's comment records why: the WEB endpoint strips it (its
    playAddr/downloadAddr are Akamai-signed and 403 to non-browsers, reconfirmed live).
    Only called when a row's carried URL fails, so billboard rows (which ship a working
    videoURL) normally cost nothing extra."""
    global th_calls
    if not (KEY and item_id):
        return ""
    try:
        req = urllib.request.Request(
            f"https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_one_video?aweme_id={item_id}",
            headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
        th_calls += 1
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return ""

    def deep(o):
        if isinstance(o, dict):
            pa = o.get("play_addr") or o.get("download_addr")
            if isinstance(pa, dict) and pa.get("url_list"):
                return pa["url_list"][0]
            for v in o.values():
                if (r := deep(v)):
                    return r
        elif isinstance(o, list):
            for v in o:
                if (r := deep(v)):
                    return r
        return ""
    return deep(d)


def read_hook(video_url, item_id):
    """Three early frames -> (text, stable). See the rolling-caption note in the docstring.

    stable=True means the same text was still on screen a beat later, which is what an
    intentional hook overlay does and what rolling auto-captions do not.

    The unlink() is load-bearing, not tidiness. hook_text.frame() reports success as
    "the destination exists and is >800 bytes", which is only a valid test if the
    destination cannot already exist. Reusing fixed temp paths across a loop breaks
    that: when ffmpeg fails (every web-API URL 403s, so this is the COMMON path, not
    an edge case) the previous video's JPG is still sitting at that path, frame()
    returns True, and the OCR silently attributes the previous video's hook to this
    one. Observed live before this fix: 57 consecutive explore rows, all distinct
    creators, all reporting one identical hook — which then clustered into a fake
    "58 creators used this template" row, the single most confident-looking output of
    the whole run. Delete first so a failed grab reads as a failure."""
    texts = []
    for i, t in enumerate(FRAME_TIMES):
        dest = f"/tmp/gh_{item_id or 'x'}_{i}.jpg"
        try:
            os.unlink(dest)
        except OSError:
            pass
        if ocr_frame(video_url, t, dest):
            txt = (run_ocr(dest) or "").strip()
            if txt:
                texts.append(txt)
        try:
            os.unlink(dest)
        except OSError:
            pass
    if not texts:
        return "", False
    # Pick the read that best repeats across frames; ties fall back to the longest.
    best, best_score = texts[0], 0.0
    for i, a in enumerate(texts):
        for j, b in enumerate(texts):
            if i >= j:
                continue
            s = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
            if s > best_score:
                best_score, best = s, max((a, b), key=len)
    if best_score < STABLE_RATIO:
        return max(texts, key=len), False
    return best, True


# Sponsored-content markers, matched against the OCR'd ON-SCREEN text (not the caption).
# The billboard is an advertiser surface and organic_only=true does not exclude branded
# posts — measured on the validation corpus, 27% of usable reads carried one of these.
# TikTok requires the disclosure to be visible in-video, which is exactly what OCR sees:
# "#DifferinPartner", "#CeraVePartner", "Disney+ Paid promotion for", "#ad", "#АД"
# (Cyrillic — the same disclosure in other locales). Kept as its own regex rather than
# folded into general_corpus.EXCLUDE because that one filters CAPTIONS at fetch time,
# and these markers frequently appear only in the burned-in overlay.
BRANDED = re.compile(r"#\s?ad\b|#\s?АД\b|#\w*partner\b|paid\s+promotion|paid\s+partnership|"
                     r"#\w*sponsored|\bsponsored\s+by\b|#workingwith\w*", re.I)


# Function words are the cheapest available "is this a real English sentence" test.
# Garbled OCR of logos, UI chrome and stylised type passes every structural check —
# 'shosho TELE:ALO SM' is 4 alphabetic words and ranked FIRST on the validation run —
# but it almost never contains grammar. Real hooks nearly always do ("where ARE YOU…",
# "come ON A staycation WITH us"). Requiring one is a strong filter with very few false
# rejections, and unlike a spellcheck it needs no dictionary or extra dependency.
FUNCTION_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "so", "to", "of", "in", "on", "at", "for",
    "with", "from", "by", "is", "are", "was", "were", "be", "been", "am", "do", "does",
    "did", "have", "has", "had", "i", "you", "your", "my", "me", "we", "our", "he", "she",
    "his", "her", "they", "them", "their", "it", "its", "this", "that", "these", "those",
    "what", "when", "where", "why", "how", "who", "which", "not", "no", "just", "like",
    "get", "got", "can", "will", "would", "should", "about", "into", "out", "up", "down",
    "over", "after", "before", "than", "then", "there", "here", "all", "every", "some",
}


def usable(hook):
    """Same shape gate hook_search.raw_candidates() applies — drops OCR garbage, single
    words, and non-ASCII reads that can never cluster with an English corpus — plus a
    grammar check (see FUNCTION_WORDS) that the shape gate alone cannot provide."""
    n = norm(hook)
    words = n.split()
    if not (3 <= len(words) <= 14) or len(n) < 12 or not n.isascii():
        return False
    realish = sum(1 for w in words if len(w) >= 2 and w.isalpha())
    if realish / len(words) < 0.7:
        return False
    return any(w in FUNCTION_WORDS for w in words)


def verify_by_search(hook, top_n=8):
    """Measure whether OTHER creators actually reuse this hook — the step that makes a
    template a TREND rather than one person's line.

    Random sampling cannot answer this. Two creators using one hook inside a 157-video
    sample is a coincidence, so a sample-only run reports ~1 template however healthy the
    corpus is (measured: 57 clusters -> 1 row). hook_search.py solves it by asking TikTok
    directly, and this is that stage, ported: search the phrase, OCR what comes back, and
    count how many results genuinely reuse the STRUCTURE.

    Two things it deliberately does NOT do, both learned from hook_search.py's docstring:
      · it does not trust search relevance as proof — TikTok returns topically-related
        posts, and counting those was the exact bug that made "Bali" look like a hook.
        Only the OCR'd on-screen text of each result is evidence.
      · it does not use embedding similarity — that re-introduces the same topic/structure
        conflation one layer down. Claude judges "same template" (see judge_matches).
    Capped at the top 8 results: relevance falls off fast past there, and each result
    costs a frame-grab pass.
    """
    from hook_search import search  # local import: needs TIKHUB_TOKEN, only when used
    items = [(it.get("aweme_info") or {}) for it in search(hook)][:top_n]
    checked, texts = [], []
    now = time.time()
    for a in items:
        vurl = ((a.get("video") or {}).get("play_addr") or {}).get("url_list", [""])[0]
        aid = a.get("aweme_id", "")
        if not vurl:
            continue
        text, _stable = read_hook(vurl, aid)
        if not text:
            continue
        author = (a.get("author") or {}).get("unique_id", "")
        ct = a.get("create_time") or 0
        checked.append({
            "text": text, "account": author,
            "url": f"https://www.tiktok.com/@{author}/video/{aid}" if author and aid else "",
            "likes": (a.get("statistics") or {}).get("digg_count", 0),
            "ageDays": round((now - ct) / 86400, 1) if ct else 9999,
        })
        texts.append(text)
        time.sleep(0.15)
    if not texts:
        return [], [], 0   # 3-tuple: the caller unpacks (reused, qualified, checked)
    matched = judge_matches(hook, texts)
    # Without ANTHROPIC_API_KEY judge_matches returns [] and every candidate would read
    # as unconfirmed. Fall back to the skeleton comparison the clustering already trusts,
    # so the lane still measures reuse (less precisely) instead of reporting nothing.
    if not matched and not os.environ.get("ANTHROPIC_API_KEY"):
        sk = skeletonize(hook)
        matched = [t for t in texts
                   if difflib.SequenceMatcher(None, sk, skeletonize(t)).ratio() >= CLUSTER_RATIO]
    reused = [c for c in checked if c["text"] in matched]
    # Split rather than filter: `reused` still reports every structural match found, and
    # `qualified` reports the subset current and popular enough to be evidence of a live
    # trend. Confirmation and the displayed examples use `qualified`; keeping both means
    # the page can say "4 reused, 1 with real traction" instead of quietly showing 1.
    qualified = [c for c in reused if c["likes"] >= MIN_LIKES and c["ageDays"] <= MAX_AGE_DAYS]
    return reused, qualified, len(checked)


def cluster(rows, threshold=CLUSTER_RATIO):
    """Incremental fuzzy clustering on the SKELETON (prices/places -> placeholders), so
    two uses of one template merge even when every specific differs. Same approach as
    hook_search.cluster_internal, but carrying the per-row metrics this lane ranks on."""
    clusters = []
    for r in rows:
        sk = skeletonize(r["hook"])
        best, best_score = None, 0.0
        for c in clusters:
            s = difflib.SequenceMatcher(None, sk, skeletonize(c["hook"])).ratio()
            if s > best_score:
                best, best_score = c, s
        if best and best_score >= threshold:
            best["members"].append(r)
            best["accounts"].add(r["account"])
        else:
            clusters.append({"hook": r["hook"], "members": [r], "accounts": {r["account"]}})
    return clusters


def historic_rows(cache, corpus_ids, max_age_days=90):
    """Hooks read on EARLIER runs, so a template reused weeks apart still clusters.

    Without this the semantic pass is a measured no-op: cluster() only ever sees one run's
    rows, and a single run of ~150 videos produced 35 usable hooks across 35 distinct
    accounts — zero in-sample reuse. The reuse that exists in this data is spread across
    runs, and reuse weeks apart by unrelated creators is arguably a STRONGER trend signal
    than two people posting the same week.

    Rows are tagged origin='cache' so downstream can tell live evidence from historic; they
    carry likes/age forward so they face the same MIN_LIKES/MAX_AGE_DAYS bar as everything
    else before they can contribute to a confirmation.

    NOTE: max_age_days here bounds what is loaded for CLUSTERING and is deliberately a
    different knob from MAX_AGE_DAYS, which bounds what can count as EVIDENCE. A row can
    legitimately be old enough to help name a template but too old to prove it is current.
    """
    out = []
    for vid, v in (cache or {}).items():
        if vid in corpus_ids:
            continue
        hook = v.get("hook") or ""
        if not hook or BRANDED.search(hook) or not usable(hook):
            continue
        # Entries cached before ageDays was recorded have no age. Let them cluster (age is
        # irrelevant to whether two hooks share a structure) but leave the value absent, so
        # the evidence bar downstream still rejects them — unknown age must never read as
        # "recent enough to prove a trend".
        age = v.get("ageDays")
        if age is not None and age > max_age_days:
            continue
        out.append({"hook": hook, "account": v.get("account", ""), "url": v.get("url", ""),
                    "views": v.get("views", 0), "likes": v.get("likes", 0),
                    "source": v.get("source", ""), "stable": v.get("stable", False),
                    "ageDays": 9999 if age is None else age, "origin": "cache"})
    return out


def merge_clusters_semantic(clusters, threshold=None, max_group=8, budget=25, explain=False):
    """Second RECALL pass over cluster REPRESENTATIVES: propose merges between groups that
    are semantically close but lexically distant, then require judge_matches() to confirm
    each proposed merge before it happens.

    This is the retrieve-then-rerank split described in hook_embed's docstring. Embeddings
    only choose which pairs Claude is asked about; the answer is always Claude's. That
    keeps hook_search.py's rejection of embedding-as-verifier intact while lifting the
    recall ceiling its skeleton comparison imposes on non-travel hooks.

    Operates on representatives (~tens) rather than raw rows (~hundreds), so the judge
    budget scales with how many TEMPLATES exist, not how big the corpus is — typically
    under ten Claude calls per run.

    Returns `clusters` unchanged when either key is missing, so the no-key path (today's
    CI, and today's local runs) produces byte-identical output to before this existed.
    """
    if len(clusters) < 2 or not hook_embed.available():
        return clusters
    judged = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not judged and not os.environ.get("GH_EMBED_UNJUDGED"):
        # Embeddings with no reranker behind them is exactly the configuration
        # hook_search.py argues against. Reachable, so it is opt-in rather than impossible.
        print("  semantic merge: skipped (no ANTHROPIC_API_KEY to confirm merges; "
              "set GH_EMBED_UNJUDGED=1 to merge on similarity alone)")
        return clusters

    texts = [norm(c["hook"]) for c in clusters]
    vecs = hook_embed.embed(texts)
    if not vecs:
        print("  semantic merge: skipped (embeddings unavailable this run)")
        return clusters

    # Calibrate off pairs the skeleton comparison already accepts — they are known-positive
    # by construction. Falls back to the constant when there are too few to be meaningful.
    known = []
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if difflib.SequenceMatcher(None, skeletonize(clusters[i]["hook"]),
                                       skeletonize(clusters[j]["hook"])).ratio() >= CLUSTER_RATIO:
                known.append((i, j))
    thr, n_pos = hook_embed.calibrate_threshold(texts, vecs, known,
                                                default=threshold or hook_embed.DEFAULT_THRESHOLD)
    groups = hook_embed.cluster_by_vectors(texts, vecs, threshold=thr, max_group=max_group)
    proposals = [(s, m) for s, m in groups if m]
    print(f"  semantic merge: threshold {thr:.2f} "
          f"({'calibrated from %d known pairs' % n_pos if n_pos >= 8 else 'default, %d known pairs' % n_pos}), "
          f"{len(proposals)} merge(s) proposed across {len(clusters)} templates")

    merged_into, spent = set(), 0
    for seed, members in proposals:
        if spent >= budget:
            break
        cand = [m for m in members if m not in merged_into]
        if not cand:
            continue
        anchor = clusters[seed]["hook"]
        cand_texts = [clusters[m]["hook"] for m in cand]
        ok_texts = judge_matches(anchor, cand_texts) if judged else cand_texts
        spent += 1
        for m, t in zip(cand, cand_texts):
            verdict = t in ok_texts
            if explain:
                print(f"    {'MERGE ' if verdict else 'reject'} cos={hook_embed.cosine(vecs[seed], vecs[m]):.3f} "
                      f"{anchor[:34]!r} <- {t[:34]!r}")
            if verdict:
                clusters[seed]["members"].extend(clusters[m]["members"])
                clusters[seed]["accounts"] |= clusters[m]["accounts"]
                merged_into.add(m)
    if merged_into:
        print(f"  semantic merge: {len(merged_into)} template(s) folded in "
              f"({spent} judge call(s))")
    return [c for i, c in enumerate(clusters) if i not in merged_into]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", help="path to a general_corpus_*.json (default: newest)")
    ap.add_argument("--limit", type=int, default=250, help="max videos to OCR")
    ap.add_argument("--top", type=int, default=25, help="max template rows to write")
    ap.add_argument("--verify", type=int, default=20,
                    help="how many candidates get search-verified (1 search + up to 8 frame passes each)")
    ap.add_argument("--explain-merges", action="store_true",
                    help="print every proposed semantic merge with its cosine and the judge's verdict")
    ap.add_argument("--history-days", type=int, default=90,
                    help="how far back to pull earlier runs' cached hooks in for clustering (0 = off)")
    args = ap.parse_args()

    path = args.corpus
    if not path:
        fs = sorted(glob.glob(str(OUT / "general_corpus_*.json")), key=os.path.getmtime)
        if not fs:
            raise SystemExit("No output/general_corpus_*.json — run general_corpus.py first.")
        path = fs[-1]
    corpus = json.loads(Path(path).read_text())[: args.limit]
    # Cache by video id, like hook_text.py does: OCR is the slow step (3 frame grabs
    # over the network per video), and corpora overlap heavily week to week, so a
    # re-run should never re-read a video it has already read.
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    print(f"OCR'ing {len(corpus)} videos from {Path(path).name} ({len(cache)} cached) …")

    def read_one(p):
        """OCR one corpus row. Pure w.r.t. shared state — the caller owns the cache, so
        this is safe to run on many threads at once."""
        vid = p.get("itemID", "")
        vurl = p.get("video") or ""
        hook, stable = read_hook(vurl, vid) if vurl else ("", False)
        if not hook:  # carried URL dead (web-API rows) -> pay for a fresh one, once
            vurl = fresh_video_url(vid)
            if vurl:
                hook, stable = read_hook(vurl, vid)
        # Store age at read time so a LATER run can reuse this row as historic evidence
        # and still apply the age bar to it. Recomputed from the corpus timestamp for
        # rows in the current run (see below) — this copy is for future runs.
        try:
            ts = datetime.fromisoformat(p.get("timestamp") or "")
            age = round((datetime.now(ts.tzinfo) - ts).total_seconds() / 86400, 1)
        except (TypeError, ValueError):
            age = None
        return vid, {"hook": hook, "stable": stable, "account": p.get("account", ""),
                     "url": p.get("url", ""), "views": p.get("views", 0),
                     "likes": p.get("likes", 0), "source": p.get("source", ""),
                     "ageDays": age}

    todo_rows = [p for p in corpus if p.get("itemID") not in cache]
    fresh = len(todo_rows)
    if todo_rows:
        # OCR is ~3 network frame-grabs per video and is the whole runtime of this script;
        # at 1000 videos the sequential version takes hours. The work is I/O-bound
        # (ffmpeg + network), so threads help even under the GIL. Per-video temp paths are
        # already unique (/tmp/gh_<id>_<n>.jpg), which is what makes this safe — with the
        # old shared paths, concurrent workers would have read each other's frames, the
        # same contamination that once fabricated a 58-creator "trend".
        lock = threading.Lock()
        done = 0
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for vid, entry in pool.map(lambda p: read_one(p), todo_rows):
                with lock:
                    if vid:
                        cache[vid] = entry
                    done += 1
                    if done % 25 == 0:
                        print(f"  …{done}/{len(todo_rows)} newly OCR'd")
                        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"  ({fresh} newly OCR'd on {WORKERS} worker(s), {len(corpus) - fresh} from cache)")

    read, unstable, failed, branded = [], 0, 0, 0
    for p in corpus:
        entry = cache.get(p.get("itemID", "")) or {}
        hook, stable = entry.get("hook", ""), entry.get("stable", False)
        if not hook:
            failed += 1
        elif BRANDED.search(hook):
            branded += 1          # sponsored post — excluded, but counted so the drop is visible
        elif usable(hook):
            if not stable:
                unstable += 1
            # Age comes from the corpus row (ISO timestamp), not the cache: the cache
            # persists across runs, so a stored age would silently go stale.
            try:
                ts = datetime.fromisoformat(p.get("timestamp") or "")
                age = (datetime.now(ts.tzinfo) - ts).total_seconds() / 86400
            except (TypeError, ValueError):
                age = 9999
            read.append({"hook": hook, "account": entry.get("account", ""), "url": entry.get("url", ""),
                         "views": entry.get("views", 0), "likes": entry.get("likes", 0),
                         "source": entry.get("source", ""), "stable": stable,
                         "ageDays": round(age, 1), "origin": "run"})

    print(f"\n{len(read)} usable on-screen texts ({unstable} unstable/likely-captions, "
          f"{failed} unreadable, {branded} branded/sponsored excluded)")
    if not read:
        raise SystemExit("Nothing readable — check ffmpeg/OCR_CMD and that the corpus URLs are fresh.")

    hist = historic_rows(cache, {p.get("itemID") for p in corpus}, args.history_days)
    if hist:
        print(f"{len(hist)} additional hook(s) from earlier runs (last {args.history_days}d) "
              f"joined for clustering")

    # Stable reads anchor the clusters; an unstable read can still JOIN one, it just
    # can't be the thing a template is discovered from. Historic rows go last — they can
    # join a cluster but should never be the representative a template is named after.
    clusters = cluster([r for r in read if r["stable"]] + [r for r in read if not r["stable"]] + hist)
    clusters = merge_clusters_semantic(clusters, explain=args.explain_merges)
    clusters.sort(key=lambda c: (len(c["accounts"]), sum(m["views"] for m in c["members"])), reverse=True)

    # Spend the (slow, paid) search-verification budget on the most promising candidates
    # first: in-sample reuse ranks above a stable single read, which ranks above an
    # unstable one. --verify caps how many ever reach the network.
    clusters.sort(key=lambda c: (len(c["accounts"]),
                                 sum(1 for m in c["members"] if m["stable"]),
                                 sum(m["views"] for m in c["members"])), reverse=True)
    todo = [c for c in clusters if len(c["members"]) >= 2 or c["members"][0]["stable"]][: args.verify]
    print(f"\nSearch-verifying {len(todo)} of {len(clusters)} candidates "
          f"(top {args.verify} by in-sample reuse, then stability) …")

    rows = []
    for n, c in enumerate(todo, 1):
        in_sample = len(c["accounts"])
        ext, qual, checked = verify_by_search(c["hook"])
        ext_accounts = {e["account"] for e in qual if e.get("account")}
        creators = len(c["accounts"] | ext_accounts)
        likes_pool = [e["likes"] for e in qual] or [e["likes"] for e in ext] \
            or [m["likes"] for m in c["members"]] or [0]
        # Members of our own sample can be shown as examples too, but only if they clear
        # the same bar — a corpus row is not exempt from the standard its evidence meets.
        own_ok = [m for m in c["members"]
                  if m.get("likes", 0) >= MIN_LIKES and m.get("ageDays", 9999) <= MAX_AGE_DAYS]
        # Creators from EARLIER runs reusing this template. Counted toward confirmation,
        # but only via own_ok — i.e. only after clearing the same likes/age bar external
        # evidence clears, so a cache row can never confirm a template on age alone.
        hist_accounts = {m["account"] for m in own_ok
                         if m.get("origin") == "cache" and m.get("account")}
        # A trend needs reuse by someone OTHER than the creators already in this run's
        # sample, on posts that clear the recency/engagement bar (see MIN_LIKES). Cross-run
        # reuse counts the same way — two creators weeks apart is reuse just as much as two
        # in one week — and comes only from own_ok, so the bar is identical either way.
        this_run = {m["account"] for m in c["members"] if m.get("origin") == "run"}
        confirmed = len((ext_accounts | hist_accounts) - this_run) >= 2
        rows.append({
            "hook": c["hook"],
            "distinct_creators": creators,
            "internalCreators": in_sample,
            "historicCreators": len(hist_accounts),
            "uses": len(c["members"]),
            "templateMatches": len(qual),      # evidence that clears the bar
            "rawMatches": len(ext),            # every structural match, bar or not
            "results": checked,
            "minLikes": MIN_LIKES,
            "maxAgeDays": MAX_AGE_DAYS,
            "stableReads": sum(1 for m in c["members"] if m["stable"]),
            "maxLikes": max(likes_pool),
            "medianLikes": sorted(likes_pool)[len(likes_pool) // 2],
            "totalViews": sum(m["views"] for m in c["members"]),
            "verifiedBy": "search_verified" if confirmed else ("in_sample_reuse" if in_sample >= 2 else "unconfirmed"),
            "confirmed": confirmed or in_sample >= 2,
            # External proof first — an independent creator reusing it is the real evidence.
            # dict.fromkeys dedupes while preserving that order: a creator who appears both
            # in our sample and in the search results would otherwise be listed twice.
            "examples": list(dict.fromkeys(
                [e["url"] for e in qual if e.get("url")]
                + [m["url"] for m in own_ok if m.get("url")]))[:4],
        })
        dropped = len(ext) - len(qual)
        print(f"  [{n}/{len(todo)}] {'✓' if confirmed else ' '} {c['hook'][:40]!r} "
              f"— {len(qual)}/{checked} qualified, {len(ext_accounts)} other creators"
              + (f" ({dropped} reuse{'s' if dropped != 1 else ''} below bar)" if dropped else ""))

    rows.sort(key=lambda r: (r["confirmed"], r["templateMatches"], r["distinct_creators"]), reverse=True)
    rows = rows[: args.top]
    stamp = datetime.now().strftime("%Y-%m-%d")
    dest = OUT / f"general_hook_trends_{stamp}.json"
    dest.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    n_conf = sum(1 for r in rows if r["confirmed"])
    print(f"{len(clusters)} clusters -> {len(rows)} template rows ({n_conf} reused by 2+ creators)")
    print(f"Wrote output/general_hook_trends_{stamp}.json")
    try:
        import cost_tracker
        cost_tracker.record("general_hooks", tikhub_calls=th_calls,
                            embed_calls=hook_embed.embed_calls,
                            embed_tokens=hook_embed.embed_tokens)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
