#!/usr/bin/env python3
"""
Build the deployable dashboard: inline dashboard/data.json into the template so
the result is a single self-contained static file (works on any host, and as a
local file:// with no CORS/fetch issues).

Pure transform — no network, no secrets. Reads:
  dashboard/_template.html   (contains the literal token __DATA__)
  dashboard/data.json
Writes:
  dashboard/index.html

Run after exporting the Swipe File to dashboard/data.json:
  python3 build_dashboard.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"


def copy_thumbs(data: dict) -> int:
    """Ensure every thumbnail referenced in data.json exists under dashboard/.
    Copies from output/thumbs/ when missing. Returns count copied."""
    n = 0
    for p in data["posts"]:
        rels = [r for r in [p.get("thumb")] + (p.get("carousel") or []) if r]
        for rel in rels:
            dst = DASH / rel
            if not dst.exists():
                src = ROOT / "output" / rel  # output/thumbs/... or output/carousels/...
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
                    n += 1
    return n


def main() -> None:
    data = json.loads((DASH / "data.json").read_text())
    copied = copy_thumbs(data)
    if copied:
        print(f"Copied {copied} new thumbnail(s) into dashboard/thumbs.")
    template = (DASH / "_template.html").read_text()
    if "__DATA__" not in template:
        raise SystemExit("Template missing __DATA__ token.")
    # Compact JSON; </script> can't legally appear in our data but guard anyway.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("__DATA__", blob)
    (DASH / "index.html").write_text(html)
    print(f"Built dashboard/index.html — {len(data['posts'])} posts, "
          f"{len(html)//1024} KB.")


if __name__ == "__main__":
    main()
