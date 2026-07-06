# Running the weekly digest in the cloud (GitHub Actions)

Goal: the Monday run happens on GitHub's always-on infra, **not your Mac**.

This works because the whole pipeline is just **TikHub HTTP calls + file writes** — no
logged-in browser or residential IP to replicate. OCR swaps from macOS Vision (Mac-only)
to **Tesseract** (`tools/ocr_tesseract.sh`, installed by the workflow).

## What's automated vs. not

| Layer | In Actions? |
|---|---|
| Scrape (IG + TikTok + keyword lane), sound chart, TikTok trends, AudD, hooks, trend **candidates**, provenance, build, deploy | ✅ `weekly-digest.yml` |
| **IG Swipe File curation** (pick keepers, visual-style tags, hook headline) | ✅ `curate_posts.py` (Claude vision via `ANTHROPIC_API_KEY`) |
| **Trend Radar card prose** + the written digest | ❌ still needs a manual Claude session — see "The Claude layer" |

`curate_posts.py` runs fully automatically, no review gate — its prompt is the only
filter against monetization bait, sponsored/branded posts, and off-niche content.
Per-post audio identification (AudD fingerprinting) also runs automatically now:
`fetch_user_posts` stopped returning `clips_metadata.music_info`/`original_sound_info`
at the API level sometime before 2026-07 (confirmed even against a post independently
verified to use a licensed track), so every curated Reel gets fingerprinted rather than
relying on that field.

## One-time setup (only you can do these)

1. **Put the project in a GitHub repo.** `gh` isn't installed locally, so either install it
   (`brew install gh && gh auth login`) or create a **private** repo `trend-digest` in the
   browser and push:
   ```bash
   cd "/Users/arlen/Trend Digest"
   git init && git add -A && git commit -m "pipeline"
   git remote add origin git@github.com:arlen99/trend-digest.git
   git push -u origin main
   ```
   (`.gitignore` already excludes `.env`, `output/`, and the compiled `tools/ocr`.)

2. **Add Actions secrets** (repo → Settings → Secrets and variables → Actions):
   `TIKHUB_TOKEN`, `AUDD_TOKEN`, `BLOB_READ_WRITE_TOKEN`, and `ANTHROPIC_API_KEY`
   (a dedicated Anthropic Console API key, separate from your Claude Code session —
   `curate_posts.py` needs this to run at all; the workflow will fail without it).
   Secrets are entered in GitHub's UI — they can't be scripted.

3. **Dashboard push from CI.** The deploy step pushes the `dashboard/` repo. Give the
   workflow a deploy key or a PAT secret with push access to `trend-digest-dashboard`, or
   restructure to a single repo with Vercel pointed at the `dashboard/` subdir.

4. **Adjust the cron** in `weekly-digest.yml` (it's UTC) and test via the **Run workflow**
   button (workflow_dispatch) before trusting the schedule.

## The Claude layer (the real decision)

**Curation is now Option B, done**: `curate_posts.py` calls the Anthropic Messages API
directly (raw `urllib`, no SDK dependency — consistent with the rest of the pipeline)
with vision + tool-use, deciding include/exclude and tagging each kept post. Runs fully
automatically inside `weekly-digest.yml`, no review gate.

**Trend Radar card prose** is the one piece that still needs a manual Claude session. Options:
- **A. `/schedule` cloud routine** — keep Claude native and always-on for just this piece.
  *(Recommended if you want it automated too.)*
- **B. Extend curate_posts.py-style automation** — same pattern as curation, a second
  scripted Claude call inside the workflow.
- **C. Leave it manual** — Actions refreshes data + curates the swipe file weekly; you run a
  Claude session when you want fresh trend-card prose.

> First run note: the workflow is untested until it runs in Actions (no repo to test against
> locally). Treat the first `workflow_dispatch` as a smoke test.
