#!/usr/bin/env python3
"""
TikTok niche creator discovery — build the niche's TikTok account list from zero
(TikTok-native; mirrors the IG bootstrap but uses TikTok search).

Pipeline:
  1. For each niche keyword, search TikTok creators (fetch_user_search_result —
     returns handle + follower count + bio) and video authors (fetch_video_search_result).
  2. Deep-extract creator records (the response nesting varies, so we walk it).
  3. Vet: niche-signal regex on bio/nickname + spam exclude + follower floor,
     ranked by cross-keyword frequency.
  4. Write tiktok_accounts.json (handle + sec_uid, which the scraper needs).

Niche-portable: edit KEYWORDS or pass --keywords. With --write, merges into
tiktok_accounts.json.

Usage:
  set -a && . ./.env && set +a
  python3 tiktok_discover.py --pilot          # 3 keywords, preview
  python3 tiktok_discover.py --write           # full, merge into tiktok_accounts.json
Env: TIKHUB_TOKEN.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("TIKHUB_TOKEN")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")
KEYWORDS = ["cinematic travel", "travel film", "cinematic photography",
            "travel filmmaker", "fx3 travel", "cinematic b roll", "travel cinematography"]
MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "5000"))
USERS_PER_KW = int(os.environ.get("USERS_PER_KW", "20"))
TARGET = int(os.environ.get("TARGET", "80"))

NICHE = re.compile(r"cinematic|colou?r ?grad|davinci|fx3|fx6|a7s|bmpcc|film(?!s? fest)|"
                   r"travel ?film|drone|fpv|gimbal|shot on|b-?roll|cinematograph|visual|reel", re.I)
EXCLUDE = re.compile(r"giveaway|crypto|forex|onlyfans|promo code|link in bio to (buy|shop)|"
                     r"fitness|weight ?loss|wedding|dropship|\bnsfw\b|telegram", re.I)
calls = 0


def th(path):
    global calls
    if not KEY:
        sys.exit("TIKHUB_TOKEN not set.")
    req = urllib.request.Request("https://api.tikhub.io" + path,
                                 headers={"Authorization": "Bearer " + KEY, "accept": "application/json", "User-Agent": UA})
    calls += 1
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:90]}


def find_int_near(d, names):
    for n in names:
        v = d.get(n)
        if isinstance(v, int):
            return v
    return None


def collect(obj, found):
    """Walk a TikTok response, pulling any creator record (has a handle)."""
    if isinstance(obj, dict):
        uid = obj.get("unique_id") or obj.get("uniqueId")
        if uid:
            fol = find_int_near(obj, ["follower_count", "followerCount", "fans"])
            if fol is None:
                stats = obj.get("stats") or obj.get("statsV2") or {}
                fol = find_int_near(stats, ["follower_count", "followerCount", "fans"])
                if fol is None and isinstance(stats.get("followerCount"), str) and stats["followerCount"].isdigit():
                    fol = int(stats["followerCount"])
            rec = found.setdefault(uid.lower(), {"unique_id": uid, "sec_uid": "", "nickname": "",
                                                 "signature": "", "followers": None, "kw": set()})
            rec["sec_uid"] = rec["sec_uid"] or obj.get("sec_uid") or obj.get("secUid") or ""
            rec["nickname"] = rec["nickname"] or obj.get("nickname") or ""
            rec["signature"] = rec["signature"] or obj.get("signature") or ""
            if fol is not None:
                rec["followers"] = fol
        for v in obj.values():
            collect(v, found)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords")
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    kws = [k.strip() for k in args.keywords.split(",")] if args.keywords else KEYWORDS
    if args.pilot:
        kws = kws[:3]

    found = {}
    for kw in kws:
        q = urllib.parse.quote(kw)
        d = th(f"/api/v1/tiktok/app/v3/fetch_user_search_result?keyword={q}&count={USERS_PER_KW}")
        before = len(found)
        collect(d, found)
        # tag which keyword surfaced each newly/again-seen creator
        for rec in found.values():
            rec.setdefault("kw", set())
        v = th(f"/api/v1/tiktok/app/v3/fetch_video_search_result?keyword={q}&count={USERS_PER_KW}")
        collect(v, found)
        for uid, rec in found.items():
            rec["kw"].add(kw)  # coarse tag; refined below by re-walk not needed for pilot
        print(f"  '{kw}': pool now {len(found)} creators (+{len(found)-before})")
        time.sleep(0.3)

    # vet
    existing = set()
    f = ROOT / "tiktok_accounts.json"
    if f.exists():
        existing = {a.lower() for a in json.loads(f.read_text()).get("accounts", [])}
    seeds = []
    for uid, r in found.items():
        if uid in existing:
            continue
        text = f"{r['nickname']} {r['signature']}"
        if EXCLUDE.search(text):
            continue
        if not NICHE.search(text):
            continue
        fol = r["followers"]
        if fol is None or fol < MIN_FOLLOWERS:
            continue
        seeds.append(r)
    seeds.sort(key=lambda r: r["followers"] or 0, reverse=True)
    seeds = seeds[:TARGET]

    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = [{"unique_id": r["unique_id"], "sec_uid": r["sec_uid"], "nickname": r["nickname"],
                "followers": r["followers"], "signature": r["signature"][:90]} for r in seeds]
    (OUT / f"tiktok_discover_{stamp}.json").write_text(json.dumps(payload, indent=2))
    lines = [f"# TikTok niche creators — {stamp}",
             f"_{len(found)} harvested · {len(seeds)} vetted (niche + ≥{MIN_FOLLOWERS} followers) · {calls} API calls_\n",
             "| Handle | Followers | Bio |", "|--|--|--|"]
    for r in seeds:
        lines.append(f"| @{r['unique_id']} | {(r['followers'] or 0):,} | {r['signature'][:54].replace(chr(10),' ')} |")
    (OUT / f"tiktok_discover_{stamp}.md").write_text("\n".join(lines))
    print(f"\nWrote output/tiktok_discover_{stamp}.json/.md — {len(seeds)} vetted TikTok creators")

    if args.write:
        f = ROOT / "tiktok_accounts.json"
        data = json.loads(f.read_text()) if f.exists() else {"niche": "travel & cinematic", "accounts": [], "sec_uids": {}}
        have = {a.lower() for a in data["accounts"]}
        added = 0
        for r in seeds:
            if r["unique_id"].lower() not in have:
                data["accounts"].append(r["unique_id"]); added += 1
            data.setdefault("sec_uids", {})[r["unique_id"]] = r["sec_uid"]
        data[f"_discovered_{stamp}"] = f"Added {added} TikTok creators via tiktok_discover.py."
        f.write_text(json.dumps(data, indent=2))
        print(f"--write: added {added} creators to tiktok_accounts.json (now {len(data['accounts'])}).")


if __name__ == "__main__":
    main()
