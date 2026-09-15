#!/usr/bin/env python3
"""
Semantic RECALL for hook clustering — embeddings that propose, never decide.

Why this exists: hook_search.skeletonize() blanks prices and a hardcoded ~150-name PLACES
set, then compares with difflib at 0.82. That is precisely tuned for a travel niche, and
measured on real pairs it shows exactly that shape:

    "$35 hotel room in China"  / "$12 hotel room in Vietnam"          -> 1.000  merges
    "My daughter thinks this is ice cream" / "My son thinks this is candy"
                                                                       -> 0.667  misses
    "POV: that one person who thinks farting is funny"
      / "POV: the one friend who thinks burping is hilarious"          -> 0.701  misses
    "new gens would never understand" / "kids these days would never understand"
                                                                       -> 0.754  misses

Every one of those misses is genuinely one template. They vary on dimensions skeletonize
has no blank for (daughter/son, ice cream/candy, farting/burping), so no threshold tuning
recovers them — at 0.60 the same comparison starts merging "<price> hotel room in <loc>"
with "<price> street food tour in <loc>" (0.733) instead.

Why embeddings are nonetheless kept on a leash: hook_search.py's docstring already
considered and REJECTED embedding similarity, because embeddings are calibrated for
topical closeness, and "same topic" masquerading as "same template" was the original bug
(every result for a Bali hook matched on the word "Bali"). That reasoning is sound and
still applies — to embeddings used as the VERIFIER.

The resolution here is retrieve-then-rerank: embeddings choose which pairs are worth
ASKING about, and hook_search.judge_matches() — Claude, with the same "same structure, not
same subject" instruction — decides. An embedding never merges anything by itself. The one
configuration where it would (an OpenAI key present but no Anthropic key) is off unless
explicitly enabled, because that is literally the setup the docstring warns against.

Pure stdlib, matching the rest of this project: urllib for HTTP, math.sumprod for the dot
product. No numpy, no SDK, no model download.

Env: OPENAI_API_KEY (required — absent means every entry point degrades to a no-op),
     EMBED_MODEL, EMBED_DIMS, GH_EMBED_THRESHOLD.
"""
import json
import math
import os
import time
import urllib.error
import urllib.request

EMBED_URL = "https://api.openai.com/v1/embeddings"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
# 256 dims via Matryoshka truncation. Full pairwise at n=2000 costs ~12s of CPU at 256
# against ~70s at the native 1536, and the recall difference at this task is not worth
# 6x the compare time. NOTE: truncated vectors are NOT unit length any more, which is
# why unit() below is mandatory rather than defensive.
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "256"))
EMBED_BATCH = 256          # not an API limit (2048 inputs/req) — small batches are cheap to retry
# Measured live (text-embedding-3-small@256) on the five known-good pairs this module
# was built to catch, and on three same-topic-different-template pairs shaped like the
# original "Bali" false-positive bug:
#   true positives:  0.656, 0.676, 0.751, 0.796, 0.847
#   topical-only:    0.437, 0.528, 0.615
# The gap is real but narrower than a first guess would suggest — 0.70 (the initial
# default) sat INSIDE the true-positive range and missed two of five known matches.
# 0.65 sits just above the highest topical-only score. Because every proposed pair still
# has to clear judge_matches() before it merges, erring low costs a wasted Claude call,
# not a bad merge — so recall is the scarce resource here, not precision. Re-measure once
# calibrate_threshold() has enough real corpus positives to replace this constant.
DEFAULT_THRESHOLD = float(os.environ.get("GH_EMBED_THRESHOLD", "0.65"))

# Running totals for cost_tracker.
embed_calls = 0
embed_tokens = 0

try:  # C-level dot product, Python 3.12+ — the version weekly-digest.yml pins
    from math import sumprod as _dot
except ImportError:  # older local interpreter
    from operator import mul

    def _dot(a, b):
        return sum(map(mul, a, b))


def available():
    """True when embeddings can actually be produced. Callers use this to skip work
    entirely rather than discovering the missing key mid-loop."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def unit(v):
    """Scale to unit length so cosine reduces to a plain dot product.

    Required, not defensive: OpenAI returns normalized vectors at native width, but a
    `dimensions`-truncated vector has lost part of its magnitude and is no longer unit.
    Skipping this silently inflates or deflates every comparison."""
    n = math.sqrt(_dot(v, v))
    return [x / n for x in v] if n else v


def cosine(a, b):
    """Cosine similarity of two vectors that have ALREADY been through unit()."""
    return _dot(a, b)


def embed(texts):
    """texts -> list of unit vectors aligned 1:1 with the input, or None.

    ALL-OR-NOTHING on purpose. A partial result is worse than none here: a hook whose
    batch failed would come back with no neighbours, which is indistinguishable from
    "this hook has no template" and would silently under-cluster exactly the rows the
    feature exists to find. Returning None lets the caller drop a whole tier instead.

    Never raises — same contract as hook_search.judge_matches() and .search().
    """
    global embed_calls, embed_tokens
    key = os.environ.get("OPENAI_API_KEY")
    if not (key and texts):
        return None

    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        # An empty/whitespace input 400s the endpoint, and read_hook can legitimately
        # produce a whitespace-only read — substitute rather than lose alignment.
        batch = [(t if (t or "").strip() else " ") for t in texts[i:i + EMBED_BATCH]]
        body = json.dumps({"model": EMBED_MODEL, "input": batch,
                           "dimensions": EMBED_DIMS, "encoding_format": "float"}).encode()
        got = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(EMBED_URL, data=body, headers={
                    "Authorization": "Bearer " + key, "content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.loads(r.read().decode())
                embed_calls += 1
                embed_tokens += (resp.get("usage") or {}).get("prompt_tokens", 0)
                # Sort by index — the API does not promise response order matches input
                # order, and a silent misalignment here would attach every hook to the
                # wrong vector, which no downstream check would catch.
                got = [d["embedding"] for d in sorted(resp.get("data", []),
                                                      key=lambda d: d.get("index", 0))]
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529):
                    time.sleep(2 ** attempt)   # 1, 2, 4, 8s
                    continue
                return None    # 400/401/403 — a bad key or bad request won't fix itself
            except Exception:  # noqa: BLE001 — transport hiccup, worth a retry
                time.sleep(2 ** attempt)
        if got is None or len(got) != len(batch):
            return None
        out.extend(got)
    return [unit(v) for v in out]


def cluster_by_vectors(texts, vecs, threshold=DEFAULT_THRESHOLD, max_group=8):
    """Degree-seeded greedy leader clustering over the thresholded neighbour graph.

    Returns [(seed_index, [member_index, ...]), ...] covering every index exactly once;
    a lone item comes back as (i, []).

    Deliberately NOT single-linkage / connected components. Single-linkage merges A with C
    whenever A~B~C, and with a topically-calibrated metric that chains unrelated hooks
    together through a shared middle — the docstring's failure mode, amplified transitively.
    Every member here is directly similar to the SEED, never to a member.

    Leader clustering also produces exactly the shape the reranker needs: judge_matches()
    takes one anchor plus N candidates, so a (seed, members) pair maps to a single Claude
    call. A connected component has no anchor to nominate.

    Seeding by degree (not input order, as hook_search.cluster_internal does) makes the
    canonical hook the densest one in its neighbourhood rather than whichever happened to
    be read first, which also makes the result deterministic for a given input set.
    """
    n = len(texts)
    if n != len(vecs or []):
        return [(i, []) for i in range(n)]
    adj = {i: [] for i in range(n)}
    for i in range(n):
        vi = vecs[i]
        for j in range(i + 1, n):
            s = cosine(vi, vecs[j])
            if s >= threshold:
                adj[i].append((j, s))
                adj[j].append((i, s))
    order = sorted(range(n), key=lambda i: (-len(adj[i]), texts[i]))
    taken, groups = set(), []
    for seed in order:
        if seed in taken:
            continue
        taken.add(seed)
        members = []
        for j, _s in sorted(adj[seed], key=lambda p: -p[1]):
            if j in taken or len(members) >= max_group:
                continue
            taken.add(j)
            members.append(j)
        groups.append((seed, members))
    return groups


def calibrate_threshold(texts, vecs, skeleton_pairs, floor=0.60, ceil=0.85,
                        default=DEFAULT_THRESHOLD, min_positives=8):
    """Derive the cosine threshold from pairs difflib ALREADY accepts.

    Those pairs are known-positive by construction, so the cosine below which we'd start
    losing them is a measured floor rather than a guessed constant. Takes the 10th
    percentile of their similarities, clamped to [floor, ceil].

    Returns (threshold, n_positives). With fewer than min_positives known-good pairs the
    percentile is noise, so it returns `default` and reports the count — the caller should
    say which path it took rather than presenting a guess as a measurement. On the corpus
    this was written against there is exactly ONE such pair, so the default is the live
    path today; this becomes useful once the corpus carries real reuse.
    """
    sims = []
    for i, j in skeleton_pairs:
        if 0 <= i < len(vecs) and 0 <= j < len(vecs):
            sims.append(cosine(vecs[i], vecs[j]))
    if len(sims) < min_positives:
        return default, len(sims)
    sims.sort()
    p10 = sims[max(0, int(len(sims) * 0.10) - 1)]
    return min(max(p10, floor), ceil), len(sims)
