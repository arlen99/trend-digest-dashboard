// Passphrase-gated handle verifier — confirms an Instagram or TikTok username actually
// exists before the dashboard queues it for the watchlist. Uses the same EDIT_SECRET as
// /api/state, so only the authorised user can spend TikHub credits.
//
//   GET /api/verify?plat=ig|tiktok&handle=<name>   header X-Edit-Secret
//   → { ok: true, handle, displayName?, followers? }   ← exists
//   → { ok: false, reason: 'not_found' | ... }          ← doesn't
//
// Reuses TIKHUB_TOKEN (already set for the weekly cloud run).

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15";
const TH = "https://api.tikhub.io";

async function tikhub(path, token) {
  const r = await fetch(TH + path, {
    headers: { authorization: `Bearer ${token}`, "user-agent": UA, accept: "application/json" },
  });
  const body = await r.text();
  let j = null; try { j = JSON.parse(body); } catch (e) {}
  return { status: r.status, json: j };
}

function deepFind(o, key) {
  if (!o || typeof o !== "object") return undefined;
  if (key in o) return o[key];
  for (const v of Object.values(o)) { const r = deepFind(v, key); if (r !== undefined) return r; }
  return undefined;
}

async function verifyIG(handle, token) {
  const { status, json } = await tikhub(`/api/v1/instagram/v1/fetch_user_info_by_username?username=${encodeURIComponent(handle)}`, token);
  if (status !== 200 || !json) return { ok: false, reason: "lookup_failed" };
  const data = json.data || json;
  // bogus handles come back with status:"fail" + errorMessage; valid ones nest user under data.data
  if (data && (data.status === "fail" || data.errorMessage)) return { ok: false, reason: "not_found" };
  const pk = deepFind(data, "pk") || deepFind(data, "id");
  if (!pk) return { ok: false, reason: "not_found" };
  return { ok: true, handle, displayName: deepFind(data, "full_name") || "", followers: deepFind(data, "follower_count") || null };
}

async function verifyTT(handle, token) {
  const { status, json } = await tikhub(`/api/v1/tiktok/app/v3/handler_user_profile?unique_id=${encodeURIComponent(handle)}`, token);
  if (status === 400) return { ok: false, reason: "not_found" };
  if (status !== 200 || !json) return { ok: false, reason: "lookup_failed" };
  // success-shaped response carries the user; absence = not found
  const data = json.data || json;
  const found = deepFind(data, "unique_id") || deepFind(data, "uniqueId") || deepFind(data, "sec_uid") || deepFind(data, "secUid");
  if (!found) return { ok: false, reason: "not_found" };
  return { ok: true, handle, displayName: deepFind(data, "nickname") || "", followers: deepFind(data, "follower_count") || deepFind(data, "followerCount") || null };
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  const token = process.env.TIKHUB_TOKEN, secret = process.env.EDIT_SECRET;
  if (!token || !secret) { res.status(503).json({ ok: false, reason: "backend not configured (set TIKHUB_TOKEN + EDIT_SECRET in Vercel)" }); return; }
  const pass = req.headers["x-edit-secret"] || (req.query && req.query.secret);
  if (pass !== secret) { res.status(401).json({ ok: false, reason: "unauthorized" }); return; }
  const plat = (req.query && req.query.plat) || "";
  let handle = (req.query && req.query.handle) || "";
  handle = String(handle).trim().replace(/^@/, "");
  if (!handle || !/^[A-Za-z0-9._-]{1,30}$/.test(handle)) { res.status(400).json({ ok: false, reason: "bad_handle" }); return; }
  try {
    const r = plat === "tiktok" ? await verifyTT(handle, token) : await verifyIG(handle, token);
    res.status(200).json(r);
  } catch (e) {
    res.status(500).json({ ok: false, reason: String(e).slice(0, 100) });
  }
};
