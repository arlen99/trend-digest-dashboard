#!/usr/bin/env bash
# Publish the dashboard via GitHub → Vercel auto-deploy.
# Single repo (trend-digest-dashboard): pipeline at root, site under dashboard/.
# Vercel's Root Directory is set to "dashboard"; pushing main triggers a deploy.
#
# IMPORTANT: the GitHub Actions bot OWNS dashboard/data.json + output/ + dashboard/thumbs/
# + dashboard/carousels/ (it refreshes them weekly). When we deploy a CODE change
# (template, build script, py modules), we must NOT clobber the bot's data files.
# Workflow: stash local edits → pull bot's latest → re-apply our edits → rebuild → push.
set -euo pipefail
cd "$(dirname "$0")"

# 1) Save any uncommitted local edits + sync with bot's latest from origin
git fetch -q origin
STASH=""
if [ -n "$(git status --porcelain)" ]; then
  git stash push -q -u -m "deploy_dashboard auto-stash $(date +%s)" && STASH="yes"
fi
git pull --ff-only -q origin main || {
  echo "ERROR: branches diverged; resolve manually before deploying."; exit 1; }
if [ -n "$STASH" ]; then
  git stash pop -q || { echo "ERROR: stash pop conflict; check 'git status'."; exit 1; }
fi

# 2) Rebuild index.html FROM THE FRESHLY-PULLED data.json (so bot's refresh is honored)
python3 build_dashboard.py

# 3) Commit + push
git add -A
if git diff --cached --quiet; then
  echo "No changes to deploy."; exit 0
fi
git -c user.email="arlentan@gmail.com" -c user.name="Arlen Tan" \
  commit -q -m "Refresh $(date +%F)"
git push -q origin main
echo "Pushed to GitHub — Vercel auto-deploys from main (root dir: dashboard/)."
