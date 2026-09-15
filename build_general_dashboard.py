#!/usr/bin/env python3
"""
Render the general (niche-free) hook-template findings as a standalone HTML page.

Kept separate from build_dashboard.py on purpose: that one renders the travel/cinematic
board and owns dashboard/index.html. This writes a self-contained single file with its
data inlined, so it can be published as an Artifact, committed, or served from anywhere
without an API, a build step, or a network request at view time.

Regenerating is the whole point — point the weekly job at this after general_hooks.py
and the page reflects the newest run.

Usage: python3 build_general_dashboard.py [--out dashboard/general_hooks.html]
"""
import argparse
import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"


def latest(pattern):
    fs = sorted(glob.glob(str(OUT / pattern)), key=os.path.getmtime)
    return Path(fs[-1]) if fs else None


def corpus_stats():
    """Reconstruct the yield funnel from the OCR cache + corpus, so the page can show
    what was filtered rather than only what survived. A dashboard that reports 3 hooks
    without saying they came from 157 videos is hiding its own sample size."""
    import sys
    sys.path.insert(0, str(ROOT))
    from general_hooks import usable, BRANDED
    cache_f = OUT / "general_hook_texts.json"
    corpus_f = latest("general_corpus_*.json")
    if not (cache_f.exists() and corpus_f):
        return {}
    cache = json.loads(cache_f.read_text())
    # Scope to THIS run's corpus. The OCR cache deliberately persists across runs so
    # repeat videos are never re-read, which means it holds every video ever seen —
    # counting it whole reports a funnel bigger than the corpus (observed: 193 reads
    # against a 136-video run) and silently mixes last week's videos into this week's
    # yield. Intersect, so the funnel describes the run the page is actually showing.
    ids = {r.get("itemID") for r in json.loads(corpus_f.read_text()) if r.get("itemID")}
    cache = {k: v for k, v in cache.items() if k in ids}
    total = len(cache)
    no_text = sum(1 for v in cache.values() if not v.get("hook"))
    has = [v for v in cache.values() if v.get("hook")]
    branded = [v for v in has if BRANDED.search(v["hook"])]
    good = [v for v in has if not BRANDED.search(v["hook"]) and usable(v["hook"])]
    unreadable = len(has) - len(branded) - len(good)
    # Per-source yield, not just per-source volume. The sources differ enormously in how
    # much usable hook text they actually produce — the advertiser billboard carries a
    # working videoURL and lands ~30%, while the global Explore feed lands under 10% — and
    # a raw fetched count hides exactly that.
    good_ids = {k for k, v in cache.items()
                if v.get("hook") and not BRANDED.search(v["hook"]) and usable(v["hook"])}
    src = {}
    for k, v in cache.items():
        s = v.get("source") or "?"
        row = src.setdefault(s, {"fetched": 0, "usable": 0})
        row["fetched"] += 1
        if k in good_ids:
            row["usable"] += 1
    return {"total": total, "noText": no_text, "branded": len(branded),
            "unreadable": unreadable, "usable": len(good), "bySource": src}


def run_cost():
    """This run's measured spend, straight from cost_tracker — so the page reports what
    was actually billed rather than an estimate written into the copy."""
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        import cost_tracker
        s = cost_tracker.summarize()
        return {"calls": s["totals"].get("tikhubCalls", 0), "est": s.get("estCost", 0)}
    except Exception:  # noqa: BLE001 — cost display must never break the build
        return {}


CSS = """
*,*::before,*::after{box-sizing:border-box}
/* Complete light palette on bare :root — the un-stamped "system" state resolves here. */
:root{
  --ink:#101822; --ground:#F4F7F7; --panel:#FFFFFF; --panel-2:#EDF2F2;
  --line:#D6E0E0; --line-soft:#E5ECEC;
  --text:#101822; --text-2:#4A5A63; --text-3:#75868F;
  --signal:#0E8C85; --signal-soft:#D8EFED;
  --strong:#2E9A63; --partial:#B7822A; --idle:#8A9AA3;
  --shadow:0 1px 2px rgba(16,24,34,.06),0 8px 24px -12px rgba(16,24,34,.18);
  --radius:10px;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ink:#0E1520; --ground:#0E1520; --panel:#161F2C; --panel-2:#1D2837;
    --line:#2A3849; --line-soft:#212D3C;
    --text:#E8EFF2; --text-2:#9FB1BD; --text-3:#71838F;
    --signal:#3FCFC4; --signal-soft:#123334;
    --strong:#4FCB88; --partial:#E0A33E; --idle:#64757F;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 32px -14px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"]{
  --ink:#0E1520; --ground:#0E1520; --panel:#161F2C; --panel-2:#1D2837;
  --line:#2A3849; --line-soft:#212D3C;
  --text:#E8EFF2; --text-2:#9FB1BD; --text-3:#71838F;
  --signal:#3FCFC4; --signal-soft:#123334;
  --strong:#4FCB88; --partial:#E0A33E; --idle:#64757F;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 32px -14px rgba(0,0,0,.6);
}
body{
  margin:0; background:var(--ground); color:var(--text);
  font-family:"Archivo",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1000px;margin:0 auto;padding:40px 24px 72px;display:flex;flex-direction:column;gap:28px}

/* ---- header ---- */
.head{display:flex;flex-direction:column;gap:10px}
.eyebrow{
  font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--signal);
  font-weight:600;display:flex;align-items:center;gap:8px;
}
.eyebrow::before{content:"";width:22px;height:2px;background:var(--signal);border-radius:2px}
h1{margin:0;font-size:clamp(28px,4.2vw,40px);font-weight:700;letter-spacing:-.02em;text-wrap:balance}
.sub{margin:0;color:var(--text-2);max-width:62ch}
.stamp{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;color:var(--text-3)}

/* ---- funnel ---- */
.funnel{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:20px 22px;display:flex;flex-direction:column;gap:16px;
}
.funnel h2{margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-3);font-weight:600}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--panel-2)}
.bar span{display:block}
.legend{display:flex;flex-wrap:wrap;gap:14px 22px}
.lg{display:flex;align-items:baseline;gap:8px;font-size:13px}
.dot{width:9px;height:9px;border-radius:2px;flex:none;transform:translateY(-1px)}
.lg b{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;font-weight:600}
.lg span{color:var(--text-2)}
/* per-source yield */
.srcs{display:flex;flex-wrap:wrap;gap:10px;border-top:1px solid var(--line-soft);padding-top:14px}
.src{
  flex:1 1 150px;min-width:0;background:var(--panel-2);border:1px solid var(--line-soft);
  border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:5px;
}
.src .nm{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--text-3);font-weight:600}
.src .yld{
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:15px;font-weight:600;
}
.src .yld u{text-decoration:none;color:var(--text-3);font-weight:400;font-size:12px}
.src .rt{height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.src .rt i{display:block;height:100%;background:var(--signal)}
.src .pc{font-size:11px;color:var(--text-2)}

/* ---- rows ---- */
.rows{display:flex;flex-direction:column;gap:12px}
.sect{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-3);font-weight:600;margin:8px 0 0}
.row{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:18px 20px;display:grid;gap:14px;
  grid-template-columns:auto 1fr auto;align-items:start;
}
.row.confirmed{border-left:3px solid var(--strong)}
.row.partial{border-left:3px solid var(--partial)}
.row.idle{border-left:3px solid var(--line)}
.rank{
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;color:var(--text-3);
  font-variant-numeric:tabular-nums;padding-top:4px;
}
.mid{display:flex;flex-direction:column;gap:10px;min-width:0}
/* The hook set as it appears in its native medium: burned-in on-screen caption type. */
.hook{
  font-family:"Figtree",system-ui,sans-serif;font-weight:800;font-size:17px;
  line-height:1.3;letter-spacing:-.01em;text-wrap:balance;overflow-wrap:anywhere;
}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.chip{
  font-size:11px;letter-spacing:.04em;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line);color:var(--text-2);background:var(--panel-2);white-space:nowrap;
}
.chip.on{border-color:transparent;background:var(--strong);color:#06210F;font-weight:600}
.chip.mid{border-color:transparent;background:var(--partial);color:#251803;font-weight:600}
.ex{display:flex;flex-wrap:wrap;gap:6px}
.ex a{
  font-size:11px;font-family:"IBM Plex Mono",ui-monospace,monospace;
  color:var(--signal);text-decoration:none;border:1px solid var(--line);
  padding:3px 8px;border-radius:6px;background:var(--panel-2);
}
.ex a:hover,.ex a:focus-visible{border-color:var(--signal);background:var(--signal-soft)}
.right{display:flex;flex-direction:column;align-items:flex-end;gap:7px;min-width:132px}
/* Reuse strength readable as form before it is read as a number. */
.sig{display:flex;gap:3px}
.sig i{width:11px;height:16px;border-radius:2px;background:var(--panel-2);border:1px solid var(--line-soft)}
.sig i.f{background:var(--strong);border-color:transparent}
.sig i.p{background:var(--partial);border-color:transparent}
.num{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;font-size:12px;color:var(--text-2)}
.num b{color:var(--text);font-weight:600}

/* ---- method ---- */
.method{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px 22px;display:flex;flex-direction:column;gap:10px;
}
.method h2{margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-3);font-weight:600}
.method p{margin:0;color:var(--text-2);font-size:14px;max-width:70ch}
.method code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px;color:var(--signal)}
.empty{color:var(--text-2);font-size:14px}

/* ---- method dialog ---- */
.mbtn{
  font:inherit;font-size:12px;font-weight:600;letter-spacing:.04em;cursor:pointer;
  color:var(--signal);background:var(--signal-soft);border:1px solid var(--signal);
  padding:7px 13px;border-radius:999px;align-self:flex-start;
}
.mbtn:hover{filter:brightness(1.08)}
dialog#method{
  border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);
  color:var(--text);padding:0;max-width:640px;width:calc(100% - 32px);box-shadow:var(--shadow);
}
dialog#method::backdrop{background:rgba(6,12,18,.62)}
.mwrap{padding:26px 28px;display:flex;flex-direction:column;gap:18px}
.mhead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.mhead h2{margin:0;font-size:19px;font-weight:700;letter-spacing:-.01em}
.mx{
  font:inherit;cursor:pointer;background:none;border:1px solid var(--line);color:var(--text-2);
  width:28px;height:28px;border-radius:6px;line-height:1;flex:none;
}
.mx:hover{border-color:var(--signal);color:var(--signal)}
.step{display:grid;grid-template-columns:auto 1fr;gap:12px;align-items:start}
.sn{
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;font-weight:600;
  color:var(--signal);background:var(--signal-soft);border-radius:5px;
  padding:3px 7px;line-height:1.4;
}
.step p{margin:0 0 5px;font-size:13.5px;color:var(--text-2)}
.step p b{color:var(--text);font-weight:600}
.ep{
  display:block;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
  color:var(--signal);background:var(--panel-2);border:1px solid var(--line-soft);
  border-radius:5px;padding:5px 8px;overflow-x:auto;white-space:nowrap;
}
.mnote{
  font-size:12.5px;color:var(--text-3);border-top:1px solid var(--line-soft);
  padding-top:14px;margin:0;
}
a:focus-visible,.ex a:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
@media (max-width:640px){
  .row{grid-template-columns:auto 1fr}
  .right{grid-column:2;align-items:flex-start;min-width:0}
}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def sig_bar(matched, checked, cap=8):
    """Filled segments = results that genuinely reused the template, out of results
    actually OCR'd. Encodes strength as form, so a weak row reads weak at a glance."""
    checked = min(checked or 0, cap)
    out = []
    for i in range(cap):
        if i < matched:
            out.append('<i class="f"></i>' if matched >= 2 else '<i class="p"></i>')
        elif i < checked:
            out.append("<i></i>")
        else:
            out.append('<i style="opacity:.35"></i>')
    return "".join(out)


def render(rows, stats, stamp):
    conf = [r for r in rows if r.get("confirmed")]
    rest = [r for r in rows if not r.get("confirmed")]

    def row_html(r, i):
        m, checked = r.get("templateMatches", 0), r.get("results", 0)
        raw = r.get("rawMatches", m)
        others = r.get("distinct_creators", 0)
        cls = "confirmed" if r.get("confirmed") else ("partial" if m >= 1 else "idle")
        chip = ('<span class="chip on">reused by other creators</span>' if r.get("confirmed")
                else ('<span class="chip mid">one other creator</span>' if m >= 1
                      else '<span class="chip">no reuse found</span>'))
        # Say when matches were found but discarded as too old or too small, rather than
        # showing a quietly smaller number than the run actually saw.
        belowbar = (f'<span class="chip">{raw - m} below the bar</span>' if raw > m else "")
        ex = "".join(f'<a href="{esc(u)}" target="_blank" rel="noopener">example {k+1}</a>'
                     for k, u in enumerate(r.get("examples") or []))
        return f"""<article class="row {cls}">
  <div class="rank">{i:02d}</div>
  <div class="mid">
    <div class="hook">{esc(r.get('hook',''))}</div>
    <div class="meta">{chip}<span class="chip">{others} creators</span>
      <span class="chip">{r.get('maxLikes',0):,} peak likes</span>{belowbar}</div>
    <div class="ex">{ex}</div>
  </div>
  <div class="right">
    <div class="sig" role="img" aria-label="{m} of {checked} checked posts qualified">{sig_bar(m, checked)}</div>
    <div class="num"><b>{m}</b>/{checked} qualified</div>
  </div>
</article>"""

    seg = ""
    legend = ""
    if stats:
        t = max(stats["total"], 1)
        parts = [("usable", stats["usable"], "var(--signal)"),
                 ("branded / sponsored", stats["branded"], "var(--partial)"),
                 ("unreadable text", stats["unreadable"], "var(--idle)"),
                 ("no on-screen text", stats["noText"], "var(--line)")]
        seg = "".join(f'<span style="width:{100*n/t:.2f}%;background:{c}"></span>' for _, n, c in parts if n)
        legend = "".join(
            f'<div class="lg"><i class="dot" style="background:{c}"></i><b>{n}</b><span>{lbl}</span></div>'
            for lbl, n, c in parts)

    SRC_LABEL = {"top_contents": "Ranked board", "explore": "Explore feed",
                 "home_feed": "For You feed", "?": "Unknown"}
    srcs = ""
    for name, row in sorted((stats.get("bySource") or {}).items(),
                            key=lambda kv: -kv[1]["fetched"]):
        f, u = row["fetched"], row["usable"]
        pct = (100 * u / f) if f else 0
        srcs += (f'<div class="src"><div class="nm">{esc(SRC_LABEL.get(name, name))}</div>'
                 f'<div class="yld">{u}<u> / {f} videos</u></div>'
                 f'<div class="rt"><i style="width:{pct:.1f}%"></i></div>'
                 f'<div class="pc">{pct:.0f}% gave a usable hook</div></div>')
    cost = run_cost()
    cost_line = (f'<p class="stamp">{cost["calls"]:,} API calls this run · '
                 f'about ${cost["est"]:.2f}</p>') if cost.get("calls") else ""

    conf_html = "".join(row_html(r, i) for i, r in enumerate(conf, 1)) or \
        '<p class="empty">No template cleared the bar this run — it needs two creators outside the sample reusing the same structure.</p>'
    rest_html = "".join(row_html(r, i) for i, r in enumerate(rest, len(conf) + 1))

    return f"""<title>Hook Signal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=Figtree:wght@800&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <header class="head">
    <div class="eyebrow">TikTok · general trending</div>
    <h1>Which opening lines are creators actually reusing</h1>
    <p class="sub">On-screen hook text read straight off trending videos, grouped into reusable
      templates, then checked against TikTok search to see whether anyone else is really using them.
      Captions are ignored — only text burned into the video counts.</p>
    <p class="stamp">Run {esc(stamp)} · {len(conf)} confirmed of {len(rows)} candidates</p>
    <button class="mbtn" type="button" id="open-method">How this is measured</button>
  </header>

  <dialog id="method" aria-labelledby="method-title">
    <div class="mwrap">
      <div class="mhead">
        <h2 id="method-title">How this is measured</h2>
        <button class="mx" type="button" id="close-method" aria-label="Close">&times;</button>
      </div>

      <div class="step"><span class="sn">1</span><div>
        <p><b>Collect trending videos.</b> Two feeds, no keyword and no topic — so nothing
          is pre-selected for a niche. The first is TikTok's own ranked board of top
          content, sorted by how many people were still watching at six seconds. The second
          is the Explore tab, which is fresher but unranked.</p>
        <code class="ep">POST /api/v1/tiktok/ads/get_top_contents_list</code>
        <code class="ep">GET&nbsp; /api/v1/tiktok/web/fetch_explore_post</code>
      </div></div>

      <div class="step"><span class="sn">2</span><div>
        <p><b>Read the text off the video.</b> Each video carries a direct video link, so
          three still frames are grabbed from the opening seconds and the text in them is
          read on-device. Nothing is downloaded and stored. Captions are ignored entirely —
          only text burned into the picture counts, because that is the hook the viewer sees.</p>
      </div></div>

      <div class="step"><span class="sn">3</span><div>
        <p><b>Tell a hook from a subtitle.</b> Text that stays on screen across those frames
          is treated as a hook. Text that changes between them is a rolling subtitle and is
          set aside. Sponsored posts and non-English videos are dropped here too.</p>
      </div></div>

      <div class="step"><span class="sn">4</span><div>
        <p><b>Group hooks into templates.</b> Two passes. First a word-level one: prices and
          place names are swapped for blanks, so “$35 hotel in China” and “$12 hotel in
          Vietnam” read as one template. That only catches hooks differing by a number or a
          place, so a second pass compares hooks by <em>meaning</em> — enough to see that
          “My daughter thinks this is ice cream” and “My son thinks this is candy” are the
          same idea. Meaning alone never merges anything: it only nominates pairs, and the
          same judge from step 5 decides.</p>
      </div></div>

      <div class="step"><span class="sn">5</span><div>
        <p><b>Check whether anyone else really uses it.</b> Each template is searched on
          TikTok, and the text is read off the results the same way. A result only counts
          if its own on-screen text matches the structure — appearing in the search is never
          treated as proof, since a post can match the words while sharing only the subject.</p>
        <code class="ep">GET&nbsp; /api/v1/tiktok/app/v3/fetch_video_search_result</code>
      </div></div>

      <div class="step"><span class="sn">6</span><div>
        <p><b>Confirm.</b> A template is confirmed when at least two creators outside the
          original sample are found reusing it, on posts under six months old with at least
          1,000 likes. That bar exists because search surfaces a long tail of dead reposts,
          and three copies of one video with under a hundred likes each is not a trend.
          Reuse found weeks apart counts the same as reuse in one week — it has to clear
          the same bar either way.</p>
      </div></div>

      <p class="mnote">Everything above reads public data only. Nothing is posted, followed,
        or liked, and no logged-in account is used.</p>
    </div>
  </dialog>

  <section class="funnel">
    <h2>What the run saw</h2>
    <div class="bar">{seg}</div>
    <div class="legend">{legend}</div>
    <div class="srcs">{srcs}</div>
    {cost_line}
  </section>

  <h2 class="sect">Confirmed templates</h2>
  <div class="rows">{conf_html}</div>

  {'<h2 class="sect">Checked, not confirmed</h2>' if rest else ''}
  <div class="rows">{rest_html}</div>

  <section class="method">
    <h2>How a row gets confirmed</h2>
    <p>Every hook is read from three frames in the opening seconds; text that stays put
      across them is treated as a hook, text that changes is treated as a rolling caption.
      Hooks are grouped both by wording — with prices and place names blanked out, so
      <code>$35 hotel in China</code> and <code>$12 hotel in Vietnam</code> count as one
      template — and by meaning, which catches the ones that differ by anything else.</p>
    <p>A template is confirmed only when at least two creators <em>outside</em> the original
      sample are found reusing its structure. Search results are never counted on their own —
      each result's own on-screen text is read and compared, because a post can match a phrase
      while sharing nothing but the topic.</p>
  </section>
</div>
<script>
(function(){{
  var dlg=document.getElementById('method');
  document.getElementById('open-method').addEventListener('click',function(){{dlg.showModal();}});
  document.getElementById('close-method').addEventListener('click',function(){{dlg.close();}});
  // Click outside the panel closes it — the backdrop is part of the dialog element,
  // so a click landing on the dialog itself (not its content) is a backdrop click.
  dlg.addEventListener('click',function(e){{if(e.target===dlg)dlg.close();}});
}})();
</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dashboard" / "general_hooks.html"))
    args = ap.parse_args()

    src = latest("general_hook_trends_*.json")
    if not src:
        raise SystemExit("No output/general_hook_trends_*.json — run general_hooks.py first.")
    rows = json.loads(src.read_text())
    stamp = re.sub(r".*?(\d{4}-\d{2}-\d{2}).*", r"\1", src.name)
    html = render(rows, corpus_stats(), stamp)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    print(f"Wrote {dest} ({len(html)//1024}KB) — {len(rows)} rows from {src.name}")


if __name__ == "__main__":
    main()
