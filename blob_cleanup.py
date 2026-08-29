#!/usr/bin/env python3
"""
Reclaim Vercel Blob storage quota.

Policy (per user instruction, 2026-08-29): keep every download from the
2026-08-10 archived week onwards (2026-08-10, 2026-08-17, plus the live
current week). Remove the download for anything ONLY referenced by an
archived week older than that cutoff -- its post video, and any Trend
Radar / Audio chart example clip that isn't ALSO used by a kept week or
the current week.

Always protected, regardless of week:
  - bookmarked (★ saved) posts -- state/dashboard-state.json `saved`
  - saved Inspiration Links -- links/*.json on Blob

A blob is deleted only if NONE of its references fall in a protected
place. If the same file backs both an old archived week AND a kept week
(same post reused as a trend example, say), it's kept.

Any post/trend/sound row in a PURGED week that pointed at a deleted blob
has its `video` field cleared to "" so nothing dangles (falls back to the
platform embed, same as any post that was never self-hosted).

Defaults to DRY RUN (prints what it would do, deletes nothing). Pass
--go to actually delete. Pure stdlib.

Usage: set -a && . ./.env && set +a && python3 blob_cleanup.py [--go]
Env: BLOB_READ_WRITE_TOKEN.
"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
BLOB = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://blob.vercel-storage.com"
KEEP_WEEKS_FROM = "2026-08-10"  # keep this archived week and newer; purge older


def blob_list(prefix):
    out, cursor = [], None
    while True:
        url = f"{BLOB_API}?prefix={prefix}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        req = urllib.request.Request(url, headers={"authorization": "Bearer " + BLOB})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
        out.extend(j.get("blobs", []))
        if not j.get("hasMore"):
            return out
        cursor = j.get("cursor")


def blob_delete(urls):
    if not urls:
        return
    req = urllib.request.Request(f"{BLOB_API}/delete", method="POST",
                                 data=json.dumps({"urls": urls}).encode(),
                                 headers={"authorization": "Bearer " + BLOB, "content-type": "application/json", "x-api-version": "7"})
    urllib.request.urlopen(req, timeout=60).read()


def blob_put(pathname, data, content_type="application/json"):
    req = urllib.request.Request(f"{BLOB_API}/{pathname}", data=data, method="PUT", headers={
        "authorization": "Bearer " + BLOB, "x-content-type": content_type,
        "x-add-random-suffix": "0", "x-allow-overwrite": "1", "x-api-version": "7"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["url"]


def fetch_json(url):
    with urllib.request.urlopen(f"{url}?t={int(time.time()*1000)}", timeout=30) as r:
        return json.loads(r.read().decode())


def load_state():
    blobs = blob_list("state/dashboard-state.json")
    match = next((b for b in blobs if b["pathname"] == "state/dashboard-state.json"), None)
    if not match:
        return {}
    return fetch_json(match["url"])


def examples_of(section):
    """section is a `trends` or `soundChart` list -- pull example/sample urls."""
    out = []
    for t in section or []:
        out.extend(t.get("examples") or [])
        out.extend(t.get("samples") or [])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually delete (default is dry run)")
    args = ap.parse_args()

    if not BLOB:
        raise SystemExit("Need BLOB_READ_WRITE_TOKEN in env.")

    data = json.loads((DASH / "data.json").read_text())
    state = load_state()
    saved_data = state.get("saved") or {}
    vmap = data.get("videos") or {}
    weeks = data.get("weeks") or {}

    kept_weeks = {w: wk for w, wk in weeks.items() if w >= KEEP_WEEKS_FROM}
    purge_weeks = {w: wk for w, wk in weeks.items() if w < KEEP_WEEKS_FROM}
    print(f"Keeping archived weeks: {sorted(kept_weeks)}")
    print(f"Purging downloads from archived weeks: {sorted(purge_weeks)}")

    protected = set()

    def note_protected(video):
        if video and "blob.vercel-storage" in video:
            protected.add(video)

    # current (live) week -- always protected
    for p in data.get("posts", []):
        note_protected(p.get("video"))
    for u in examples_of(data.get("trends")) + examples_of(data.get("soundChart")):
        note_protected(vmap.get(u) or u)

    # kept archived weeks (>= cutoff)
    for wk in kept_weeks.values():
        for p in (wk.get("posts") or []):
            note_protected(p.get("video"))
        for u in examples_of(wk.get("trends")) + examples_of(wk.get("soundChart")):
            note_protected(vmap.get(u) or u)  # archived rows may store the blob url directly

    # bookmarks -- always protected, any week
    for p in saved_data.values():
        if isinstance(p, dict):
            note_protected(p.get("video"))

    # saved Inspiration Links -- always protected, any week
    link_blobs = blob_list("links/")
    link_rows = {}
    for b in link_blobs:
        try:
            link = fetch_json(b["url"])
        except Exception:  # noqa: BLE001
            continue
        link_rows[b["pathname"]] = link
        note_protected(link.get("video"))

    # ---- inventory + decide
    video_blobs = blob_list("videos/")
    to_delete = [b for b in video_blobs if b["url"] not in protected]
    freed_kb = sum((b.get("size") or 0) for b in to_delete) // 1024

    print(f"\nBlob store: {len(video_blobs)} videos. Protected (kept): {len(protected)}.")
    print(f"{'Would delete' if not args.go else 'Deleting'} {len(to_delete)} video blob(s), ~{freed_kb//1024}MB:")
    for b in to_delete[:40]:
        print(f"  {b['pathname']:<40} {(b.get('size') or 0)//1024:>6}KB  uploaded {b.get('uploadedAt','?')[:10]}")
    if len(to_delete) > 40:
        print(f"  ... and {len(to_delete) - 40} more")

    if not args.go:
        print("\nDry run only -- nothing deleted. Re-run with --go to apply.")
        return

    urls = [b["url"] for b in to_delete]
    deleted_urls = set(urls)
    if urls:
        blob_delete(urls)

    # scrub dangling `video` fields in the PURGED weeks (and, for safety, anywhere
    # else a deleted url still shows up) so nothing points at a gone blob
    scrubbed = 0
    for wk in weeks.values():
        for p in (wk.get("posts") or []):
            if p.get("video") in deleted_urls:
                p["video"] = ""; scrubbed += 1
    for p in data.get("posts", []):
        if p.get("video") in deleted_urls:
            p["video"] = ""; scrubbed += 1
    for url in list(vmap):
        if vmap[url] in deleted_urls:
            del vmap[url]; scrubbed += 1
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

    link_scrubbed = 0
    for pathname, link in link_rows.items():
        if link.get("video") in deleted_urls:
            link["video"] = ""
            try:
                blob_put(pathname, json.dumps(link, ensure_ascii=False).encode())
                link_scrubbed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ! failed to scrub {pathname}: {str(e)[:80]}")

    print(f"\nDeleted {len(urls)} blob(s), freed ~{freed_kb//1024}MB.")
    print(f"Scrubbed {scrubbed} dangling video field(s) in data.json, {link_scrubbed} in links/.")
    print("dashboard/data.json changed locally -- commit + push to publish.")


if __name__ == "__main__":
    main()
