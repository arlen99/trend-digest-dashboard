// Live TikHub re-fetch of a fresh, currently-valid signed video URL for one post.
//
// Board cards store the `video` URL captured at scrape/curation time — but IG/TikTok
// sign every CDN media URL with a built-in expiry (confirmed live: ~24-36h after
// capture), so a card that played natively early in the week silently breaks a day
// or two later once that signature dies. fetch_videos.py exists to replace this with
// a permanent Blob copy, but while Blob's plan is suspended nothing gets hosted, so
// cards are left running on borrowed time on their original signed URL. This endpoint
// is the stopgap: when a <video>'s `error` event fires client-side, it calls here for
// a fresh URL and retries before giving up and falling back to the platform embed.
//
// Unauthenticated on purpose — unlike verify.js/state.js (which can add accounts or
// spend a week's scrape budget), this only re-reads data that's already public on the
// board, so it needs to work for a casual visit with no passphrase entered. Cheap
// (~$0.001/call, TikHub's own rate) and cached a few minutes so repeat page loads for
// the same post don't multiply calls.
//
//   GET /api/refresh-video?url=<post_url>&platform=ig|tiktok
//   → { ok: true, video: <fresh_url> }
//   → { ok: false, reason: ... }

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16 Safari/605.1.15";
const TH = "https://api.tikhub.io";

function ttId(u) {
  const m = /\/video\/(\d+)/.exec(u || "");
  return m ? m[1] : "";
}

// Same deep-search pattern as fetch_videos.py's deep_play_url() — TikTok's
// fetch_one_video response nests play_addr/download_addr at varying depths.
function deepPlayUrl(o) {
  if (Array.isArray(o)) {
    for (const v of o) { const r = deepPlayUrl(v); if (r) return r; }
    return "";
  }
  if (o && typeof o === "object") {
    const pa = o.play_addr || o.download_addr;
    if (pa && Array.isArray(pa.url_list) && pa.url_list.length) return pa.url_list[0];
    for (const v of Object.values(o)) { const r = deepPlayUrl(v); if (r) return r; }
  }
  return "";
}

async function tikhub(path, token) {
  const r = await fetch(TH + path, { headers: { authorization: `Bearer ${token}`, "user-agent": UA, accept: "application/json" } });
  let json = null; try { json = await r.json(); } catch (e) {}
  return { status: r.status, json };
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "public, max-age=300, stale-while-revalidate=120");
  const token = process.env.TIKHUB_TOKEN;
  if (!token) { res.status(503).json({ ok: false, reason: "backend not configured (set TIKHUB_TOKEN in Vercel)" }); return; }
  const url = (req.query && req.query.url) || "";
  const platform = (req.query && req.query.platform) || "instagram";
  if (!url || !/^https:\/\/(www\.)?(instagram\.com|tiktok\.com)\//.test(url)) {
    res.status(400).json({ ok: false, reason: "bad_url" }); return;
  }
  try {
    let fresh = "";
    if (platform === "tiktok") {
      const id = ttId(url);
      if (!id) { res.status(400).json({ ok: false, reason: "bad_url" }); return; }
      const { status, json } = await tikhub(`/api/v1/tiktok/app/v3/fetch_one_video?aweme_id=${id}`, token);
      if (status !== 200 || !json) { res.status(502).json({ ok: false, reason: `upstream_${status}` }); return; }
      fresh = deepPlayUrl(json);
    } else {
      const { status, json } = await tikhub(`/api/v1/instagram/v1/fetch_post_by_url?post_url=${encodeURIComponent(url)}`, token);
      if (status !== 200 || !json) { res.status(502).json({ ok: false, reason: `upstream_${status}` }); return; }
      fresh = (json.data || json || {}).video_url || "";
    }
    if (!fresh) { res.status(404).json({ ok: false, reason: "no_video" }); return; }
    res.status(200).json({ ok: true, video: fresh });
  } catch (e) {
    res.status(500).json({ ok: false, reason: String(e).slice(0, 100) });
  }
};
