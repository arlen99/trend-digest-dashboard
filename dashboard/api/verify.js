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
  // TikHub occasionally returns 5xx / empty bodies; retry briefly before giving up.
  let lastStatus = 0, lastBody = "";
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 12000);
      const r = await fetch(TH + path, {
        headers: { authorization: `Bearer ${token}`, "user-agent": UA, accept: "application/json" },
        signal: ctrl.signal,
      });
      clearTimeout(t);
      const body = await r.text();
      lastStatus = r.status; lastBody = body;
      let j = null; try { j = JSON.parse(body); } catch (e) {}
      // success-looking: 200 + parseable JSON; otherwise retry
      if (r.status === 200 && j) return { status: 200, json: j };
      if (r.status === 400) return { status: 400, json: j };  // 400 = not found, don't retry
    } catch (e) {
      lastBody = String(e).slice(0, 80);
    }
    await new Promise(res => setTimeout(res, 400 * (attempt + 1)));
  }
  return { status: lastStatus || 0, json: null, _hint: lastBody.slice(0, 80) };
}

function deepFind(o, key) {
  if (!o || typeof o !== "object") return undefined;
  if (key in o) return o[key];
  for (const v of Object.values(o)) { const r = deepFind(v, key); if (r !== undefined) return r; }
  return undefined;
}

async function verifyIG(handle, token) {
  const { status, json, _hint } = await tikhub(`/api/v1/instagram/v1/fetch_user_info_by_username?username=${encodeURIComponent(handle)}`, token);
  if (status !== 200) return { ok: false, reason: `upstream_${status || "timeout"}`, hint: _hint };
  if (!json) return { ok: false, reason: "bad_response" };
  // Validity is decided by ONE signal: does the payload carry a numeric IG user id anywhere?
  const pk = deepFind(json, "pk") || deepFind(json, "id");
  if (!pk || !/^\d+$/.test(String(pk))) return { ok: false, reason: "not_found" };
  return { ok: true, handle, displayName: deepFind(json, "full_name") || "", followers: deepFind(json, "follower_count") || null,
           bio: deepFind(json, "biography") || "", avatar: deepFind(json, "profile_pic_url_hd") || deepFind(json, "profile_pic_url") || "",
           verified: !!deepFind(json, "is_verified"), pk: String(pk) };
}

async function verifyTT(handle, token) {
  const { status, json, _hint } = await tikhub(`/api/v1/tiktok/app/v3/handler_user_profile?unique_id=${encodeURIComponent(handle)}`, token);
  if (status === 400) return { ok: false, reason: "not_found" };
  if (status !== 200) return { ok: false, reason: `upstream_${status || "timeout"}`, hint: _hint };
  if (!json) return { ok: false, reason: "bad_response" };
  const found = deepFind(json, "unique_id") || deepFind(json, "uniqueId") || deepFind(json, "sec_uid") || deepFind(json, "secUid");
  if (!found) return { ok: false, reason: "not_found" };
  const av = deepFind(json, "avatar_larger") || deepFind(json, "avatar_thumb") || deepFind(json, "avatarLarger") || {};
  return { ok: true, handle, displayName: deepFind(json, "nickname") || "",
           followers: deepFind(json, "follower_count") || deepFind(json, "followerCount") || null,
           bio: deepFind(json, "signature") || "", verified: !!deepFind(json, "verification_type"),
           avatar: (av && av.url_list && av.url_list[0]) || deepFind(json, "avatar") || "",
           secUid: deepFind(json, "sec_uid") || deepFind(json, "secUid") || "" };
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
