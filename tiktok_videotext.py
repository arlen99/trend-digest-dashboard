#!/usr/bin/env python3
"""OCR the on-screen hook text from each TikTok post's cover thumbnail → videoText
in dashboard/data.json (so TikTok cards carry the same on-screen-hook field as Reels).
Uses tools/ocr (macOS Vision) / set OCR_CMD for Tesseract. Run after the TikTok merges."""
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
DASH = ROOT / "dashboard"
OCR = os.environ.get("OCR_CMD", str(ROOT / "tools" / "ocr"))


def ocr(path):
    try:
        out = subprocess.run([OCR, path], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for ln in out.split("\n"):
        s = ln.strip().strip('"').strip()
        if len(s) < 3 or s.startswith("@") or re.fullmatch(r"[\d.,KMviews\s]+", s, re.I):
            continue
        lines.append(s)
    return " ".join(lines)[:120]


def main():
    data = json.loads((DASH / "data.json").read_text())
    tt = [p for p in data["posts"] if p.get("platform") == "tiktok"]
    got = 0
    for p in tt:
        th = DASH / (p.get("thumb") or "")
        if p.get("thumb") and th.exists():
            t = ocr(str(th))
            p["videoText"] = t
            if t:
                got += 1
            print(f"  @{p['account'][:18]:<18} {p.get('lane','acct'):<8} -> {t[:58]!r}")
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nvideoText set on {got}/{len(tt)} TikTok posts")


if __name__ == "__main__":
    main()
