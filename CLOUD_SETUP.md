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
| **Trend Radar card prose** (name, format description, trigger, "Ride it" copy) | ✅ `curate_trends.py` (same Claude vision pattern) |

Both run fully automatically, no review gate — their prompts are the only filter
against monetization bait, sponsored/branded posts, off-niche content, and (for
trend candidates specifically) evidence that's really just OCR/clustering noise
rather than a genuine repeatable template.

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

**Both curation steps are now automated**, same underlying approach (Option B from
the original plan here): `curate_posts.py` and `curate_trends.py` each call the
Anthropic Messages API directly (raw `urllib`, no SDK dependency — consistent with
the rest of the pipeline) with vision + tool-use. `curate_posts.py` decides
include/exclude and tags each kept IG post; `curate_trends.py` turns trend evidence
packs into named cards with a visual/structural description, emotional trigger,
and "Ride it" recipe. Both run fully automatically inside `weekly-digest.yml`, no
review gate — nothing left that requires a manual Claude session for the weekly
refresh itself.

> First run note: the workflow is untested until it runs in Actions (no repo to test against
> locally). Treat the first `workflow_dispatch` as a smoke test.
