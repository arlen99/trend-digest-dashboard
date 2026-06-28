// Single-user, passphrase-gated cross-device state for the Trend Digest dashboard.
// Stores one JSON blob on Vercel Blob: { saved:{url:post}, dismissed:[url], accountEdits:{igAdd,igRemove,ttAdd,ttRemove} }.
// GET  -> returns the state (so every device hydrates the same bookmarks/dismissals/account edits).
// POST -> replaces the state with the request body (client holds the merged state, last-write-wins).
// Auth: X-Edit-Secret header (or ?secret=) must equal process.env.EDIT_SECRET.
// Reuses BLOB_READ_WRITE_TOKEN (already set for video self-hosting). No new service.

const BLOB = "https://blob.vercel-storage.com";
const PATH = "state/dashboard-state.json";

async function stateUrl(token) {
  const r = await fetch(`${BLOB}?prefix=${encodeURIComponent(PATH)}`, { headers: { authorization: `Bearer ${token}` } });
  if (!r.ok) return null;
  const j = await r.json();
  const b = (j.blobs || []).find((x) => x.pathname === PATH);
  return b ? b.url : null;
}

async function readState(token) {
  const u = await stateUrl(token);
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
  if (!token || !secret) {
    res.status(503).json({ error: "backend not configured (set EDIT_SECRET + BLOB_READ_WRITE_TOKEN in Vercel)" });
    return;
  }
  const pass = req.headers["x-edit-secret"] || (req.query && req.query.secret);
  if (pass !== secret) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  try {
    if (req.method === "GET") {
      res.status(200).json(await readState(token));
      return;
    }
    if (req.method === "POST") {
      const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
      await writeState(token, body);
      res.status(200).json({ ok: true });
      return;
    }
    res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 120) });
  }
};
