// Real audio download for an Instagram link — either a "reels/audio/<id>" music
// page or a direct reel/post link.
//
// For a reels/audio/<id> link with a REGISTERED/licensed track, IG's own music
// metadata (fetch_music_posts) includes a `progressive_download_url` — the raw,
// complete original track (e.g. the full 5:40 studio recording), independent of
// any specific reel. That's what we serve when it's available: creators often
// layer their own foley/SFX on top of a sound in their reel, so extracting audio
// from a random reel using that sound is NOT the same as the clean original track.
// Named "{track title} ({audio ID}).m4a", e.g. "Sign of the Times (204373260395952).m4a".
//
// Fallback path (used for creator-original audio with no registered-track metadata,
// or when the audio-page lookup fails/times out): extract audio from a representative
// reel's video instead — same as before, named "{account}-{audio ID or shortcode}.m4a".
//
// Direct reel/post links (no reels/audio/ in the URL) always use the fallback path,
// since fetch_post_by_url's response has no audio metadata at all (verified by
// walking its full response tree — only a boolean `has_audio` exists there).
//
// The reels/audio/<id> lookup (fetch_music_posts) is proxied IG-internal API and
// genuinely flaky — empirically ~1-in-3 to 1-in-9 calls return real data, the rest
// come back with an opaque `{attempts: N}` payload with no error message. We retry
// a few times, which in testing reliably turns up real data within 2-3 attempts.
// If it still fails, the client falls back to opening instasaver.io (confirmed by
// hand to work specifically for the reels/audio/ URL shape, unlike reelsave.app).
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

function isAudioLink(url) {
  return /instagram\.com\/reels?\/audio\//.test(url);
}
function audioIdFromUrl(url) {
  const m = (url || "").match(/\/reels?\/audio\/(\d+)/);
  return m ? m[1] : "";
}
function shortcodeFromUrl(url) {
  const m = (url || "").match(/\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/);
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
async function lookupByPostUrl(url) {
  const j = await tikhub(`/api/v1/instagram/v1/fetch_post_by_url?post_url=${encodeURIComponent(url)}`);
  const d = (j && j.data) || {};
  if (!d.id) return null;
  return {
    account: deep(d, "owner", "username") || "",
    video: d.video_url || "",
    idLabel: "reel_" + (shortcodeFromUrl(url) || d.shortcode || ""),
  };
}

// For a reels/audio/<id> link: try to get the RAW track first (registered/licensed
// sounds only — music_info is null for creator-original audio). Falls back to a
// representative reel's video if no raw track is available.
// Empirically this endpoint can need 6-8+ tries before it returns real data (vs. an
// opaque `{attempts: N}` payload) — well within Vercel's function time budget, so we
// retry persistently rather than giving up after a couple of attempts.
const MUSIC_LOOKUP_ATTEMPTS = 10;
async function lookupByMusicUrl(url) {
  const audioId = audioIdFromUrl(url);
  for (let attempt = 0; attempt < MUSIC_LOOKUP_ATTEMPTS; attempt++) {
    const j = await tikhub(`/api/v1/instagram/v1/fetch_music_posts?music_url=${encodeURIComponent(url)}`);
    const d = deep(j, "data") || {};
    const items = d.items;
    if (Array.isArray(items) && items.length) {
      const ai = deep(d, "metadata", "music_info", "music_asset_info");
      const rawUrl = ai && (ai.progressive_download_url || ai.fast_start_progressive_download_url);
      if (rawUrl) {
        return { rawAudio: rawUrl, title: ai.title || "", audioId };
      }
      for (const it of items) {
        const m = it.media;
        const video = m && deep(m, "video_versions", 0, "url");
        if (video) return { account: deep(m, "user", "username") || "", video, idLabel: audioId };
      }
    }
    if (attempt < MUSIC_LOOKUP_ATTEMPTS - 1) await new Promise((res) => setTimeout(res, 3000));
  }
  return null;
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

  const audioLink = isAudioLink(url);
  let found;
  try {
    found = audioLink ? await lookupByMusicUrl(url) : await lookupByPostUrl(url);
  } catch (e) {
    found = null;
  }
  if (!found || (!found.video && !found.rawAudio)) {
    res.status(200).json({ ok: false, reason: "lookup_failed", fallback: audioLink ? "instasaver" : null });
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
    res.status(200).json({ ok: false, reason: "extract_failed", fallback: audioLink ? "instasaver" : null });
  }
};
