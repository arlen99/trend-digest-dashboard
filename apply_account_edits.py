#!/usr/bin/env python3
"""
Apply queued account add/removes from the dashboard's synced state to the local
reference pools BEFORE the weekly scrape runs.

Source: `accountEdits` field on the synced state blob (state/dashboard-state.json),
written by the dashboard's Accounts panel. Shape:
  { igAdd:[handle,...], igRemove:[handle,...], ttAdd:[handle,...], ttRemove:[handle,...] }

Applies them to accounts.json + tiktok_accounts.json (handles are case-insensitive,
de-duped; preserves the existing JSON structure), then CLEARS the queue on the backend
so the same edits aren't reapplied next week.

Snowballing (discover.py + bootstrap.py) reads accounts.json directly, so any added
accounts immediately become snowball seeds on the next run — no extra wiring.

Usage: set -a && . ./.env && set +a && python3 apply_account_edits.py
Env: BLOB_READ_WRITE_TOKEN. Optional STATE_BLOB_PATH (default state/dashboard-state.json).
Safe no-op if BLOB not configured or no edits queued.
"""
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
BLOB = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://blob.vercel-storage.com"
PATH = os.environ.get("STATE_BLOB_PATH", "state/dashboard-state.json")


def blob_get_state():
    """Returns parsed state JSON, or {} if no blob yet."""
    if not BLOB:
        return {}
    try:
        r = urllib.request.Request(f"{BLOB_API}?prefix={PATH}", headers={"authorization": "Bearer " + BLOB})
        with urllib.request.urlopen(r, timeout=30) as resp:
            blobs = json.loads(resp.read()).get("blobs", [])
        match = next((b for b in blobs if b["pathname"] == PATH), None)
        if not match:
            return {}
        with urllib.request.urlopen(match["url"], timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        print(f"  blob read failed: {str(e)[:80]}")
        return {}


def blob_pending_adds():
    """Profile URLs shared via /api/save-link are queued as their own atomic blobs
    at account-adds/<platform>_<handle>.json (they can't be merged into the shared
    state blob server-side — its CDN-cached reads lag writes by a minute-plus, so a
    read-modify-write there could clobber recent dashboard state). Returns
    ({'ig': [handles], 'tt': [handles]}, [blob urls to delete after applying])."""
    adds, urls = {"ig": [], "tt": []}, []
    if not BLOB:
        return adds, urls
    try:
        r = urllib.request.Request(f"{BLOB_API}?prefix=account-adds/", headers={"authorization": "Bearer " + BLOB})
        with urllib.request.urlopen(r, timeout=30) as resp:
            blobs = json.loads(resp.read()).get("blobs", [])
        for b in blobs:
            try:
                with urllib.request.urlopen(b["url"], timeout=30) as resp:
                    j = json.loads(resp.read())
                plat = "tt" if j.get("platform") == "tt" else "ig"
                if j.get("handle"):
                    adds[plat].append(j["handle"])
                urls.append(b["url"])
            except Exception as e:  # noqa: BLE001 - skip one unreadable entry, keep the rest
                print(f"  pending-add blob unreadable ({b.get('pathname')}): {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"  pending-adds list failed: {str(e)[:80]}")
    return adds, urls


def blob_delete(urls):
    if not (BLOB and urls):
        return
    r = urllib.request.Request(f"{BLOB_API}/delete", method="POST",
                               data=json.dumps({"urls": urls}).encode(),
                               headers={"authorization": "Bearer " + BLOB,
                                        "content-type": "application/json", "x-api-version": "7"})
    urllib.request.urlopen(r, timeout=30).read()


def blob_put_state(state):
    r = urllib.request.Request(f"{BLOB_API}/{PATH}", data=json.dumps(state).encode(),
                               method="PUT", headers={
                                   "authorization": "Bearer " + BLOB,
                                   "x-content-type": "application/json",
                                   "x-add-random-suffix": "0",
                                   "x-allow-overwrite": "1",
                                   "x-api-version": "7",
                                   "x-cache-control-max-age": "0"})
    urllib.request.urlopen(r, timeout=30).read()


def apply_to(path: Path, add: list, remove: list) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    data = json.loads(path.read_text())
    pool = data.get("accounts", [])
    lower = {h.lower() for h in pool}
    removed = 0
    if remove:
        rem_lower = {h.lower().lstrip("@").strip() for h in remove if h}
        new_pool = [h for h in pool if h.lower() not in rem_lower]
        removed = len(pool) - len(new_pool)
        pool = new_pool; lower = {h.lower() for h in pool}
    added = 0
    for h in add or []:
        h = (h or "").lstrip("@").strip()
        if h and h.lower() not in lower:
            pool.append(h); lower.add(h.lower()); added += 1
    data["accounts"] = pool
    data[f"_dashboard_edit_{datetime.now().strftime('%Y-%m-%d')}"] = f"+{added} / -{removed} via Accounts panel"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return added, removed


def main():
    state = blob_get_state()
    edits = (state or {}).get("accountEdits") or {}
    ig_add, ig_rem = edits.get("igAdd") or [], edits.get("igRemove") or []
    tt_add, tt_rem = edits.get("ttAdd") or [], edits.get("ttRemove") or []
    pending, pending_urls = blob_pending_adds()
    ig_add = ig_add + pending["ig"]
    tt_add = tt_add + pending["tt"]
    if pending_urls:
        print(f"  shared-profile queue: +{len(pending['ig'])} IG / +{len(pending['tt'])} TikTok")
    if not (ig_add or ig_rem or tt_add or tt_rem):
        print("No queued account edits."); return

    ia, ir = apply_to(ROOT / "accounts.json", ig_add, ig_rem)
    ta, tr = apply_to(ROOT / "tiktok_accounts.json", tt_add, tt_rem)
    print(f"Applied IG: +{ia} / -{ir}  |  TikTok: +{ta} / -{tr}")

    # consume the shared-profile blobs now they're applied (apply_to already
    # dedupes, so a failed delete just means a harmless re-apply next run)
    if pending_urls:
        try:
            blob_delete(pending_urls); print(f"Consumed {len(pending_urls)} shared-profile blobs.")
        except Exception as e:  # noqa: BLE001
            print(f"  could not delete shared-profile blobs (will re-apply next run): {str(e)[:80]}")

    # clear the queue (keep the rest of the state — bookmarks, dismissals)
    state["accountEdits"] = {"igAdd": [], "igRemove": [], "ttAdd": [], "ttRemove": []}
    if BLOB:
        try:
            blob_put_state(state); print("Cleared account-edit queue on backend.")
        except Exception as e:  # noqa: BLE001
            print(f"  could not clear queue (will retry next run): {str(e)[:80]}")


if __name__ == "__main__":
    main()
