# Travel & Cinematic Photography — Trend Digest

Weekly pipeline that finds the best-performing Reels/TikToks across ~100 reference
creators in the travel/cinematic photography niche, ranks them, and feeds a Notion
swipe file + weekly digest.

**Notion home:** [Travel & Cinematic Photography — Trend Digest](https://app.notion.com/p/3861a5177a0e8147b12cf24410bc54e0)
- *Swipe File — Top Posts* — every pulled post, tagged by hook/trigger/format/audio/style + real metrics
- *Weekly Digests* — one synthesized page per week (patterns, new hook variations, content direction)

## The pieces
| File | What it does |
|--|--|
| `accounts.json` | The reference handle list. Edit freely. |
| `scrape.py` | Pulls recent posts via Apify, ranks by **outlier score** (engagement vs. each account's own median) + raw reach. Writes `output/top_posts_<date>.json` + `.md`. |
| `notion_push.py` | *(optional)* Pushes the JSON straight into the Notion Swipe File. |

## Why "outlier score" and not just raw likes
A creator with 2M followers will always out-like one with 50k. Raw counts just
re-rank by follower count. The **outlier score** = a post's engagement ÷ that
account's *own median* — so a 50k creator whose reel did 8× their normal numbers
surfaces above a megastar's average day. That ratio is what actually flags a
trend worth copying.

## Weekly run
```bash
# 1. Get an Apify token (free tier is fine for small runs): https://console.apify.com/account/integrations
export APIFY_TOKEN=apify_api_xxxxx

# 2. Pull + rank
python3 scrape.py            # honors DAYS_BACK, POSTS_PER_ACCOUNT, TOP_N env vars

# 3. Review output/top_posts_<date>.md, then either:
#    a) hand the JSON to Claude -> it imports to Notion + writes the digest, or
#    b) export NOTION_TOKEN=secret_xxx && python3 notion_push.py output/top_posts_<date>.json
```

## Verification
Every metric comes from Instagram's own counts as returned by Apify. Before
publishing a digest, open 2–3 of the top rows' URLs live and confirm the
view/like numbers match (within a few %, since counts keep climbing). If Apify
returns `-1` likes the account hid its like count — those rows rank on comments
+ views only.

## Cadence
Run weekly (e.g. Monday AM). Ask Claude to "build this week's digest" and it will
pull, rank, import, find the repeating patterns, and write fresh hook variations.
