#!/usr/bin/env python3
"""
Lightweight, shared cost accounting for the weekly pipeline — each script that
makes paid API calls records its own tally here at the end of main(); provenance.py
reads them all back and sums a rough weekly total for the dashboard's Sources.

Rates are the ones already established elsewhere in this codebase (TikHub's
$0.001/call is used by keyword_posts.py's own cost line) or best-effort public
list pricing for Claude — flagged as approximate in the dashboard copy itself,
since real spend depends on your actual Anthropic/TikHub account terms. This is
for a rough weekly READ, not a billing-accurate figure — check console.anthropic.com
and your TikHub dashboard for exact numbers.

Usage (at the end of a script's main()):
  import cost_tracker
  cost_tracker.record("scrape", tikhub_calls=calls)
  cost_tracker.record("curate_posts", claude_calls=n, claude_input_tokens=in_tok, claude_output_tokens=out_tok)

Each call OVERWRITES that script's own entry (keyed by name) — safe to re-run
a script without double-counting a prior run's tally.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
COSTS_FILE = ROOT / "output" / "pipeline_costs.json"

TIKHUB_RATE = 0.001  # $/call — matches keyword_posts.py's existing estimate
# Best-effort public Sonnet-class list pricing (approximate — not fetched live).
CLAUDE_INPUT_RATE = 3.00 / 1_000_000   # $/input token
CLAUDE_OUTPUT_RATE = 15.00 / 1_000_000  # $/output token


def record(script, tikhub_calls=0, claude_calls=0, claude_input_tokens=0, claude_output_tokens=0,
           audd_calls=0, audd_auth_dead=False):
    data = {}
    if COSTS_FILE.exists():
        try:
            data = json.loads(COSTS_FILE.read_text())
        except Exception:  # noqa: BLE001
            data = {}
    data[script] = {
        "tikhubCalls": tikhub_calls,
        "claudeCalls": claude_calls,
        "claudeInputTokens": claude_input_tokens,
        "claudeOutputTokens": claude_output_tokens,
        "auddCalls": audd_calls,
        "auddAuthDead": audd_auth_dead,
    }
    COSTS_FILE.parent.mkdir(exist_ok=True)
    COSTS_FILE.write_text(json.dumps(data, indent=2))


def summarize():
    """Read back every script's tally -> {perScript, totals, estCost}. Used by provenance.py."""
    data = json.loads(COSTS_FILE.read_text()) if COSTS_FILE.exists() else {}
    tikhub = sum(v.get("tikhubCalls", 0) for v in data.values())
    claude_calls = sum(v.get("claudeCalls", 0) for v in data.values())
    claude_in = sum(v.get("claudeInputTokens", 0) for v in data.values())
    claude_out = sum(v.get("claudeOutputTokens", 0) for v in data.values())
    audd = sum(v.get("auddCalls", 0) for v in data.values())
    audd_auth_dead = any(v.get("auddAuthDead") for v in data.values())
    tikhub_cost = tikhub * TIKHUB_RATE
    claude_cost = claude_in * CLAUDE_INPUT_RATE + claude_out * CLAUDE_OUTPUT_RATE
    return {
        "perScript": data,
        "totals": {
            "tikhubCalls": tikhub, "claudeCalls": claude_calls,
            "claudeInputTokens": claude_in, "claudeOutputTokens": claude_out,
            "auddCalls": audd, "auddAuthDead": audd_auth_dead,
        },
        "tikhubCost": round(tikhub_cost, 3),
        "claudeCost": round(claude_cost, 3),
        "estCost": round(tikhub_cost + claude_cost, 3),
    }


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
