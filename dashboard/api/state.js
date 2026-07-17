// Single-user, passphrase-gated cross-device state for the Trend Digest dashboard.
// Stores one JSON blob on Vercel Blob: { saved:{url:post}, dismissed:[url], accountEdits:{igAdd,igRemove,ttAdd,ttRemove} }.
// GET  -> returns the state (so every device hydrates the same bookmarks/dismissals/account edits).
// POST -> replaces the state with the request body (client holds the merged state, last-write-wins).
// Auth: X-Edit-Secret header (or ?secret=) must equal process.env.EDIT_SECRET.
// Reuses BLOB_READ_WRITE_TOKEN (already set for video self-hosting). No new service.

const BLOB = "https://blob.vercel-storage.com";
const PATH = "state/dashboard-state.json";

// The blob's public URL is deterministic (store-id + pathname, no random suffix),
// so we construct it directly instead of using the `?prefix=` list lookup — that
// list index is eventually consistent and can lag behind recent writes, which was
// silently dropping state written moments earlier (e.g. via /api/save-link).
function directUrl(token) {
  const storeId = (token.split("_")[3] || "").toLowerCase();
  return storeId ? `https://${storeId}.public.blob.vercel-storage.com/${PATH}` : null;
}

// Returns { state, blocked }. `blocked` distinguishes "the store is rejecting
// reads" (Advanced Operations quota exhausted — a 403, seen 2026-07) from
// "nothing saved yet" (no blob there yet — the fetch itself still resolves
// r.ok=false, but that's a 404, not a 403). Previously both collapsed to the
// same silent {} — a real outage was indistinguishable from a fresh install,
// and every write built on top of it (persistState's staleness check) had no
// way to know the state it was comparing against might just be missing.
async function readState(token) {
  const u = directUrl(token);
  if (!u) return { state: {}, blocked: false };
  const r = await fetch(`${u}?t=${Date.now()}`, { cache: "no-store" });
  if (r.ok) return { state: await r.json(), blocked: false };
  return { state: {}, blocked: r.status === 403 };
}

// Profile URLs shared via /api/save-link are queued as their own atomic blobs at
// account-adds/<platform>_<handle>.json (see save-link.js for why they can't be
// written into this state blob directly). Merge them into the accountEdits we
// return so the Accounts panel shows them as pending immediately; the weekly
// apply_account_edits.py run consumes and deletes the blobs. Display-side merge
// only — never written back here. Caveat: un-queuing a SHARED add in the panel
// won't stick (it re-merges on next load) until the weekly run consumes it.
async function mergePendingAdds(token, state) {
  try {
    const r = await fetch(`${BLOB}?prefix=account-adds/`, { headers: { authorization: `Bearer ${token}` } });
    if (!r.ok) return state;
    const blobs = (await r.json()).blobs || [];
    if (!blobs.length) return state;
    const ed = (state.accountEdits = state.accountEdits || {});
    ed.igAdd = ed.igAdd || []; ed.ttAdd = ed.ttAdd || [];
    await Promise.all(blobs.map(async (b) => {
      try {
        const j = await (await fetch(`${b.url}?t=${Date.now()}`, { cache: "no-store" })).json();
        if (!j || !j.handle) return;
        const key = j.platform === "tt" ? "ttAdd" : "igAdd";
        const n = j.handle.toLowerCase();
        if (!ed[key].some((x) => x.toLowerCase() === n)) ed[key].push(n);
      } catch (e) { /* skip unreadable entries */ }
    }));
  } catch (e) { /* pending-adds merge is best-effort */ }
  return state;
}

// Previously unchecked — a failed PUT (e.g. the store's Advanced Operations
// quota exhausted, "store_suspended") was silently reported to the client as
// a successful save. The dashboard showed "● synced" while nothing was
// actually being written, which is exactly the bug this was reported as.
async function writeState(token, state) {
  const r = await fetch(`${BLOB}/${PATH}`, {
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
      const { state, blocked } = await readState(token);
      const merged = await mergePendingAdds(token, state);
      res.status(200).json({ ...merged, blocked });
      return;
    }
    if (req.method === "POST") {
      const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
      try {
        await writeState(token, body);
      } catch (e) {
        res.status(507).json({ ok: false, error: String(e.message || e).slice(0, 160) });
        return;
      }
      res.status(200).json({ ok: true });
      return;
    }
    res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 120) });
  }
};
