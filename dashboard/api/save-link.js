// Accepts a shared URL (IG post/reel/audio, TikTok video/sound) from the iOS Shortcut
// and stores it in the Inspiration Links section of Saved.
//
// Each link is its own blob at links/<md5(url)>.json — NOT a read-modify-write against
// a single shared state file. A prior version read the whole dashboard-state.json,
// appended to it, and wrote it back; back-to-back saves from the Shortcut raced against
// each other (the second write's read didn't see the first write yet) and silently
// dropped links. Keying each link by hash of its own URL makes saves atomic (a write is
// either fully there or not) and naturally idempotent (re-saving the same URL just
// overwrites its own file, no dedup logic needed).
//
// On save, if TIKHUB_TOKEN is configured, we also pull real metrics (views/likes/
// comments/thumbnail/video/account/caption) the same way the main scrape pipeline does
// (fetch_post_by_url for IG, fetch_one_video for TikTok) — this is a single HTTP call,
// no native deps, so it runs fine in a Vercel Node function. The on-screen text (videoText)
// is NOT OCR'd here: that needs on-device Vision (see tiktok_videotext.py), which can't
// run in this serverless environment. Run `python3 enrich_links.py` locally to backfill
// on-screen text for saved links (same OCR mechanism the weekly pipeline already uses).
//
//   POST   /api/save-link   { url, note? }        -> save/update one link (+ metrics)
//   GET    /api/save-link                          -> list all links
//   DELETE /api/save-link   { url }                -> remove one link
// Auth: X-Edit-Secret header (or ?secret=), same as /api/state.

const crypto = require("crypto");

const BLOB = "https://blob.vercel-storage.com";
const PREFIX = "links/";
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15";

function keyFor(url) {
  return PREFIX + crypto.createHash("md5").update(url).digest("hex") + ".json";
}

function parseLink(url) {
  try {
    const u = new URL(url);
    const h = u.hostname.replace("www.", "");
    const p = u.pathname;
    if (h === "instagram.com") {
      if (p.includes("/reels/audio/")) return { platform: "ig", type: "audio", label: "IG Audio" };
      if (p.includes("/reel/"))        return { platform: "ig", type: "reel",  label: "IG Reel" };
      if (p.includes("/p/"))           return { platform: "ig", type: "post",  label: "IG Post" };
      if (p.includes("/stories/"))     return { platform: "ig", type: "story", label: "IG Story" };
      return { platform: "ig", type: "post", label: "Instagram" };
    }
    if (h === "tiktok.com" || h === "vm.tiktok.com" || h === "vt.tiktok.com") {
      if (p.includes("/music/")) return { platform: "tt", type: "sound", label: "TikTok Sound" };
      if (p.includes("/video/")) return { platform: "tt", type: "video", label: "TikTok Video" };
      return { platform: "tt", type: "video", label: "TikTok" };
    }
    return { platform: "other", type: "link", label: h };
  } catch (e) {
    return { platform: "other", type: "link", label: "Link" };
  }
}

function igCode(url) {
  const m = (url || "").match(/\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}
function ttId(url) {
  const m = (url || "").match(/\/video\/(\d+)/);
  return m ? m[1] : "";
}
function deep(obj, ...path) {
  let cur = obj;
  for (const k of path) {
    if (cur == null) return undefined;
    cur = cur[k];
  }
  return cur;
}
function deepPlayUrl(o) {
  if (o && typeof o === "object") {
    if (!Array.isArray(o)) {
      const pa = o.play_addr || o.download_addr;
      if (pa && Array.isArray(pa.url_list) && pa.url_list.length) return pa.url_list[0];
      for (const v of Object.values(o)) {
        const r = deepPlayUrl(v);
        if (r) return r;
      }
    } else {
      for (const v of o) {
        const r = deepPlayUrl(v);
        if (r) return r;
      }
    }
  }
  return "";
}
function deepCoverUrl(o) {
  if (o && typeof o === "object" && !Array.isArray(o)) {
    for (const key of ["cover", "origin_cover", "dynamic_cover"]) {
      const c = o[key];
      if (c && Array.isArray(c.url_list) && c.url_list.length) return c.url_list[0];
    }
    for (const v of Object.values(o)) {
      const r = deepCoverUrl(v);
      if (r) return r;
    }
  } else if (Array.isArray(o)) {
    for (const v of o) {
      const r = deepCoverUrl(v);
      if (r) return r;
    }
  }
  return "";
}

async function tikhub(path, token) {
  const r = await fetch("https://api.tikhub.io" + path, {
    headers: { authorization: `Bearer ${token}`, "user-agent": UA, accept: "application/json" },
  });
  if (!r.ok) return null;
  return r.json();
}

async function fetchIgMetrics(url, token) {
  const j = await tikhub(`/api/v1/instagram/v1/fetch_post_by_url?post_url=${encodeURIComponent(url)}`, token);
  const d = (j && j.data) || {};
  if (!d.id) return null;
  const likes = deep(d, "edge_media_preview_like", "count") || 0;
  const comments = deep(d, "edge_media_to_parent_comment", "count") || 0;
  const views = d.video_play_count || d.video_view_count || 0;
  const captionNode = deep(d, "edge_media_to_caption", "edges", 0, "node");
  return {
    account: deep(d, "owner", "username") || "",
    thumbnail: d.thumbnail_src || d.display_url || "",
    video: d.video_url || "",
    views, likes, comments,
    engagement: likes + comments,
    caption: (captionNode && captionNode.text) || "",
    timestamp: d.taken_at_timestamp ? new Date(d.taken_at_timestamp * 1000).toISOString() : "",
  };
}

async function fetchTtMetrics(url, token) {
  const id = ttId(url);
  if (!id) return null;
  const j = await tikhub(`/api/v1/tiktok/app/v3/fetch_one_video?aweme_id=${id}`, token);
  const a = deep(j, "data", "aweme_detail") || deep(j, "data") || {};
  const st = a.statistics || {};
  const likes = st.digg_count || 0;
  const comments = st.comment_count || 0;
  const shares = st.share_count || 0;
  const views = st.play_count || 0;
  return {
    account: deep(a, "author", "unique_id") || deep(a, "author", "uniqueId") || "",
    thumbnail: deepCoverUrl(a.video || {}),
    video: deepPlayUrl(a.video || {}),
    views, likes, comments, shares,
    engagement: likes + comments,
    caption: a.desc || "",
    timestamp: a.create_time ? new Date(a.create_time * 1000).toISOString() : "",
  };
}

async function listLinks(token) {
  const r = await fetch(`${BLOB}?prefix=${encodeURIComponent(PREFIX)}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!r.ok) return [];
  const j = await r.json();
  const blobs = j.blobs || [];
  const items = await Promise.all(
    blobs.map(async (b) => {
      try {
        const rr = await fetch(`${b.url}?t=${Date.now()}`, { cache: "no-store" });
        return rr.ok ? await rr.json() : null;
      } catch (e) {
        return null;
      }
    })
  );
  return items.filter(Boolean).sort((a, b) => (b.savedAt || "").localeCompare(a.savedAt || ""));
}

async function readLink(token, url) {
  const pathname = keyFor(url);
  const r = await fetch(`${BLOB}?prefix=${encodeURIComponent(pathname)}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!r.ok) return null;
  const j = await r.json();
  const b = (j.blobs || []).find((x) => x.pathname === pathname);
  if (!b) return null;
  const rr = await fetch(`${b.url}?t=${Date.now()}`, { cache: "no-store" });
  return rr.ok ? await rr.json() : null;
}

async function writeLink(token, url, patch) {
  const existing = await readLink(token, url);
  const { platform, type, label } = parseLink(url);
  const base = existing || { url, platform, type, label, note: "", savedAt: new Date().toISOString() };
  const merged = Object.assign({}, base, patch, { url, updatedAt: new Date().toISOString() });
  await fetch(`${BLOB}/${keyFor(url)}`, {
    method: "PUT",
    headers: {
      authorization: `Bearer ${token}`,
      "x-content-type": "application/json",
      "x-add-random-suffix": "0",
      "x-allow-overwrite": "1",
      "x-api-version": "7",
      "x-cache-control-max-age": "0",
    },
    body: JSON.stringify(merged),
  });
  return merged;
}

async function deleteLink(token, url) {
  const pathname = keyFor(url);
  const r = await fetch(`${BLOB}?prefix=${encodeURIComponent(pathname)}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!r.ok) return false;
  const j = await r.json();
  const b = (j.blobs || []).find((x) => x.pathname === pathname);
  if (!b) return false;
  await fetch(`${BLOB}/delete`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", "x-api-version": "7" },
    body: JSON.stringify({ urls: [b.url] }),
  });
  return true;
}

module.exports = async (req, res) => {
  const token = process.env.BLOB_READ_WRITE_TOKEN;
  const secret = process.env.EDIT_SECRET;
  const tikhubToken = process.env.TIKHUB_TOKEN;

  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "X-Edit-Secret, Content-Type");

  if (req.method === "OPTIONS") { res.status(200).end(); return; }

  if (!token || !secret) {
    res.status(503).json({ error: "backend not configured (set EDIT_SECRET + BLOB_READ_WRITE_TOKEN in Vercel)" });
    return;
  }

  const pass = req.headers["x-edit-secret"] || (req.query && req.query.secret);
  if (pass !== secret) { res.status(401).json({ error: "unauthorized" }); return; }

  try {
    if (req.method === "GET") {
      res.status(200).json({ links: await listLinks(token) });
      return;
    }
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const url = (body.url || "").trim();
    if (!url) { res.status(400).json({ error: "url required" }); return; }

    if (req.method === "POST") {
      const { platform } = parseLink(url);
      let metrics = null;
      // videoText (on-screen OCR from enrich_links.py) is passed straight through in
      // `patch` alongside note — writeLink merges rather than replaces.
      const patch = { note: (body.note || "").trim() };
      if (body.videoText !== undefined) patch.videoText = body.videoText;
      if (tikhubToken && (platform === "ig" || platform === "tt")) {
        try {
          metrics = platform === "ig" ? await fetchIgMetrics(url, tikhubToken) : await fetchTtMetrics(url, tikhubToken);
        } catch (e) { metrics = null; }
      }
      if (metrics) Object.assign(patch, metrics);
      const saved = await writeLink(token, url, patch);
      res.status(200).json({ ok: true, link: saved, enriched: !!metrics });
      return;
    }
    if (req.method === "DELETE") {
      const found = await deleteLink(token, url);
      res.status(200).json({ ok: true, found });
      return;
    }
    res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 120) });
  }
};
