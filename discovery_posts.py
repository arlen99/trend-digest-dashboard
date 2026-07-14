#!/usr/bin/env python3
"""
Fetches a small sample of REAL posts for each IG/TikTok discovery/bootstrap
candidate account, so they can be reviewed as actual posts on the dashboard
(tagged "⚡ new find") instead of the account being merged into accounts.json /
tiktok_accounts.json blind. An account only joins the permanent watchlist when
you save one of its posts and confirm the "add to watchlist?" prompt — see
discovery_to_dashboard.py for the merge step and the dashboard's saveWithConfirm()
for the approval prompt itself. Nothing in this script writes to either accounts
file.

Reads whatever the LATEST discover.py / bootstrap.py / tiktok_discover.py output
files already are — doesn't re-run discovery itself. Excludes anyone already
tracked, caps the pool for cost control, and reuses scrape.py's / tiktok_scrape.py's
own fetch+normalize functions so results are identical in shape to a normal scrape.

Usage:
  set -a && . ./.env && set +a
  python3 discover.py && python3 bootstrap.py && python3 tiktok_discover.py   # refresh candidates first
  python3 discovery_posts.py                  # both platforms, up to 20 candidates each
  python3 discovery_posts.py --max 10         # cheaper test
Env: TIKHUB_TOKEN.
"""
import argparse
import glob
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import scrape
import tiktok_scrape

ROOT = Path(__file__).parent
OUT = ROOT / "output"
POSTS_PER_CANDIDATE = 5  # a sniff test, not a full harvest — just enough to judge the account


def latest(pattern):
    files = sorted(glob.glob(str(OUT / pattern)), key=lambda p: Path(p).stat().st_mtime)
    return Path(files[-1]) if files else None


def ig_candidates(max_n):
    """Combine discover.py + bootstrap.py's latest candidate lists, dedup against
    accounts.json, cap. Returns {username: source_note}."""
    acc = json.loads((ROOT / "accounts.json").read_text())
    have = {a.strip().lstrip("@").lower() for a in acc.get("accounts", [])}
    cands = {}

    f = latest("discovered_*.json")
    if f:
        for d in json.loads(f.read_text()):
            u = d["account"].lower()
            if u not in have:
                cands.setdefault(u, f"discover.py — related to {d['seed_overlap']} seed accounts")

    f = latest("bootstrap_*.json")
    if f:
        payload = json.loads(f.read_text())
        for s in payload.get("seeds", []):
            u = s["account"].lower()
            if u not in have:
                cands.setdefault(u, "bootstrap.py — keyword/hashtag harvest")
        for s in payload.get("snowball", []):
            u = s["account"].lower()
            if u not in have:
                cands.setdefault(u, "bootstrap.py — related-profiles snowball")

    return dict(list(cands.items())[:max_n])


def tt_candidates(max_n):
    """tiktok_discover.py's latest candidate list, dedup against tiktok_accounts.json.
    Returns {unique_id: full_record} — need sec_uid to fetch posts."""
    tt = json.loads((ROOT / "tiktok_accounts.json").read_text())
    have = {a.strip().lstrip("@").lower() for a in tt.get("accounts", [])}
    f = latest("tiktok_discover_*.json")
    if not f:
        return {}
    cands = {}
    for r in json.loads(f.read_text()):
        u = r["unique_id"].lower()
        if u not in have:
            cands[u] = r
    return dict(list(cands.items())[:max_n])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=20, help="cap candidates scraped, per platform")
    args = ap.parse_args()
    stamp = datetime.now().strftime("%Y-%m-%d")
    rows = []

    ig_cands = ig_candidates(args.max)
    if ig_cands:
        print(f"IG: {len(ig_cands)} untracked candidates from discover.py/bootstrap.py")
        old_ppa = scrape.POSTS_PER_ACCOUNT
        scrape.POSTS_PER_ACCOUNT = POSTS_PER_CANDIDATE
        try:
            ids = scrape.resolve_ids(list(ig_cands.keys()))
            for u, pk in ids.items():
                try:
                    posts = scrape.fetch_account(pk, u)
                except Exception as e:  # noqa: BLE001 - one bad candidate shouldn't kill the run
                    print(f"  ! @{u} -> {str(e)[:80]}"); continue
                for p in posts:
                    p["platform"] = "instagram"
                    p["discoverySource"] = ig_cands[u]
                rows.extend(posts)
                time.sleep(0.15)
        finally:
            scrape.POSTS_PER_ACCOUNT = old_ppa
    else:
        print("IG: no untracked candidates (run discover.py / bootstrap.py first, or everyone's already tracked).")

    tt_cands = tt_candidates(args.max)
    if tt_cands:
        print(f"TikTok: {len(tt_cands)} untracked candidates from tiktok_discover.py")
        for u, r in tt_cands.items():
            d = tiktok_scrape.th(f"/api/v1/tiktok/web/fetch_user_post?secUid={r['sec_uid']}&count={POSTS_PER_CANDIDATE}")
            for a in (tiktok_scrape.find_aweme_list(d) or []):
                try:
                    n = tiktok_scrape.normalize(a)
                except Exception:  # noqa: BLE001
                    continue
                if n:
                    n["discoverySource"] = "tiktok_discover.py — keyword/hashtag search"
                    rows.append(n)
            time.sleep(0.2)
    else:
        print("TikTok: no untracked candidates (run tiktok_discover.py first, or everyone's already tracked).")

    if not rows:
        print("No candidate posts fetched. Nothing to write.")
        return

    # One post per candidate account (their own best by outlier score) — this is a
    # discovery signal to decide whether the ACCOUNT is worth watching, not a full
    # audit of everything they've posted.
    by_acct = {}
    for r in rows:
        by_acct.setdefault((r["platform"], r["account"]), []).append(r)
    best = []
    for (plat, acct), posts in by_acct.items():
        engs = [p["engagement"] for p in posts]
        med = statistics.median(engs) or 1
        for p in posts:
            p["outlier_score"] = round(p["engagement"] / med, 2)
        posts.sort(key=lambda p: (p["outlier_score"], p["engagement"]), reverse=True)
        best.append(posts[0])
    best.sort(key=lambda p: p["outlier_score"], reverse=True)

    (OUT / f"discovery_candidates_{stamp}.json").write_text(json.dumps(best, indent=2, ensure_ascii=False))
    print(f"\nWrote output/discovery_candidates_{stamp}.json — {len(best)} candidate accounts' "
          f"best post each. Next: python3 discovery_to_dashboard.py")

    import cost_tracker
    cost_tracker.record("discovery_posts", tikhub_calls=scrape.th_calls + tiktok_scrape.th_calls)


if __name__ == "__main__":
    main()
