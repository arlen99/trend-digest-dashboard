#!/usr/bin/env python3
"""
GENERAL (niche-free) trending corpus — the input hook_search.py's Stage 0/1 need
when the question is "what hooks are trending on TikTok at all", not "…in the
travel/cinematic niche".

The existing lanes are niche-scoped by SOURCE, not by a filter you can switch off:
scrape.py only ever sees accounts.json's ~100 seed creators, and keyword_posts.py
only ever searches KEYWORDS = ["cinematic travel", …]. So no flag on hook_search.py
can make them general — the videos it clusters were already picked for the niche.
This lane replaces the SOURCE with TikTok's own general trending surfaces, and
writes the same row shape those scripts emit, so everything downstream is unchanged.

Two sources, deliberately both (verified live 2026-08, see notes):
  top_contents  — Creative Center's ranked billboard (ads/get_top_contents_list).
                  Carries videoURL directly, so no per-video lookup call is needed.
                  Sortable by 6-SECOND COMPLETION RATE, which is as close to "did the
                  opening actually hold anyone" as any public metric gets — the right
                  ranking for hook research specifically. CAVEAT: this is an
                  advertiser-facing surface. organic_only=true does NOT fully exclude
                  branded content (the top US result on the validation run was a
                  #CursorPartner sponsorship), so it skews toward high-production and
                  branded posts. EXCLUDE below strips the worst of it; the skew is why
                  this is not the only source.
  explore       — web/fetch_explore_post, TikTok's own Explore tab. Organic, fresher,
                  no ad-surface skew, but unranked and no completion-rate signal.

Region note: hooks are TEXT, so they cluster per-LANGUAGE. Mixing JP/KR/etc. into one
corpus produces clusters that can never merge and quietly wastes the Claude judging
budget on cross-language non-matches. REGIONS defaults to English-speaking markets;
override for another language, but keep one language per run.

Usage:
  set -a && . ./.env && set +a
  python3 general_corpus.py                 # ~200 videos, US/GB/AU/CA
  python3 general_corpus.py --pilot         # 1 region, 1 page — a few cents
  python3 general_corpus.py --regions US,GB --pages 5
Output: output/general_corpus_<date>.json   (feeds general_hooks.py)
Env: TIKHUB_TOKEN. Knobs: GC_REGIONS, GC_PAGES, GC_VIEW_FLOOR.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")

# US only by default: the billboard returns StatusCode 0 with ZERO entities for GB/AU/CA
# at every period_dimension (1/3/5) — checked live 2026-08. It is not an error and not a
# param mistake; that surface simply has no data for those markets on this plan, so
# listing them just burns a call each. Add a region back only if you verify it returns rows.
REGIONS = [r.strip().upper() for r in os.environ.get("GC_REGIONS", "US").split(",") if r.strip()]
PAGES = int(os.environ.get("GC_PAGES", "3"))          # ×20 per region; the pool caps at 100 (5 pages)
VIEW_FLOOR = int(os.environ.get("GC_VIEW_FLOOR", "0"))  # 0 = keep all; billboard entries are already high-reach
# Explore's real per-page ceiling is between 30 and 50 (count=30 -> 29 rows, count=50 -> statusCode 10201,
# checked live). 30 is the largest verified-good value.
EXPLORE_COUNT = 30

# Same shape as keyword_posts.py's guard — monetization bait and dropship spam reuse
# hook templates aggressively, which is exactly the kind of thing that would dominate
# a general (unfiltered) corpus and crowd out real organic formats.
EXCLUDE = re.compile(r"giveaway|crypto|forex|onlyfans|promo code|dropship|weight ?loss|\bnsfw\b|"
                     r"link in bio to (buy|shop)|telegram", re.I)

th_calls = 0


def _req(path, body=None):
    """GET (body=None) or POST JSON against TikHub. Retries transient 502s like the
    other lanes do; returns {} rather than raising so one bad page can't kill a run."""
    global th_calls
    if not KEY:
        sys.exit("TIKHUB_TOKEN not set.")
    url = "https://api.tikhub.io" + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA}
    if data:
        headers["Content-Type"] = "application/json"
    for _ in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            th_calls += 1
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001 — TikHub 502s transiently
            time.sleep(1.5)
    return {}


def _iso(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def from_top_contents(region, pages, period_end):
    """Creative Center billboard, ranked by 6-second completion rate (order_by_metric=3).

    Returns rows carrying videoURL directly — the field ffmpeg can stream a frame from
    without any download or second API call (validated live: a frame pulled straight
    off this URL OCR'd cleanly, where the web API's playAddr/downloadAddr 403s)."""
    rows = []
    for page in range(1, pages + 1):
        d = _req("/api/v1/tiktok/ads/get_top_contents_list", {
            "period_end_timestamp": period_end,
            "period_dimension": 3,
            "country_code": region,
            "content_label_ids": "",      # empty = ALL categories — this is the niche-free part
            "order_by_metric": 3,         # 6s completion rate — the hook-relevant ranking
            "organic_only": True,
            "page": page,
            "limit": 20,
        })
        data = (d.get("data") or {})
        ents = data.get("entityInfos") or []
        if not ents:
            break
        for e in ents:
            ii = e.get("itemInfo") or {}
            author = (e.get("itemAuthorInfo") or {}).get("handlerName") or ""
            item_id = ii.get("itemID") or ""
            vurl = ii.get("videoURL") or ""
            if not (item_id and vurl):
                continue
            caption = ii.get("title") or ""
            if EXCLUDE.search(caption):
                continue
            m = e.get("itemMetrics") or {}
            rows.append({
                "url": f"https://www.tiktok.com/@{author}/video/{item_id}" if author else "",
                "video": vurl,
                "itemID": item_id,
                "account": author,
                "caption": caption,
                "views": int(m.get("vv") or m.get("views") or 0),
                "likes": int(m.get("likes") or 0),
                "timestamp": _iso(ii.get("createTime")),
                "region": region,
                "source": "top_contents",
                "platform": "tiktok",
            })
        pg = data.get("pagination") or {}
        if not pg.get("hasMore"):
            break
        time.sleep(0.3)
    return rows


def english_ish(item):
    """Cheap pre-filter so an OCR pass is never spent on a video whose text can't cluster.

    Explore takes no region parameter and serves a GLOBAL feed — measured on one page:
    3/29 en, 7 ru, 3 es, plus pt/bg/tl/ar and 12 'un'. Hooks are text and cluster per
    language, so the non-English ones are dead weight; before this filter they cost ~7s
    of frame-grabbing each and then failed the ASCII gate anyway (2 usable rows out of 57).

    textLanguage is derived from the CAPTION, so it is a hint, not a verdict:
      · 'en'  -> keep.
      · 'un'  -> undetermined, which is mostly hashtag-only captions. Keep it when the
                 caption is ASCII ('#tractor #truck' reads as probably-English) and drop
                 it when it is not ('#всёрадиигры', Arabic tags) — those are certain misses.
      · empty caption -> keep; there is nothing to judge on, and the on-screen text may
                 still be English.
    Deliberately permissive: a false positive costs one wasted OCR that usable() catches
    downstream, while a false negative silently discards a real hook.
    """
    lang = (item.get("textLanguage") or "").lower()
    if lang == "en":
        return True
    if lang and lang != "un":
        return False
    caption = item.get("desc") or ""
    return caption.isascii()


def from_explore(region, want, max_calls=None):
    """TikTok's Explore tab — organic counterweight to the ad-surface skew above.

    NOTE: the endpoint takes no region parameter (unlike home_feed), so `region` is
    recorded for provenance only and every call returns the same US-default surface.
    Paging is cursor-based, but the cursor is not accepted as a query param here, so
    repeated calls just re-roll the feed; dedupe by id is what actually accumulates.

    Loops on ACCUMULATED KEPT ROWS, not call count. A fixed `want // EXPLORE_COUNT + 1`
    call budget silently under-delivers: measured survival past english_ish()+EXCLUDE is
    ~41% (36 kept of ~88 scanned in one run), so a naive count assuming 1 raw item = 1 kept
    row would ask for --explore 900 and actually return ~370 with no warning. max_calls is
    a cost safety valve, not the primary stop condition — without it, a `want` this loop
    can never satisfy (feed genuinely exhausted, or survival far worse than measured) would
    spin indefinitely."""
    if max_calls is None:
        # Sized for the measured ~41% survival rate plus real margin, so `want` is
        # actually reachable rather than a number the loop was never going to hit.
        max_calls = max(10, int(want / EXPLORE_COUNT / 0.35))
    rows, seen, skipped_lang, calls, empty_streak = [], set(), 0, 0, 0
    while len(rows) < want and calls < max_calls:
        calls += 1
        d = _req(f"/api/v1/tiktok/web/fetch_explore_post?count={EXPLORE_COUNT}")
        items = ((d.get("data") or {}).get("itemList")) or []
        if not items:
            # Observed live: 8 consecutive calls returned 0 items with statusCode 0 (a
            # reported SUCCESS) minutes after this same endpoint returned real data in
            # this same process — not a format/auth error, and not "the feed ran dry"
            # either, since a genuinely exhausted feed wouldn't recover. Treating this as
            # terminal (the original behavior) silently truncates the corpus on what is
            # more likely a transient rate-limit or upstream hiccup. Back off and retry a
            # few times before actually giving up, the same shape _req() already uses for
            # 502s — just gated separately since an empty 200 isn't an exception _req()
            # would catch.
            empty_streak += 1
            if empty_streak >= 5:
                print(f"    ({empty_streak} consecutive empty responses — stopping, "
                      f"possible sustained rate-limit or outage)")
                break
            time.sleep(2 * empty_streak)   # 2, 4, 8, 16s
            continue
        empty_streak = 0
        for it in items:
            vid = it.get("id") or ""
            if not vid or vid in seen:
                continue
            seen.add(vid)
            if not english_ish(it):
                skipped_lang += 1
                continue
            author = (it.get("author") or {}).get("uniqueId") or ""
            caption = it.get("desc") or ""
            if EXCLUDE.search(caption):
                continue
            v = it.get("video") or {}
            # playAddr/downloadAddr from the WEB api are Akamai-signed and 403 to
            # anything but a real browser (confirmed live) — but they cost nothing to
            # carry, and general_hooks.py falls back to app/v3 fetch_one_video per
            # video when a frame grab fails, exactly like fetch_videos.py does.
            vurl = v.get("playAddr") or v.get("downloadAddr") or ""
            st = it.get("stats") or {}
            if int(st.get("playCount") or 0) < VIEW_FLOOR:
                continue
            rows.append({
                "url": f"https://www.tiktok.com/@{author}/video/{vid}" if author else "",
                "video": vurl,
                "itemID": vid,
                "account": author,
                "caption": caption,
                "views": int(st.get("playCount") or 0),
                "likes": int(st.get("diggCount") or 0),
                "timestamp": _iso(it.get("createTime")),
                "region": region,
                "source": "explore",
                "platform": "tiktok",
            })
            if len(rows) >= want:
                print(f"    ({calls} calls, skipped {skipped_lang} non-English before OCR)")
                return rows
        time.sleep(0.3)
    # Loop ended via max_calls or an exhausted feed, not by reaching `want` — say so
    # explicitly rather than let a shortfall look identical to success.
    if len(rows) < want:
        print(f"    WARNING: wanted {want}, got {len(rows)} after {calls} calls "
              f"(max_calls={max_calls}) — feed may be exhausted or survival rate lower than expected")
    else:
        print(f"    ({calls} calls, skipped {skipped_lang} non-English before OCR)")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", help="comma-separated, e.g. US,GB (default GC_REGIONS)")
    ap.add_argument("--pages", type=int, default=PAGES, help="billboard pages per region (20 rows each, pool caps at 5)")
    ap.add_argument("--explore", type=int, default=60, help="how many Explore rows to add")
    ap.add_argument("--pilot", action="store_true", help="1 region, 1 page, 30 explore — a few cents")
    args = ap.parse_args()

    regions = [r.strip().upper() for r in args.regions.split(",")] if args.regions else REGIONS
    pages, want_explore = args.pages, args.explore
    if args.pilot:
        regions, pages, want_explore = regions[:1], 1, 30

    # The billboard is a RANKED PERIOD, not a live feed — it needs a closed period to
    # rank. Asking for a period ending "now" returns an incomplete/empty board, so
    # anchor 2 days back (validated: 2d back returns a full 100-row pool).
    period_end = int(time.time()) - 86400 * 2

    rows, seen_urls = [], set()
    for region in regions:
        tc = from_top_contents(region, pages, period_end)
        kept = 0
        for r in tc:
            if r["url"] and r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            rows.append(r); kept += 1
        print(f"  {region} billboard: {kept} kept ({len(tc)} fetched, {pages} page(s))")

    ex = from_explore(regions[0] if regions else "US", want_explore)
    kept = 0
    for r in ex:
        if r["url"] and r["url"] in seen_urls:
            continue
        seen_urls.add(r["url"])
        rows.append(r); kept += 1
    print(f"  explore: {kept} kept ({len(ex)} fetched)")

    stamp = datetime.now().strftime("%Y-%m-%d")
    dest = OUT / f"general_corpus_{stamp}.json"
    dest.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    by_src = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"\nWrote {len(rows)} general trending videos -> output/general_corpus_{stamp}.json")
    print(f"  by source: {by_src}   regions: {','.join(regions)}")
    print(f"  {th_calls} TikHub calls (~${th_calls * 0.001:.3f})")
    try:
        import cost_tracker
        cost_tracker.record("general_corpus", tikhub_calls=th_calls)
    except Exception:  # noqa: BLE001 — cost logging must never fail the run
        pass


if __name__ == "__main__":
    main()
