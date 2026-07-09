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
import hashlib
import json
import os
import re
import subprocess
import tempfile
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

# ffmpeg transpose filter values for a CLOCKWISE correction of N degrees (some
# source videos have their content baked in sideways — not a container rotation
# flag ffmpeg/browsers would auto-correct on their own, the actual pixels are
# rotated — curate_posts.py's Claude vision pass judges this from the thumbnail
# it already reviewed and sets post["rotate"] to the clockwise degrees needed).
_ROTATE_FILTER = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}


def rotate_mp4(mp4_bytes, degrees):
    vf = _ROTATE_FILTER.get(degrees)
    if not vf:
        return mp4_bytes
    with tempfile.TemporaryDirectory() as td:
        src, dst = f"{td}/in.mp4", f"{td}/out.mp4"
        Path(src).write_bytes(mp4_bytes)
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-c:a", "copy", dst],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if r.returncode != 0 or not Path(dst).exists():
            return mp4_bytes  # best-effort — ship the unrotated video rather than fail the post
        return Path(dst).read_bytes()


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


def blob_put(pathname, data, content_type="video/mp4"):
    req = urllib.request.Request(f"{BLOB_API}/{pathname}", data=data, method="PUT", headers={
        "authorization": "Bearer " + BLOB, "x-content-type": content_type,
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
    """URLs to never (re-)download a video for: explicit removals (removedVideos —
    also covers the "Remove download" button on a post that's NOT dismissed, a
    separate valid action) UNION every currently-dismissed post (dismissed — dismissing
    is supposed to free its download too, per dismissWithConfirm, so honour that
    directly here rather than depending solely on removedVideos staying in sync).
    dismissed is the older, far more heavily-used field — relying on it too makes this
    robust even if removedVideos itself is incomplete (e.g. an active browser tab's
    stale in-memory copy overwriting a fresher server value between visits — a real,
    observed race in this single-user last-write-wins sync design).
    NOTE: match["url"] is still the public CDN URL under the hood (just discovered via
    the list API rather than hardcoded), so a read can be up to ~60s+ stale/incomplete
    relative to a very recent write — documented elsewhere in this project, no full fix
    without a timestamp to compare against. Harmless in real weekly-cadence usage (an
    edit is always at least days old by the next run) and self-healing regardless
    (every run re-reads this fresh). This retry only covers outright read failures,
    not partial staleness."""
    if not BLOB:
        return set()
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{BLOB_API}?prefix=state/dashboard-state.json",
                                         headers={"authorization": "Bearer " + BLOB})
            with urllib.request.urlopen(req, timeout=30) as r:
                blobs = json.loads(r.read()).get("blobs", [])
            match = next((b for b in blobs if b["pathname"] == "state/dashboard-state.json"), None)
            if not match:
                return set()
            with urllib.request.urlopen(f"{match['url']}?t={int(time.time()*1000)}", timeout=30) as r:
                state = json.loads(r.read())
            return set(state.get("removedVideos") or []) | set(state.get("dismissed") or [])
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return set()
            time.sleep(3)
    return set()


def main():
    if not (TIKHUB and BLOB):
        raise SystemExit("Need TIKHUB_TOKEN and BLOB_READ_WRITE_TOKEN in env.")
    data = json.loads((DASH / "data.json").read_text())
    removed = removed_set()
    if removed:
        print(f"Skipping {len(removed)} URLs the user removed from Blob (won't be re-downloaded).")
        # A dismiss+remove only clears the CURRENT week's copy of a post at the moment
        # it happens (client-side) — a post that's since rolled into an archived
        # weeks[] snapshot keeps its old, un-cleared video field forever, since nothing
        # else ever revisits frozen archives. Self-heal it here on every run: any post
        # anywhere in the archive whose URL is in `removed` gets its blob URL stripped,
        # so an old week can't keep serving a link to a video the user explicitly removed.
        archived_scrubbed = 0
        for wk_posts in (data.get("weeks") or {}).values():
            for p in (wk_posts.get("posts") or []):
                if p.get("url") in removed and "blob.vercel-storage" in (p.get("video") or ""):
                    p["video"] = ""; archived_scrubbed += 1
        if archived_scrubbed:
            print(f"  scrubbed {archived_scrubbed} stale video links from archived weeks.")
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
            if p.get("rotate"):
                mp4 = rotate_mp4(mp4, p["rotate"])
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

    # ---- ALSO self-host Inspiration Links (dashboard/api/save-link.js) — that feature
    # has never been wired into Blob hosting at all, so every saved link's `video` field
    # is whatever raw, SIGNED, hours-lived CDN URL TikHub returned at save time. Once
    # that expires the card silently falls back to the platform embed (or, if the post
    # itself is gone, the platform's own "broken link" state) — same failure mode board
    # posts had before this script existed. Reuses the SAME stable pathname scheme as
    # board posts (videos/<platform>_<code>.mp4), so a reel that's both a board post and
    # a saved link shares one Blob file — no duplicate storage, one fetch either way.
    li_ok = li_kept = li_fail = 0
    if BLOB:
        try:
            req = urllib.request.Request(f"{BLOB_API}?prefix=links/", headers={"authorization": "Bearer " + BLOB})
            with urllib.request.urlopen(req, timeout=30) as r:
                link_blobs = json.loads(r.read().decode()).get("blobs", [])
        except Exception:  # noqa: BLE001
            link_blobs = []
        for b in link_blobs:
            try:
                with urllib.request.urlopen(f"{b['url']}?t={int(time.time()*1000)}", timeout=30) as r:
                    link = json.loads(r.read().decode())
            except Exception:  # noqa: BLE001
                continue
            url = link.get("url", "")
            if link.get("type") not in ("reel", "video", "post"):  # audio pages, TikTok sounds, profiles — no single video
                continue
            if url in removed:  # user explicitly removed this download — honour it, don't re-fetch
                if "blob.vercel-storage" in (link.get("video") or ""):
                    link["video"] = ""
                    try:
                        blob_put(f"links/{hashlib.md5(url.encode()).hexdigest()}.json", json.dumps(link, ensure_ascii=False).encode(), content_type="application/json")
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if "blob.vercel-storage" in (link.get("video") or ""):
                li_kept += 1; continue
            plat = "tiktok" if link.get("platform") == "tt" else "instagram"
            code = tt_id(url) if plat == "tiktok" else ig_code(url)
            if not code:
                continue
            pathname = f"videos/{plat}_{code}.mp4"
            try:
                blob_url = by_url.get(url) or video_map.get(url)  # already hosted as a board post or example reel? reuse it
                if not blob_url:
                    src = fresh_source({"platform": plat, "url": url})
                    if not src:
                        li_fail += 1; continue
                    mp4 = download(src)
                    if len(mp4) < 5000:
                        li_fail += 1; continue
                    blob_url = blob_put(pathname, mp4)
                link["video"] = blob_url
                blob_put(f"links/{hashlib.md5(url.encode()).hexdigest()}.json", json.dumps(link, ensure_ascii=False).encode(), content_type="application/json")
                li_ok += 1
                print(f"  ✓ LINK {plat:9} @{(link.get('account') or '')[:16]:<16} {blob_url[-24:]}")
            except Exception as e:  # noqa: BLE001
                li_fail += 1
                print(f"  ✗ LINK {plat} {url[-18:]}: {str(e)[:50]}")
            time.sleep(0.2)

    # NO pruning — videos are kept PERMANENTLY so saved posts always play natively (no embed).
    total = len(blob_list())
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nBoard posts: {ok} new, {kept} kept, {fail} failed."
          f"\nExample reels: {ex_ok} new, {ex_kept} kept, {ex_fail} failed."
          f"\nInspiration links: {li_ok} new, {li_kept} kept, {li_fail} failed."
          f"\nBlob now holds {total} videos (kept permanently).")


if __name__ == "__main__":
    main()
