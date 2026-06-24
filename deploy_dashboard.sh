#!/usr/bin/env bash
# Publish the dashboard via GitHub → Vercel auto-deploy.
# Single repo (trend-digest-dashboard): pipeline at root, site under dashboard/.
# Vercel's Root Directory is set to "dashboard"; pushing main triggers a deploy.
set -euo pipefail
cd "$(dirname "$0")"            # project root = git repo root
python3 build_dashboard.py     # regenerate dashboard/index.html from data.json
git add -A
if git diff --cached --quiet; then
  echo "No changes to deploy."; exit 0
fi
git -c user.email="arlentan@gmail.com" -c user.name="Arlen Tan" \
  commit -q -m "Refresh $(date +%F)"
git push -q origin main
echo "Pushed to GitHub — Vercel auto-deploys from main (root dir: dashboard/)."
