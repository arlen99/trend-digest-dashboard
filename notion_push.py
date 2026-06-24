#!/usr/bin/env python3
"""
Push a ranked top_posts_<date>.json into the Notion "Swipe File — Top Posts"
database.

This path is OPTIONAL / for full hands-off automation. Day to day you can
instead just hand the JSON to Claude, which imports it via the Notion MCP and
writes the weekly digest in the same pass.

Setup for this script:
  1. Create an internal Notion integration: https://www.notion.so/my-integrations
  2. Share the "Travel & Cinematic Photography — Trend Digest" page with it.
  3. export NOTION_TOKEN=secret_xxx

Usage:
  python3 notion_push.py output/top_posts_2026-06-21.json
"""
import json
import os
import sys
import urllib.request
from datetime import datetime

DATA_SOURCE_ID = "315bd81d-51ab-4c10-918e-c0ad958ef156"  # Swipe File — Top Posts
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_VERSION = "2022-06-28"


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def page_for(row: dict, week: str) -> dict:
    title = (row.get("caption") or row["url"])[:90] or row["url"]
    props = {
        "Hook / Caption": {"title": [{"text": {"content": title}}]},
        "Account": {"rich_text": [{"text": {"content": "@" + row["account"]}}]},
        "URL": {"url": row["url"] or None},
        "Views": {"number": row.get("views") or 0},
        "Likes": {"number": row.get("likes") or 0},
        "Comments": {"number": row.get("comments") or 0},
        "Week": {"date": {"start": week}},
        "Notes": {"rich_text": [{"text": {"content": f"Outlier {row.get('outlier_score','?')}x. Audio: {row.get('music') or 'n/a'}"}}]},
    }
    if row.get("format") in ("Reel", "Carousel", "Photo", "Story"):
        props["Format"] = {"select": {"name": row["format"]}}
    return {"parent": {"database_id": DATA_SOURCE_ID}, "properties": props}


def main() -> None:
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN not set — see header. (Or just let Claude import via MCP.)")
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 notion_push.py output/top_posts_<date>.json")
    rows = json.loads(open(sys.argv[1]).read())
    week = datetime.now().strftime("%Y-%m-%d")
    for i, row in enumerate(rows, 1):
        post("https://api.notion.com/v1/pages", page_for(row, week))
        print(f"  [{i}/{len(rows)}] @{row['account']}")
    print(f"Pushed {len(rows)} rows into the Swipe File.")


if __name__ == "__main__":
    main()
