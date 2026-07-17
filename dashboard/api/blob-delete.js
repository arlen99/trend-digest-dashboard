// Passphrase-gated: delete a self-hosted reel from Vercel Blob and record it as
// "removed" in the synced state so fetch_videos.py doesn't re-download it.
//
//   POST /api/blob-delete   header X-Edit-Secret
//   body: { url: "<reel permalink>" }
//   → { ok: true, freedKB?: number }
//
// Reuses BLOB_READ_WRITE_TOKEN + EDIT_SECRET (already set in Vercel).

const BLOB_API = "https://blob.vercel-storage.com";
const STATE_PATH = "state/dashboard-state.json";

function igCode(u) { const m = (u || "").match(/\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/); return m ? m[1] : ""; }
function ttId(u)  { const m = (u || "").match(/\/video\/(\d+)/); return m ? m[1] : ""; }

async function blobUrlFor(pathname, token) {
  const r = await fetch(`${BLOB_API}?prefix=${encodeURIComponent(pathname)}`, { headers: { authorization: `Bearer ${token}` } });
  if (!r.ok) return null;
  const j = await r.json();
  const b = (j.blobs || []).find(x => x.pathname === pathname);
  return b || null;
}

async function blobDelete(url, token) {
  const r = await fetch(`${BLOB_API}/delete`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", "x-api-version": "7" },
    body: JSON.stringify({ urls: [url] }),
  });
  return r.ok;
}

async function readState(token) {
  const meta = await blobUrlFor(STATE_PATH, token);
  if (!meta) return {};
  const r = await fetch(`${meta.url}?t=${Date.now()}`, { cache: "no-store" });
  return r.ok ? await r.json() : {};
}

// Previously unchecked, same bug as state.js's writeState() — a failed PUT was
// silently swallowed and the handler still reported ok:true, so "removed" never
// actually got recorded when the store rejects writes.
async function writeState(token, state) {
  const r = await fetch(`${BLOB_API}/${STATE_PATH}`, {
    method: "PUT",
    headers: {
      authorization: `Bearer ${token}`,
      "x-content-type": "application/json",
      "x-add-random-suffix": "0",
      "x-allow-overwrite": "1",
      "x-api-version": "7",
      "x-cache-control-max-age": "0",
    },
    body: JSON.stringify(state),
  });
  if (!r.ok) {
    const msg = (await r.text().catch(() => "")).slice(0, 140);
    throw new Error(`blob PUT ${r.status}: ${msg}`);
  }
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  const token = process.env.BLOB_READ_WRITE_TOKEN, secret = process.env.EDIT_SECRET;
  if (!token || !secret) { res.status(503).json({ ok: false, reason: "backend not configured" }); return; }
  const pass = req.headers["x-edit-secret"];
  if (pass !== secret) { res.status(401).json({ ok: false, reason: "unauthorized" }); return; }
  if (req.method !== "POST") { res.status(405).json({ ok: false, reason: "method not allowed" }); return; }
  try {
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const url = body.url;
    if (!url) { res.status(400).json({ ok: false, reason: "missing_url" }); return; }
    const plat = url.includes("tiktok.com") ? "tiktok" : "instagram";
    const code = plat === "tiktok" ? ttId(url) : igCode(url);
    if (!code) { res.status(400).json({ ok: false, reason: "bad_url" }); return; }
    const pathname = `videos/${plat}_${code}.mp4`;
    const blob = await blobUrlFor(pathname, token);
    let freedKB = 0;
    if (blob) {
      freedKB = Math.round((blob.size || 0) / 1024);
      await blobDelete(blob.url, token);
    }
    // record in state so fetch_videos.py + dashboard hydrate know to skip it
    const state = await readState(token) || {};
    const removed = Array.isArray(state.removedVideos) ? state.removedVideos : [];
    if (!removed.includes(url)) removed.push(url);
    state.removedVideos = removed;
    await writeState(token, state);
    res.status(200).json({ ok: true, freedKB, foundBlob: !!blob });
  } catch (e) {
    res.status(500).json({ ok: false, reason: String(e).slice(0, 120) });
  }
};
