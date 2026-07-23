// Real audio download for a track or reel link, across both platforms.
//
// Two distinct outcomes depending on WHAT was linked, not which platform:
//  - "audio card" (an IG reels/audio/<id> page, or a TikTok music/x-<id> page) ->
//    the RAW original track, independent of any specific reel/video. Creators often
//    layer their own foley/SFX on top of a sound in their post, so extracting audio
//    from a random post using that sound is NOT the same as the clean original.
//    Named "{track title} ({audio ID}).m4a", e.g. "Sign of the Times (204373260395952).m4a".
//  - "reel card" (a direct IG reel/post link, or a TikTok video link) -> that specific
//    post's OWN audio (its video's audio track, whatever that contains — original
//    music, remix, voiceover, foley, all of it, since that's the point: it's THIS
//    post's audio, not the platform's canonical track).
//    Named "{account}-{audio ID or shortcode}.m4a".
//
// IG raw track: fetch_music_posts's `metadata.music_info.music_asset_info` has a
// progressive_download_url — but ONLY for registered/licensed tracks; music_info is
// null for creator-original audio, which falls back to extracting from a
// representative post's video instead (same as the reel-card path).
// TikTok raw track: fetch_music_detail's `music_info.play_url` is a direct audio-only
// URL — reliable, no retry loop needed (unlike IG's item-search lookup below).
//
// IG's reels/audio/<id> lookup (fetch_music_posts) is proxied IG-internal API and
// genuinely flaky — empirically ~1-in-3 to 1-in-9 calls return real data, the rest
// come back with an opaque `{attempts: N}` payload with no error message. We retry
// persistently (up to 10x), which in testing reliably turns up real data within a
// handful of attempts and is well within Vercel's function time budget. If it still
// fails, the client falls back to opening instasaver.io (confirmed by hand to work
// specifically for the reels/audio/ URL shape, unlike reelsave.app) — IG links only,
// no equivalent fallback tool verified for TikTok.
//
// Audio extraction/repackaging uses a real ffmpeg binary (ffmpeg-static — a native
// binary bundled via npm, not WASM; comfortably fits Vercel's 250MB function budget).
// `-c:a copy` re-packages the existing AAC stream losslessly with no re-encode.
//
//   POST /api/download-audio   { url }   header X-Edit-Secret
//   -> audio/mp4 (m4a) binary response, Content-Disposition: attachment
//   -> on failure: { ok:false, reason, fallback:"instasaver"|null }

const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const ffmpegPath = require("ffmpeg-static");

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15";

function deep(obj, ...keys) {
  let cur = obj;
  for (const k of keys) {
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
      for (const v of Object.values(o)) { const r = deepPlayUrl(v); if (r) return r; }
    } else {
      for (const v of o) { const r = deepPlayUrl(v); if (r) return r; }
    }
  }
  return "";
}

function kindOf(url) {
  if (/instagram\.com\/reels?\/audio\//.test(url)) return "ig-audio";
  if (/tiktok\.com\/music\//.test(url)) return "tt-music";
  if (/tiktok\.com/.test(url)) return "tt-video";
  return "ig-reel";
}
function igAudioIdFromUrl(url) {
  const m = (url || "").match(/\/reels?\/audio\/(\d+)/);
  return m ? m[1] : "";
}
function igShortcodeFromUrl(url) {
  const m = (url || "").match(/\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}
function ttMusicIdFromUrl(url) {
  const m = (url || "").match(/\/music\/[^/?]*-(\d+)/);
  return m ? m[1] : "";
}
function ttVideoIdFromUrl(url) {
  const m = (url || "").match(/\/video\/(\d+)/);
  return m ? m[1] : "";
}

async function tikhub(path_) {
  const r = await fetch("https://api.tikhub.io" + path_, {
    headers: { authorization: `Bearer ${process.env.TIKHUB_TOKEN}`, "user-agent": UA, accept: "application/json" },
  });
  if (!r.ok) return null;
  return r.json();
}

// fetch_post_by_url's response (Instagram's public/web shape) doesn't expose an
// audio/music asset ID at all — verified by walking its full response tree for any
// audio/music/sound/asset_id key; only a boolean `has_audio` is present. So for a
// direct reel/post link we fall back to the shortcode as the traceable identifier
// (still lets you find the exact reel, just not IG's internal audio ID).
async function lookupIgReel(url) {
  const j = await tikhub(`/api/v1/instagram/v1/fetch_post_by_url?post_url=${encodeURIComponent(url)}`);
  const d = (j && j.data) || {};
  if (!d.id) return null;
  // Carousels never have a top-level video_url (this endpoint's carousel shape is
  // GraphQL-style edge_sidecar_to_children, not the carousel_media field scrape.py
  // sees from a different endpoint) — verified live against real posts: some
  // carousels are all-photo (genuinely no audio — a real "nothing to download", not
  // a bug), others mix in video slides that DO have their own video_url and audio,
  // which this used to miss entirely by only checking the top-level field. Use the
  // first video slide found, if any.
  let video = d.video_url || "";
  if (!video) {
    const slides = deep(d, "edge_sidecar_to_children", "edges") || [];
    const videoSlide = slides.map((e) => e.node).find((n) => n && n.is_video && n.video_url);
    if (videoSlide) video = videoSlide.video_url;
  }
  return {
    account: deep(d, "owner", "username") || "",
    video,
    idLabel: "reel_" + (igShortcodeFromUrl(url) || d.shortcode || ""),
  };
}

// Empirically this endpoint can need 6-8+ tries before it returns real data (vs. an
// opaque `{attempts: N}` payload) — well within Vercel's function time budget, so we
// retry persistently rather than giving up after a couple of attempts.
const IG_MUSIC_LOOKUP_ATTEMPTS = 10;
async function lookupIgAudioPage(url) {
  const audioId = igAudioIdFromUrl(url);
  for (let attempt = 0; attempt < IG_MUSIC_LOOKUP_ATTEMPTS; attempt++) {
    const j = await tikhub(`/api/v1/instagram/v1/fetch_music_posts?music_url=${encodeURIComponent(url)}`);
    const d = deep(j, "data") || {};
    const items = d.items;
    if (Array.isArray(items) && items.length) {
      const ai = deep(d, "metadata", "music_info", "music_asset_info");
      const rawUrl = ai && (ai.progressive_download_url || ai.fast_start_progressive_download_url);
      if (rawUrl) return { rawAudio: rawUrl, title: ai.title || "", audioId };
      for (const it of items) {
        const m = it.media;
        const video = m && deep(m, "video_versions", 0, "url");
        if (video) return { account: deep(m, "user", "username") || "", video, idLabel: audioId };
      }
    }
    if (attempt < IG_MUSIC_LOOKUP_ATTEMPTS - 1) await new Promise((res) => setTimeout(res, 3000));
  }
  return null;
}

async function lookupTtMusic(url) {
  const musicId = ttMusicIdFromUrl(url);
  if (!musicId) return null;
  const j = await tikhub(`/api/v1/tiktok/app/v3/fetch_music_detail?music_id=${musicId}`);
  const mi = deep(j, "data", "music_info");
  if (!mi || mi.prevent_download) return null;
  const playUrl = deep(mi, "play_url", "url_list", 0);
  if (!playUrl) return null;
  return { rawAudio: playUrl, title: mi.title || "", audioId: musicId };
}

async function lookupTtVideo(url) {
  const aid = ttVideoIdFromUrl(url);
  if (!aid) return null;
  const j = await tikhub(`/api/v1/tiktok/app/v3/fetch_one_video?aweme_id=${aid}`);
  const a = deep(j, "data", "aweme_detail") || deep(j, "data") || {};
  const video = deepPlayUrl(a.video || {});
  if (!video) return null;
  return {
    account: deep(a, "author", "unique_id") || deep(a, "author", "uniqueId") || "",
    video,
    idLabel: "video_" + aid,
  };
}

// Preserves spaces/parens/apostrophes (wanted in the filename) but strips
// characters invalid across filesystems.
function sanitizeTitle(s) {
  return (s || "Unknown").replace(/[/\\:*?"<>|]/g, "").trim().slice(0, 100);
}
function sanitize(s) {
  return (s || "unknown").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 60);
}

async function extractAudio(sourceUrl) {
  const tmpIn = path.join(os.tmpdir(), `dl_in_${Date.now()}.mp4`);
  const tmpOut = path.join(os.tmpdir(), `dl_out_${Date.now()}.m4a`);
  try {
    const vr = await fetch(sourceUrl, { headers: { "user-agent": UA } });
    if (!vr.ok) throw new Error("source fetch failed");
    fs.writeFileSync(tmpIn, Buffer.from(await vr.arrayBuffer()));
    await new Promise((resolve, reject) => {
      execFile(ffmpegPath, ["-y", "-i", tmpIn, "-vn", "-acodec", "copy", tmpOut], (err) => {
        if (err) reject(err); else resolve();
      });
    });
    return fs.readFileSync(tmpOut);
  } finally {
    try { fs.unlinkSync(tmpIn); } catch (e) {}
    try { fs.unlinkSync(tmpOut); } catch (e) {}
  }
}

module.exports = async (req, res) => {
  const secret = process.env.EDIT_SECRET;
  const tikhubToken = process.env.TIKHUB_TOKEN;

  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "X-Edit-Secret, Content-Type");
  if (req.method === "OPTIONS") { res.status(200).end(); return; }

  if (!secret || !tikhubToken) {
    res.status(503).json({ ok: false, reason: "backend not configured (set EDIT_SECRET + TIKHUB_TOKEN in Vercel)" });
    return;
  }
  const pass = req.headers["x-edit-secret"];
  if (pass !== secret) { res.status(401).json({ ok: false, reason: "unauthorized" }); return; }
  if (req.method !== "POST") { res.status(405).json({ ok: false, reason: "method not allowed" }); return; }

  const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
  const url = (body.url || "").trim();
  if (!url) { res.status(400).json({ ok: false, reason: "url required" }); return; }

  const kind = kindOf(url);
  let found;
  try {
    found = kind === "ig-audio" ? await lookupIgAudioPage(url)
      : kind === "tt-music" ? await lookupTtMusic(url)
      : kind === "tt-video" ? await lookupTtVideo(url)
      : await lookupIgReel(url);
  } catch (e) {
    found = null;
  }
  if (!found || (!found.video && !found.rawAudio)) {
    // Distinguish "found the post, it's just genuinely a photo-only carousel" (no
    // video anywhere = no audio to extract, an honest outcome) from "couldn't even
    // resolve the post" (found === null) — both used to say the same opaque
    // "lookup_failed", which read as a bug even when it wasn't one.
    const reason = kind === "ig-reel" && found ? "no_audio_photo_carousel" : "lookup_failed";
    res.status(200).json({ ok: false, reason, fallback: kind === "ig-audio" ? "instasaver" : null });
    return;
  }

  try {
    const audio = await extractAudio(found.rawAudio || found.video);
    const filename = found.rawAudio
      ? `${sanitizeTitle(found.title || "Original audio")} (${found.audioId}).m4a`
      : `${sanitize(found.account)}-${sanitize(found.idLabel)}.m4a`;
    res.setHeader("Content-Type", "audio/mp4");
    res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    res.status(200).send(audio);
  } catch (e) {
    res.status(200).json({ ok: false, reason: "extract_failed", fallback: kind === "ig-audio" ? "instasaver" : null });
  }
};
