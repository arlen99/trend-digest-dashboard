#!/usr/bin/env python3
"""
Reconciles a GitHub Issue against the weekly-digest pipeline's own run log, so a
failure surfaces on its own instead of someone checking the Actions tab and then
asking Claude to dig through it manually (which is what happened for run #7,
2026-07-13 — a TikHub 402 that took a full back-and-forth to diagnose).

Scans the log for known signatures of past incidents this project has actually
hit (TikHub balance, AudD token, Vercel Blob quota, Anthropic auth/billing) —
each of those needs a human to act (top up / renew / upgrade), so it opens or
updates a labelled issue with the specific fix link. GitHub's own notification
system (email/mobile) then reaches whoever has notifications on for this repo —
no separate email/Slack integration needed.

If the pipeline step failed but nothing matches a known signature, it's treated
as a genuine code bug rather than an account/billing issue — opens an issue
labelled the same way but framed as "needs investigation", since that's a job
for a person (or Claude) reading the traceback, not something a shell script
can repair on its own.

If nothing failed and no known signature appears anywhere in the log (including
inside a skipped non-critical step — see weekly-digest.yml's run_optional()),
any existing open issue is closed automatically.

Usage (from the workflow — GITHUB_TOKEN/GITHUB_REPOSITORY/GITHUB_RUN_ID are
already set by GitHub Actions):
  PIPELINE_OUTCOME=success|failure python3 tools/pipeline_notify.py <log_file>
"""
import json
import os
import re
import sys
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
API = f"https://api.github.com/repos/{REPO}"
LABEL = "pipeline-failure"

# (pattern, title, fix) — matched against the whole log. Each is a billing/credential
# exhaustion this project has actually hit before; only the account owner can fix any
# of these, which is exactly the "needs your action" category.
SIGNATURES = [
    (re.compile(r"402[^\n]{0,40}Payment Required|Insufficient balance", re.I),
     "TikHub balance exhausted",
     "TikHub returned 402 Payment Required — account balance hit zero.\n\n"
     "**Fix:** top up at https://user.tikhub.io/users/add_credit, then re-run the job."),
    (re.compile(r'"error_code":\s*900|api_token is incorrect, invalid, or inactive', re.I),
     "AudD token expired",
     "AUDD_TOKEN was rejected (error_code 900 — trial/subscription expired).\n\n"
     "**Fix:** renew at https://dashboard.audd.io and update the AUDD_TOKEN repo secret. "
     "(The pipeline already auto-falls back to AudD's free tier on this specific error, "
     "so this alone may not be why the run failed — check the rest of the log too.)"),
    # The Python pipeline's own except blocks only print str(e) (just "HTTP Error 403:
    # Forbidden" — urllib's HTTPError.__str__ doesn't include the JSON response body),
    # so the specific "store_suspended"/"Your store is blocked" text never actually
    # reaches this log. Match on the co-occurrence pattern real log lines DO produce
    # instead (e.g. "blob read failed: HTTP Error 403: Forbidden").
    (re.compile(r"store_suspended|Your store is blocked|blob[^\n]{0,80}\b403\b|\b403\b[^\n]{0,80}blob", re.I),
     "Vercel Blob store suspended",
     "The Vercel Blob store is rejecting reads/writes (403s on blob calls) — almost "
     "always the Advanced Operations quota being exhausted.\n\n"
     "**Fix:** check the Storage tab at https://vercel.com/dashboard for the reset date "
     "or an upgrade prompt."),
    (re.compile(r"invalid x-api-key|401 Client Error.*anthropic|insufficient_quota", re.I),
     "Anthropic API key/billing issue",
     "A Claude API call failed with an auth/quota error.\n\n"
     "**Fix:** check the ANTHROPIC_API_KEY repo secret and billing at "
     "https://console.anthropic.com."),
]


def gh(method, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": f"Bearer {TOKEN}", "accept": "application/vnd.github+json",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def find_open_issue():
    issues = gh("GET", f"/issues?labels={LABEL}&state=open")
    return issues[0] if issues else None


def classify(text):
    return [(title, fix) for pattern, title, fix in SIGNATURES if pattern.search(text)]


def upsert(heading, body):
    existing = find_open_issue()
    if existing:
        gh("POST", f"/issues/{existing['number']}/comments", {"body": f"Still happening ({RUN_URL}):\n\n{body}"})
        print(f"Commented on existing issue #{existing['number']}")
    else:
        created = gh("POST", "/issues", {"title": heading, "body": body, "labels": [LABEL]})
        print(f"Opened issue #{created['number']}")


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else "pipeline_run.log"
    outcome = os.environ.get("PIPELINE_OUTCOME", "failure")
    text = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
    tail = text[-6000:]

    findings = classify(text)
    if findings:
        titles = ", ".join(t for t, _ in findings)
        heading = f"[ACTION NEEDED] {titles}"
        body = "\n\n---\n\n".join(fix for _, fix in findings)
        body += f"\n\nRun: {RUN_URL}\n\n<details><summary>Log tail</summary>\n\n```\n{tail}\n```\n</details>"
        upsert(heading, body)
        return

    if outcome != "success":
        body = (
            "The pipeline failed but nothing matched a known billing/credential signature — "
            f"looks like a code-level bug, not an account issue.\n\nRun: {RUN_URL}\n\n"
            f"<details><summary>Log tail</summary>\n\n```\n{tail}\n```\n</details>"
        )
        upsert("[INVESTIGATE] Weekly digest pipeline failed", body)
        return

    existing = find_open_issue()
    if existing:
        gh("POST", f"/issues/{existing['number']}/comments",
           {"body": f"Resolved — {RUN_URL} succeeded with no known issues found. Closing."})
        gh("PATCH", f"/issues/{existing['number']}", {"state": "closed"})
        print(f"Closed issue #{existing['number']}")
    else:
        print("No issues found, nothing open to close.")


if __name__ == "__main__":
    main()
