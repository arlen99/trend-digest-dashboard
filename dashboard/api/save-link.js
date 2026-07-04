// Accepts a shared URL (IG post/reel/audio, TikTok video/sound) from the iOS Shortcut
// and appends it to inspirationLinks in the shared Blob state.
// POST {url, note?} with X-Edit-Secret header (same secret as /api/state).

const BLOB = "https://blob.vercel-storage.com";
const PATH = "state/dashboard-state.json";

// See api/state.js for why we construct the URL directly instead of listing for it.
function directUrl(token) {
  const storeId = (token.split("_")[3] || "").toLowerCase();
  return storeId ? `https://${storeId}.public.blob.vercel-storage.com/${PATH}` : null;
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

async function readState(token) {
  const u = directUrl(token);
  if (!u) return {};
  const r = await fetch(`${u}?t=${Date.now()}`, { cache: "no-store" });
  return r.ok ? await r.json() : {};
}

async function writeState(token, state) {
  await fetch(`${BLOB}/${PATH}`, {
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
}

module.exports = async (req, res) => {
  const token = process.env.BLOB_READ_WRITE_TOKEN;
  const secret = process.env.EDIT_SECRET;

  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "X-Edit-Secret, Content-Type");

  if (req.method === "OPTIONS") { res.status(200).end(); return; }

  if (!token || !secret) {
    res.status(503).json({ error: "backend not configured (set EDIT_SECRET + BLOB_READ_WRITE_TOKEN in Vercel)" });
    return;
  }

  const pass = req.headers["x-edit-secret"] || (req.query && req.query.secret);
  if (pass !== secret) { res.status(401).json({ error: "unauthorized" }); return; }

  if (req.method !== "POST") { res.status(405).json({ error: "method not allowed" }); return; }

  try {
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const url = (body.url || "").trim();
    if (!url) { res.status(400).json({ error: "url required" }); return; }

    const state = await readState(token);
    const links = Array.isArray(state.inspirationLinks) ? state.inspirationLinks : [];

    // deduplicate by URL
    if (!links.find((l) => l.url === url)) {
      const { platform, type, label } = parseLink(url);
      links.unshift({
        url,
        platform,
        type,
        label,
        note: (body.note || "").trim(),
        savedAt: new Date().toISOString(),
      });
    }

    state.inspirationLinks = links;
    await writeState(token, state);
    res.status(200).json({ ok: true, total: links.length });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 120) });
  }
};
