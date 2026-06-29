#!/usr/bin/env python3
"""
Self-host the displayed posts' videos on Vercel Blob so they play INLINE natively
and never expire (IG/TikTok signed CDN URLs die in hours).

Per displayed video post: fetch a FRESH source URL (IG via fetch_post_by_url, TikTok
via fetch_one_video — the web endpoint strips it), download the MP4, upload to Blob at
a STABLE pathname (videos/<platform>_<shortcode>.mp4 → stable public URL), and point
the card's `video` field at the Blob URL. Then PRUNE: delete any Blob video no longer
referenced, so storage stays bounded (~the displayed set), never accumulating.

Pure stdlib (urllib) — runs in CI with no extra deps.
Usage: set -a && . ./.env && set +a && python3 fetch_videos.py
Env: TIKHUB_TOKEN, BLOB_READ_WRITE_TOKEN.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
TIKHUB = os.environ.get("TIKHUB_TOKEN")
BLOB = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://blob.vercel-storage.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16 Safari/605.1.15")


def ig_code(u):
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", u or "");  return m.group(1) if m else ""


def tt_id(u):
    m = re.search(r"/video/(\d+)", u or "");  return m.group(1) if m else ""


def th(path):
    for _ in range(4):
        try:
            req = urllib.request.Request("https://api.tikhub.io" + path,
                                         headers={"Authorization": "Bearer " + TIKHUB, "User-Agent": UA, "accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    return {}


def deep_play_url(o):
    if isinstance(o, dict):
        pa = o.get("play_addr") or o.get("download_addr")
        if isinstance(pa, dict) and pa.get("url_list"):
            return pa["url_list"][0]
        for v in o.values():
            r = deep_play_url(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = deep_play_url(v)
            if r:
                return r
    return ""


def fresh_source(p):
    """Fresh, downloadable MP4 URL for a post (IG or TikTok)."""
    if p.get("platform") == "tiktok":
        aid = tt_id(p["url"])
        return deep_play_url(th(f"/api/v1/tiktok/app/v3/fetch_one_video?aweme_id={aid}")) if aid else ""
    d = th(f"/api/v1/instagram/v1/fetch_post_by_url?post_url={urllib.parse.quote(p['url'])}")
    return (d.get("data", {}) or {}).get("video_url", "")


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def blob_put(pathname, data):
    req = urllib.request.Request(f"{BLOB_API}/{pathname}", data=data, method="PUT", headers={
        "authorization": "Bearer " + BLOB, "x-content-type": "video/mp4",
        "x-add-random-suffix": "0", "x-allow-overwrite": "1", "x-api-version": "7"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())["url"]


def blob_list(prefix="videos/"):
    req = urllib.request.Request(f"{BLOB_API}?prefix={prefix}", headers={"authorization": "Bearer " + BLOB})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("blobs", [])


def blob_delete(urls):
    if not urls:
        return
    req = urllib.request.Request(f"{BLOB_API}/delete", method="POST",
                                 data=json.dumps({"urls": urls}).encode(),
                                 headers={"authorization": "Bearer " + BLOB, "content-type": "application/json", "x-api-version": "7"})
    urllib.request.urlopen(req, timeout=60).read()


def removed_set():
    """URLs the user explicitly removed from Blob via the dashboard → never re-download."""
    if not BLOB:
        return set()
    try:
        req = urllib.request.Request(f"{BLOB_API}?prefix=state/dashboard-state.json",
                                     headers={"authorization": "Bearer " + BLOB})
        with urllib.request.urlopen(req, timeout=30) as r:
            blobs = json.loads(r.read()).get("blobs", [])
        match = next((b for b in blobs if b["pathname"] == "state/dashboard-state.json"), None)
        if not match:
            return set()
        with urllib.request.urlopen(match["url"], timeout=30) as r:
            state = json.loads(r.read())
        return set(state.get("removedVideos") or [])
    except Exception:  # noqa: BLE001
        return set()


def main():
    if not (TIKHUB and BLOB):
        raise SystemExit("Need TIKHUB_TOKEN and BLOB_READ_WRITE_TOKEN in env.")
    data = json.loads((DASH / "data.json").read_text())
    removed = removed_set()
    if removed:
        print(f"Skipping {len(removed)} URLs the user removed from Blob (won't be re-downloaded).")
    # displayed posts that are single videos (skip carousels/photos)
    vids = [p for p in data["posts"] if p.get("platform") == "tiktok"
            or (p.get("format") == "Reel" and not p.get("carousel"))]
    ok, fail, kept = 0, 0, 0
    for p in vids:
        plat = p.get("platform", "instagram")
        code = tt_id(p["url"]) if plat == "tiktok" else ig_code(p["url"])
        if not code:
            continue
        if p["url"] in removed:
            # user removed this from Blob via the dashboard — honour that, drop any stale blob URL
            if "blob.vercel-storage" in (p.get("video") or ""):
                p["video"] = ""
            continue
        # already self-hosted on Blob? keep it — Blob URLs never expire, so no re-fetch
        if "blob.vercel-storage" in (p.get("video") or ""):
            kept += 1
            continue
        pathname = f"videos/{plat}_{code}.mp4"
        try:
            src = fresh_source(p)
            if not src:
                fail += 1; continue
            mp4 = download(src)
            if len(mp4) < 5000:
                fail += 1; continue
            p["video"] = blob_put(pathname, mp4)
            ok += 1
            print(f"  ✓ {plat:9} @{p['account'][:16]:<16} {len(mp4)//1024:>5}KB")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ✗ {plat} @{p.get('account')}: {str(e)[:50]}")
        time.sleep(0.2)

    # ---- ALSO self-host example/sample reels referenced by Trend Radar cards + Audio chart,
    # so the in-app popup plays natively instead of falling back to the platform embed.
    ex_urls = set()
    for t in data.get("trends", []):
        for u in (t.get("examples") or []):
            ex_urls.add(u)
    for s in data.get("soundChart", []):
        for u in (s.get("samples") or []):
            ex_urls.add(u)
    video_map = data.get("videos", {}) or {}
    # any URL already in the post pool? reuse its Blob URL — no re-fetch
    by_url = {p["url"]: p.get("video", "") for p in data["posts"] if "blob.vercel-storage" in (p.get("video") or "")}
    ex_ok = ex_kept = ex_fail = 0
    for url in ex_urls:
        if url in removed:
            # user removed this from Blob via the dashboard — honour the choice; strip any stale mapping
            video_map.pop(url, None)
            continue
        if url in video_map and "blob.vercel-storage" in video_map[url]:
            ex_kept += 1; continue
        if url in by_url:
            video_map[url] = by_url[url]; ex_kept += 1; continue
        plat = "tiktok" if "tiktok.com" in url else "instagram"
        code = tt_id(url) if plat == "tiktok" else ig_code(url)
        if not code:
            continue
        pathname = f"videos/{plat}_{code}.mp4"
        try:
            src = fresh_source({"platform": plat, "url": url})
            if not src:
                ex_fail += 1; continue
            mp4 = download(src)
            if len(mp4) < 5000:
                ex_fail += 1; continue
            video_map[url] = blob_put(pathname, mp4); ex_ok += 1
            print(f"  ✓ EX {plat:9} {url.split('/')[-2][:14]:<14} {len(mp4)//1024:>5}KB")
        except Exception as e:  # noqa: BLE001
            ex_fail += 1
            print(f"  ✗ EX {plat} {url[-18:]}: {str(e)[:50]}")
        time.sleep(0.2)
    data["videos"] = video_map

    # NO pruning — videos are kept PERMANENTLY so saved posts always play natively (no embed).
    total = len(blob_list())
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nBoard posts: {ok} new, {kept} kept, {fail} failed."
          f"\nExample reels: {ex_ok} new, {ex_kept} kept, {ex_fail} failed."
          f"\nBlob now holds {total} videos (kept permanently).")


if __name__ == "__main__":
    main()
