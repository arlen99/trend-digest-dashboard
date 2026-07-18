#!/usr/bin/env python3
"""
Compute the dashboard's SOURCE/PROVENANCE numbers from the real pipeline artifacts
and inject them into dashboard/data.json as a `provenance` block, so the board can
state exactly what it was built from (accounts scraped, discovered, posts pulled,
tracks scanned, etc.). Pure transform — every figure traces to a file, none invented.

Run after the scrapes + merges, before build_dashboard.py.
Usage: python3 provenance.py
"""
import glob
import json
import os
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"


def latest(pat):
    fs = sorted(glob.glob(str(ROOT / "output" / pat)), key=os.path.getmtime)
    return fs[-1] if fs else None


def jload(p, default=None):
    return json.loads(Path(p).read_text()) if p and Path(p).exists() else default


def md_header(pat):
    f = latest(pat)
    if not f:
        return ""
    f2 = f.rsplit(".", 1)[0] + ".md"
    return Path(f2).read_text().split("\n")[1] if Path(f2).exists() else ""


def main():
    acc = jload(ROOT / "accounts.json", {})
    ig_total = len(acc.get("accounts", []))
    disc = boot = 0
    for k, v in acc.items():
        if "discover" in k.lower():
            m = re.search(r"Added (\d+)", str(v));  disc = int(m.group(1)) if m else disc
        if "bootstrap" in k.lower():
            m = re.search(r"Added (\d+).*?pruned (\d+)", str(v))
            boot = (int(m.group(1)) - int(m.group(2))) if m else boot
    ig_seed = ig_total - disc - boot

    # how many of the tracked handles actually resolve to an IG id (= scrapable set)
    ids = jload(ROOT / "output" / "user_ids.json", {}) or {}
    ig_resolved = sum(1 for h in acc.get("accounts", []) if h in ids or h.lstrip("@") in ids)

    tt_acc = jload(ROOT / "tiktok_accounts.json", {})
    tt_creators = len(tt_acc.get("accounts", []))

    env = dict(re.findall(r"^(\w+)=(.*)$", (ROOT / ".env").read_text(), re.M)) if (ROOT / ".env").exists() else {}

    data = jload(DASH / "data.json", {})
    P = data.get("posts", [])
    ig_curated = sum(1 for p in P if p.get("platform", "instagram") == "instagram" and p.get("lane") != "keyword")
    tt_curated = sum(1 for p in P if p.get("platform") == "tiktok" and p.get("lane") != "keyword")
    kw_finds = sum(1 for p in P if p.get("lane") == "keyword")
    board_accounts = len(set(p["account"] for p in P))

    audio = jload(latest("audio_trends_*.json"), []) or []
    m = re.search(r"_(\d+) accounts", md_header("audio_trends_*.json"))
    audio_accounts = int(m.group(1)) if m else len({a for t in audio for a in (t.get("accounts") or [])})
    audio_multi = sum(1 for t in audio if t.get("niche_creators", 0) >= 2)

    tt_sounds = jload(latest("tiktok_trends_*.json"), []) or []
    kw_scanned = len(jload(latest("keyword_posts_*.json"), []) or [])

    # pipeline counts for the "how it's built" popups
    hooks = jload(ROOT / "output" / "hook_texts.json", {}) or {}
    hooks_ocrd = len(hooks)
    hooks_readable = sum(1 for v in hooks.values() if v.get("hook"))
    # which platforms got OCR'd (today: IG-only, since the TikTok web endpoint strips video URLs)
    hook_url_strs = " ".join((v.get("url") or "") for v in hooks.values())
    hook_plats = []
    if "instagram.com" in hook_url_strs:
        hook_plats.append("Instagram")
    if "tiktok.com" in hook_url_strs:
        hook_plats.append("TikTok")
    hook_validated = len(jload(latest("hook_trends_*.json"), []) or [])
    try:
        from keyword_posts import KEYWORDS as kw_list
    except Exception:  # noqa: BLE001
        kw_list = []

    # rough weekly $ cost — see cost_tracker.py for rates/caveats (approximate, not billing-accurate)
    # output/pipeline_costs.json is LOCAL and gitignored — a real weekly run builds it fresh
    # from a clean checkout, but running provenance.py locally for an unrelated fix (as
    # happened 2026-07-15) reads whatever's left on disk from ad-hoc script runs since,
    # silently overwriting the real run's cost with e.g. one debug script's tally. Only
    # trust it if it looks like an actual full run — carries entries for the two scripts
    # every real week unconditionally runs (scrape.py, curate_posts.py) — otherwise keep
    # whatever's already committed rather than replace real numbers with partial ones.
    import cost_tracker
    cost = cost_tracker.summarize()
    prior_cost = (data.get("provenance") or {})
    looks_like_a_real_run = {"scrape", "curate_posts"} <= set(cost.get("perScript", {}))
    if not looks_like_a_real_run and prior_cost.get("costTotal") is not None:
        cost = {
            "totals": {
                "tikhubCalls": prior_cost.get("costTikhubCalls"), "claudeCalls": prior_cost.get("costClaudeCalls"),
                "auddCalls": prior_cost.get("costAuddCalls"), "auddAuthDead": prior_cost.get("auddAuthDead", False),
            },
            "tikhubCost": prior_cost.get("costTikhub"), "claudeCost": prior_cost.get("costClaude"),
            "estCost": prior_cost.get("costTotal"),
        }

    # why candidates got excluded from the IG Swipe File this run (curate_posts.py)
    # Same staleness problem as the cost figures above, and the same guard: this file
    # only exists fresh right after a real curate_posts.py run, so an ad-hoc local
    # provenance.py call between real runs would otherwise show "0 excluded" — not
    # "we don't know", an actively wrong claim that curation looked stricter than it did.
    excludes = jload(latest("curation_excludes_*.json"), []) or []
    if not looks_like_a_real_run and not excludes and prior_cost.get("igExcluded") is not None:
        ig_excluded_fallback = prior_cost.get("igExcluded")
        ig_evaluated_fallback = prior_cost.get("igEvaluated")
        exclude_reasons_fallback = prior_cost.get("igExcludeReasons") or []
    else:
        ig_excluded_fallback = ig_evaluated_fallback = exclude_reasons_fallback = None
    exclude_reasons = Counter(e.get("reason", "") for e in excludes)

    prov = {
        "igAccounts": ig_total, "igResolved": ig_resolved,
        "igSeed": ig_seed, "igDiscovered": disc, "igBootstrap": boot,
        "tiktokCreators": tt_creators,
        "postsPerIg": int(env.get("POSTS_PER_ACCOUNT", 8)), "postsPerTt": int(env.get("TT_POSTS_PER", 10)),
        "daysBack": int(env.get("DAYS_BACK", 30)),
        "igCurated": ig_curated, "tiktokCurated": tt_curated,
        "keywordFinds": kw_finds, "keywordScanned": kw_scanned, "boardAccounts": board_accounts,
        "audioAccounts": audio_accounts, "audioReelsPer": int(os.environ.get("CLIPS_PER_ACCOUNT", 20)),
        "audioTracks": len(audio), "audioMulti": audio_multi, "audioShown": len(data.get("soundChart", [])),
        "ttSounds": len(tt_sounds), "ttXplatform": sum(1 for s in tt_sounds if s.get("also_in_ig_niche")),
        "ttShown": len(data.get("tiktokSounds", [])),
        # pipeline-diagram fields
        "audioReels": audio_accounts * int(os.environ.get("CLIPS_PER_ACCOUNT", 20)),
        "hooksOcrd": hooks_ocrd, "hooksReadable": hooks_readable, "hookValidated": hook_validated,
        "hookPlats": hook_plats, "hookMinNiche": 2, "hookMinLikes": 10000,
        "keywords": kw_list, "tiktokCreators": tt_creators,
        "trendsAudio": sum(1 for t in data.get("trends", []) if t.get("type") == "audio"),
        "trendsHook": sum(1 for t in data.get("trends", []) if t.get("type") == "hook"),
        # rough cost + curation transparency (approximate — see cost_tracker.py)
        "costTikhubCalls": cost["totals"]["tikhubCalls"], "costClaudeCalls": cost["totals"]["claudeCalls"],
        "costAuddCalls": cost["totals"]["auddCalls"], "auddAuthDead": cost["totals"]["auddAuthDead"],
        "costTikhub": cost["tikhubCost"], "costClaude": cost["claudeCost"], "costTotal": cost["estCost"],
        "igExcluded": ig_excluded_fallback if ig_excluded_fallback is not None else len(excludes),
        "igEvaluated": ig_evaluated_fallback if ig_evaluated_fallback is not None else ig_curated + len(excludes),
        "igExcludeReasons": exclude_reasons_fallback if exclude_reasons_fallback is not None else exclude_reasons.most_common(5),
    }
    data["provenance"] = prov
    # expose the live reference pools so the dashboard Accounts panel can render + edit them
    data["pools"] = {
        "ig": sorted(acc.get("accounts", []), key=str.lower),
        "tiktok": sorted(tt_acc.get("accounts", []), key=str.lower),
    }
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print("Injected provenance:", json.dumps(prov))


if __name__ == "__main__":
    main()
