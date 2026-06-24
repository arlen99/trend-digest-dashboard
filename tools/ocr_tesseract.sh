#!/usr/bin/env bash
# Portable OCR for cloud/Linux (GitHub Actions) — drop-in for tools/ocr.
# hook_text.py calls $OCR_CMD <image> and reads stdout. Set OCR_CMD=tools/ocr_tesseract.sh
tesseract "$1" stdout --psm 11 2>/dev/null
