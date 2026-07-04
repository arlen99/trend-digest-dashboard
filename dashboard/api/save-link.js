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
//   POST   /api/save-link   { url, note? }        -> save/update one link
//   GET    /api/save-link                          -> list all links
//   DELETE /api/save-link   { url }                -> remove one link
// Auth: X-Edit-Secret header (or ?secret=), same as /api/state.

const crypto = require("crypto");

const BLOB = "https://blob.vercel-storage.com";
const PREFIX = "links/";

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

async function writeLink(token, url, note) {
  const { platform, type, label } = parseLink(url);
  const body = { url, platform, type, label, note: (note || "").trim(), savedAt: new Date().toISOString() };
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
    body: JSON.stringify(body),
  });
  return body;
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
      const saved = await writeLink(token, url, body.note);
      res.status(200).json({ ok: true, link: saved });
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
