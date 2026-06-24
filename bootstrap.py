#!/usr/bin/env python3
"""
bootstrap.py — cold-start a niche's account list from ZERO seed accounts.

Pipeline:
  1. HARVEST candidate accounts two ways (no reliance on noisy post metrics):
       A. Instagram user-search for each keyword (returns accounts + bio + followers)
       B. Hashtag post-author harvest (the creators behind niche-tagged posts)
  2. VET: niche-signal regex (bio + captions) + spam/bait exclude + follower floor,
     ranked by CROSS-QUERY FREQUENCY (an account seen across many niche queries is
     real signal; a one-off is noise).
  3. SNOWBALL: expand the vetted seeds through Instagram's related-profiles graph
     (the high-quality "similar creators" signal), ranked by seed-overlap.
  4. OUTPUT a ranked seed list. With --write, merge new accounts into accounts.json
     so the weekly scrape (scrape.py) picks them up and ranks them by outlier score.

Niche-portable: edit NICHE below, or pass --keywords / --hashtags. Point it at any
niche and it builds its own account list.

  python3 bootstrap.py                 # dry-run preview -> output/bootstrap_<date>.{json,md}
  python3 bootstrap.py --no-snowball    # harvest+vet only (cheaper/faster test)
  python3 bootstrap.py --write          # also merge vetted seeds into accounts.json

Requires APIFY_TOKEN in .env. Honest note: harvest only needs to be *decent* — junk
that slips through is washed out by the snowball (won't overlap the niche cluster)
and by final ranking in scrape.py (weak accounts' posts won't out-perform).
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
ACTOR = "apify~instagram-scraper"

# ---- the niche (swap this block for any niche) ----
NICHE = {
    "name": "travel film & cinematic photography",
    "keywords": ["cinematic travel", "travel film", "travel filmmaker",
                 "cinematic photography", "travel cinematographer"],
    "hashtags": ["cinematictravel", "travelfilm", "travelcinematography", "fx3",
                 "davinciresolve", "moodygrams", "cinematicphotography",
                 "dronefilm", "visualsoflife", "travelreel"],
}
MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "4000"))
MAX_FOLLOWERS = int(os.environ.get("MAX_FOLLOWERS", "8000000"))  # exclude giant brands
USERS_PER_KEYWORD = int(os.environ.get("USERS_PER_KEYWORD", "15"))
POSTS_PER_HASHTAG = int(os.environ.get("POSTS_PER_HASHTAG", "30"))
TARGET_SEEDS = int(os.environ.get("TARGET_SEEDS", "60"))
SNOWBALL_MIN_OVERLAP = int(os.environ.get("SNOWBALL_MIN_OVERLAP", "2"))

NICHE_SIGNAL = re.compile(
    r"cinematic|colou?r\s?grad|davinci|fx3|fx6|a7s|bmpcc|film(?!s?\s*festival)|grain|"
    r"anamorphic|drone|fpv|gimbal|shot on|b-?roll|cinematograph|travel\s?film|"
    r"moody|teal|filmmaker|visual|reel", re.I)
EXCLUDE = re.compile(
    r"giveaway|crypto|forex|onlyfans|link in bio to (buy|shop)|promo code|"
    r"booking|tour operator|dm to book|cheap flights|\bagency\b|"
    r"fitness|weight ?loss|fat ?loss|body transformation|\bcoach\b|supplement|"
    r"wedding|newborn|choreograph|\bdancer\b|skin ?care|computer science|"
    r"\bai creator\b|ai cinematic|pilot training|official account", re.I)


def die(m): print(f"ERROR: {m}", file=sys.stderr); sys.exit(1)


def run_apify(payload):
    if not APIFY_TOKEN:
        die("APIFY_TOKEN not set. `set -a && . ./.env && set +a` first.")
    url = (f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
           f"?token={APIFY_TOKEN}")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode())


def existing_accounts():
    f = ROOT / "accounts.json"
    if not f.exists():
        return set()
    return {a.strip().lstrip("@").lower() for a in json.loads(f.read_text()).get("accounts", [])}


def harvest_user_search(keywords):
    """Mode A: niche keyword -> matching accounts (with bio + followers)."""
    cand = {}
    for kw in keywords:
        try:
            items = run_apify({"search": kw, "searchType": "user",
                               "searchLimit": USERS_PER_KEYWORD, "resultsType": "details"})
        except Exception as e:  # noqa: BLE001
            print(f"  user-search '{kw}' failed: {str(e)[:80]}"); continue
        for it in items:
            u = (it.get("username") or "").lower()
            if not u:
                continue
            c = cand.setdefault(u, {"queries": set(), "followers": None, "bio": ""})
            c["queries"].add("kw:" + kw)
            c["followers"] = it.get("followersCount")
            c["bio"] = it.get("biography") or ""
        print(f"  user-search '{kw}': {len(items)} accounts")
    return cand


def harvest_hashtag_authors(hashtags):
    """Mode B: niche hashtag posts -> author usernames + their captions."""
    cand = {}
    for tag in hashtags:
        try:
            items = run_apify({"directUrls": [f"https://www.instagram.com/explore/tags/{tag}/"],
                               "resultsType": "posts", "resultsLimit": POSTS_PER_HASHTAG,
                               "searchType": "hashtag"})
        except Exception as e:  # noqa: BLE001
            print(f"  hashtag '{tag}' failed: {str(e)[:80]}"); continue
        n = 0
        for it in items:
            u = (it.get("ownerUsername") or "").lower()
            if not u:
                continue
            c = cand.setdefault(u, {"queries": set(), "followers": None, "bio": "", "captions": ""})
            c["queries"].add("tag:" + tag)
            c["captions"] = (c.get("captions", "") + " " + (it.get("caption") or ""))[:600]
            n += 1
        print(f"  #{tag}: {n} posts -> authors")
    return cand


def enrich_followers(usernames):
    """One details call per account to fill follower count + bio (batched)."""
    info = {}
    BATCH = 25
    for i in range(0, len(usernames), BATCH):
        chunk = usernames[i:i + BATCH]
        try:
            items = run_apify({"directUrls": [f"https://www.instagram.com/{u}/" for u in chunk],
                               "resultsType": "details", "resultsLimit": 1})
        except Exception as e:  # noqa: BLE001
            print(f"  enrich batch failed: {str(e)[:80]}"); continue
        for it in items:
            u = (it.get("username") or "").lower()
            if u:
                info[u] = {"followers": it.get("followersCount"), "bio": it.get("biography") or "",
                           "related": [(r.get("username") or "").lower()
                                       for r in (it.get("relatedProfiles") or []) if r.get("username")]}
        print(f"  enriched {min(i+BATCH,len(usernames))}/{len(usernames)}")
    return info


def vet(cand, info):
    """Keep niche-relevant, non-spam, sufficiently-followed accounts; rank by query overlap."""
    seeds = []
    seen = existing_accounts()
    for u, c in cand.items():
        if u in seen:
            continue
        if re.search(r"\d{4,}$", u):   # numeric-suffix handles are usually bots/spam
            continue
        f = c.get("followers")
        if f is None and u in info:
            f = info[u]["followers"]
        bio = c.get("bio") or (info.get(u, {}).get("bio", ""))
        text = " ".join([bio, c.get("captions", "")])
        if EXCLUDE.search(text):
            continue
        if not NICHE_SIGNAL.search(text) and not any("tag:" in q for q in c["queries"]):
            continue  # require a niche signal unless it came from multiple hashtag posts
        if f is None or f < MIN_FOLLOWERS or f > MAX_FOLLOWERS:
            continue
        seeds.append({"account": u, "followers": f, "queries": len(c["queries"]),
                      "from": sorted(c["queries"])[:4], "bio": bio[:80]})
    seeds.sort(key=lambda s: (s["queries"], s["followers"]), reverse=True)
    return seeds[:TARGET_SEEDS]


def snowball(seed_accts, info):
    """Expand via related-profiles graph; rank candidates by # seeds pointing to them."""
    related = defaultdict(set)
    # use related lists already fetched during enrich; fetch any missing
    missing = [s for s in seed_accts if s not in info or "related" not in info[s]]
    if missing:
        info.update(enrich_followers(missing))
    seen = existing_accounts() | set(seed_accts)
    for s in seed_accts:
        for r in info.get(s, {}).get("related", []):
            if r and r not in seen:
                related[r].add(s)
    ranked = sorted(((u, srcs) for u, srcs in related.items() if len(srcs) >= SNOWBALL_MIN_OVERLAP),
                    key=lambda kv: len(kv[1]), reverse=True)
    return [{"account": u, "overlap": len(srcs), "related_to": sorted(srcs)[:4]} for u, srcs in ranked]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", help="comma-separated override")
    ap.add_argument("--hashtags", help="comma-separated override")
    ap.add_argument("--no-snowball", action="store_true")
    ap.add_argument("--write", action="store_true", help="merge seeds into accounts.json")
    args = ap.parse_args()
    kws = [k.strip() for k in args.keywords.split(",")] if args.keywords else NICHE["keywords"]
    tags = [t.strip() for t in args.hashtags.split(",")] if args.hashtags else NICHE["hashtags"]

    print(f"Bootstrapping niche: {NICHE['name']}")
    print("Stage 1a — user search…"); cand = harvest_user_search(kws)
    print("Stage 1b — hashtag authors…")
    for u, c in harvest_hashtag_authors(tags).items():
        d = cand.setdefault(u, {"queries": set(), "followers": None, "bio": "", "captions": ""})
        d["queries"] |= c["queries"]; d["captions"] = (d.get("captions", "") + c.get("captions", ""))[:600]
    print(f"  harvested {len(cand)} raw candidate accounts")

    # enrich the ones missing follower data (hashtag authors)
    need = [u for u, c in cand.items() if c.get("followers") is None]
    print(f"Stage 2 — vetting (enriching {len(need)} for followers/bio)…")
    info = enrich_followers(need) if need else {}
    seeds = vet(cand, info)
    print(f"  vetted -> {len(seeds)} seed accounts")

    snow = []
    if not args.no_snowball and seeds:
        print("Stage 3 — related-profiles snowball…")
        snow = snowball([s["account"] for s in seeds], info)
        print(f"  snowball -> {len(snow)} additional candidates")

    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = {"niche": NICHE["name"], "generated": stamp, "seeds": seeds, "snowball": snow}
    (OUT / f"bootstrap_{stamp}.json").write_text(json.dumps(payload, indent=2))
    lines = [f"# Bootstrap — {NICHE['name']} — {stamp}",
             f"_{len(seeds)} vetted seeds (keyword/hashtag harvest) + {len(snow)} snowball candidates_\n",
             "## Seeds (harvested + vetted)", "| Account | Followers | Queries | From |", "|--|--|--|--|"]
    for s in seeds:
        lines.append(f"| @{s['account']} | {s['followers']:,} | {s['queries']} | {', '.join(s['from'])} |")
    if snow:
        lines += ["\n## Snowball (related-profiles)", "| Account | Seed overlap | Related to |", "|--|--|--|"]
        for s in snow:
            lines.append(f"| @{s['account']} | {s['overlap']} | {', '.join('@'+a for a in s['related_to'])} |")
    (OUT / f"bootstrap_{stamp}.md").write_text("\n".join(lines))
    print(f"\nWrote output/bootstrap_{stamp}.json/.md")

    if args.write:
        new = [s["account"] for s in seeds] + [s["account"] for s in snow]
        f = ROOT / "accounts.json"
        data = json.loads(f.read_text()) if f.exists() else {"niche": NICHE["name"], "accounts": []}
        have = {a.lower() for a in data["accounts"]}
        added = [a for a in dict.fromkeys(new) if a.lower() not in have]
        data["accounts"].extend(added)
        data[f"_bootstrapped_{stamp}"] = f"Added {len(added)} accounts via bootstrap.py (keyword harvest + snowball)."
        f.write_text(json.dumps(data, indent=2))
        print(f"--write: added {len(added)} new accounts to accounts.json (now {len(data['accounts'])}).")
    else:
        print("Dry run. Review the .md, then re-run with --write to merge into accounts.json.")


if __name__ == "__main__":
    main()
