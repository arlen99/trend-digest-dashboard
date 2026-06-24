# Running the weekly digest in the cloud (GitHub Actions)

Goal: the Monday run happens on GitHub's always-on infra, **not your Mac**.

This works because the whole pipeline is just **TikHub HTTP calls + file writes** — no
logged-in browser or residential IP to replicate. OCR swaps from macOS Vision (Mac-only)
to **Tesseract** (`tools/ocr_tesseract.sh`, installed by the workflow).

## What's automated vs. not

| Layer | In Actions? |
|---|---|
| Scrape (IG + TikTok + keyword lane), sound chart, TikTok trends, AudD, hooks, trend **candidates**, provenance, build, deploy | ✅ `weekly-digest.yml` |
| **Curation** (pick ~20 keepers, visual-style tags) + **Trend Radar card prose** + the written digest | ❌ needs Claude — see "The Claude layer" |

A deterministic-only run keeps the **data** fresh (metrics, sound chart, trend candidates)
but leaves the **curated swipe file + trend-card writing** from the last Claude run.

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
   `TIKHUB_TOKEN`, `AUDD_TOKEN` (and `ANTHROPIC_API_KEY` if you do the Claude layer in CI).
   Secrets are entered in GitHub's UI — they can't be scripted.

3. **Dashboard push from CI.** The deploy step pushes the `dashboard/` repo. Give the
   workflow a deploy key or a PAT secret with push access to `trend-digest-dashboard`, or
   restructure to a single repo with Vercel pointed at the `dashboard/` subdir.

4. **Adjust the cron** in `weekly-digest.yml` (it's UTC) and test via the **Run workflow**
   button (workflow_dispatch) before trusting the schedule.

## The Claude layer (the real decision)

The digest's judgment — curation, visual-style vision, trend-card prose — needs Claude. Options:
- **A. `/schedule` cloud routine** — keep Claude native and always-on; let Actions do only the
  data refresh + deploy. Simplest; Claude stays exactly as it is today. *(Recommended.)*
- **B. Agent SDK step in CI** — a script calls the Claude API to curate + write cards inside the
  workflow. Fully self-contained, but a real build + per-run API cost.
- **C. Leave curation manual** — Actions refreshes data weekly; you run a Claude session when you
  want fresh curation/cards.

> First run note: the workflow is untested until it runs in Actions (no repo to test against
> locally). Treat the first `workflow_dispatch` as a smoke test.
