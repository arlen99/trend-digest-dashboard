// Real audio-only download for an Instagram link — either a "reels/audio/<id>"
// music page or a direct reel/post link. Named account_name-audio_ID.m4a so the
// exact audio can be traced back to its source later.
//
// Two lookup paths depending on what was pasted:
//  - reel/post link  -> fetch_post_by_url (reliable, same call save-link.js uses)
//  - reels/audio link -> fetch_music_posts?music_url=... (an IG-internal endpoint
//    proxied by TikHub; genuinely flaky — empirically ~1-in-3 to 1-in-9 calls
//    return real data, the rest come back with an opaque `{attempts: N}` payload
//    with no error message). We retry a few times with a short delay, which in
//    testing reliably turns up real post data within 2-3 attempts. If it still
//    fails, the client falls back to opening instasaver.io (confirmed by hand to
//    work specifically for the reels/audio/ URL shape, unlike reelsave.app).
//
// Once we have a representative post's video URL, we download it and extract just
// the audio stream with a real ffmpeg binary (ffmpeg-static — a native binary
// bundled via npm, not WASM; comfortably fits Vercel's 250MB function budget).
// `-c:a copy` re-packages the existing AAC stream losslessly with no re-encode,
// so this is fast and avoids picking an arbitrary target bitrate.
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

async function tikhub(path_) {
  const r = await fetch("https://api.tikhub.io" + path_, {
    headers: { authorization: `Bearer ${process.env.TIKHUB_TOKEN}`, "user-agent": UA, accept: "application/json" },
  });
  if (!r.ok) return null;
  return r.json();
}

function extractFromMedia(m) {
  if (!m || !m.code) return null;
  const cm = m.clips_metadata || {};
  const mi = cm.music_info || {};
  const ai = mi.music_asset_info || {};
  const osi = cm.original_sound_info || {};
  const audioId = ai.audio_cluster_id || cm.music_canonical_id || osi.audio_asset_id || "";
  return {
    account: deep(m, "user", "username") || "",
    video: deep(m, "video_versions", 0, "url") || "",
    audioId: String(audioId || ""),
  };
}

async function lookupByPostUrl(url) {
  const j = await tikhub(`/api/v1/instagram/v1/fetch_post_by_url?post_url=${encodeURIComponent(url)}`);
  const d = (j && j.data) || {};
  if (!d.id) return null;
  const cm = d.clips_metadata || {}; // fetch_post_by_url's shape mirrors the app API closely
  const audioId = deep(cm, "music_info", "music_asset_info", "audio_cluster_id")
    || cm.music_canonical_id
    || deep(cm, "original_sound_info", "audio_asset_id") || "";
  return {
    account: deep(d, "owner", "username") || "",
    video: d.video_url || "",
    audioId: String(audioId || ""),
  };
}

async function lookupByMusicUrl(url) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const j = await tikhub(`/api/v1/instagram/v1/fetch_music_posts?music_url=${encodeURIComponent(url)}`);
    const items = deep(j, "data", "items");
    if (Array.isArray(items) && items.length) {
      for (const it of items) {
        const found = extractFromMedia(it.media);
        if (found && found.video) return found;
      }
    }
    if (attempt < 2) await new Promise((res) => setTimeout(res, 3000));
  }
  return null;
}

function sanitize(s) {
  return (s || "unknown").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 60);
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
  if (!found || !found.video) {
    res.status(200).json({ ok: false, reason: "lookup_failed", fallback: audioLink ? "instasaver" : null });
    return;
  }

  const tmpIn = path.join(os.tmpdir(), `dl_in_${Date.now()}.mp4`);
  const tmpOut = path.join(os.tmpdir(), `dl_out_${Date.now()}.m4a`);
  try {
    const vr = await fetch(found.video, { headers: { "user-agent": UA } });
    if (!vr.ok) throw new Error("video fetch failed");
    fs.writeFileSync(tmpIn, Buffer.from(await vr.arrayBuffer()));

    await new Promise((resolve, reject) => {
      execFile(ffmpegPath, ["-y", "-i", tmpIn, "-vn", "-acodec", "copy", tmpOut], (err) => {
        if (err) reject(err); else resolve();
      });
    });

    const audio = fs.readFileSync(tmpOut);
    const filename = `${sanitize(found.account)}-${sanitize(found.audioId)}.m4a`;
    res.setHeader("Content-Type", "audio/mp4");
    res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    res.status(200).send(audio);
  } catch (e) {
    res.status(200).json({ ok: false, reason: "extract_failed", fallback: audioLink ? "instasaver" : null });
  } finally {
    try { fs.unlinkSync(tmpIn); } catch (e) {}
    try { fs.unlinkSync(tmpOut); } catch (e) {}
  }
};
