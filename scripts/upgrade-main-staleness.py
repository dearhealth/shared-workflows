#!/usr/bin/env python3
"""Org-level staleness monitor for the projen `upgrade-main` workflow (DNA-3277).

Every projen repo in the org runs an `upgrade-main` workflow on a daily cron
(`0 0 * * *`). Per-repo alerting already exists: `upgrade-main` has an
`alert-on-failure` job (@dearhealth/projen >= 4.65.1) that opens an issue in the
repo when the upgrade job fails. That covers *loud* failures.

It does not cover *silence*, which is the more dangerous mode:

  * GitHub auto-disables scheduled workflows in repos with no pushes for 60 days
    (`state: disabled_inactivity`). No run happens, so no failure, so no alert.
  * A workflow can be disabled manually (`disabled_manually`), again producing no
    signal at all.
  * A repo can simply stop producing successful runs (every run cancelled or
    skipped) without the failure path ever firing.

This script closes that gap from the org's meta-repo: it enumerates every
non-archived org repo that has an `upgrade-main` workflow, finds the most recent
*successful* run, and flags anything older than the threshold (default 48h — one
full day of grace on top of the daily cron) plus anything whose workflow is not
in the `active` state.

The result is reconciled into exactly ONE auto-managed issue in this repo
(`upgrade-main staleness report`): created when repos are stale, edited in place
while they stay stale, and closed when everything is healthy again.

Auth
----
The default `GITHUB_TOKEN` of this repo cannot read *other* repositories'
Actions data, so a cross-repo token is required. See
`.github/workflows/upgrade-main-staleness.yml` for the token fallback chain.

Usage
-----
    GH_TOKEN=... python3 scripts/upgrade-main-staleness.py [--dry-run]

Env:
    GH_TOKEN / GITHUB_TOKEN  token for cross-repo Actions reads (required)
    REPORT_TOKEN             token for issue writes in REPORT_REPO (default:
                             falls back to GH_TOKEN). In CI this is the
                             workflow's own GITHUB_TOKEN, so the org-read PAT
                             needs no Issues permission at all.
    ORG                      org to scan (default: dearhealth)
    REPORT_REPO              owner/name of the repo holding the report issue
                             (default: dearhealth/shared-workflows)
    STALE_HOURS              staleness threshold in hours (default: 48)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
ORG = os.environ.get("ORG", "dearhealth")
REPORT_REPO = os.environ.get("REPORT_REPO", "dearhealth/shared-workflows")
STALE_HOURS = int(os.environ.get("STALE_HOURS", "48"))
WORKFLOW_FILE = "upgrade-main.yml"
ISSUE_TITLE = "upgrade-main staleness report"
ISSUE_MARKER = "<!-- upgrade-main-staleness-monitor -->"
# Per-request socket timeout. ~340 requests per run, so this is a cap on a single
# stalled connection, not on the run.
HTTP_TIMEOUT_SECONDS = 30

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("GH_TOKEN (or GITHUB_TOKEN) must be set")
# Issue reads/writes in REPORT_REPO use the repo-scoped workflow token, so the
# cross-repo PAT can stay Actions+Metadata read-only.
REPORT_TOKEN = os.environ.get("REPORT_TOKEN") or TOKEN


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def request(method, path, body=None, *, allow=(), token=None):
    """Perform one API call. Returns (status, parsed_json_or_None).

    Statuses listed in `allow` are returned rather than raising; any other
    non-2xx raises. Transient 429/5xx responses are retried twice.
    """
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token or TOKEN}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            # An explicit timeout matters here: without one a stalled connection
            # hangs until the Actions runner's own hard timeout, and because the
            # job holds a `concurrency` group that would also block the next
            # scheduled run.
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            if err.code in allow:
                return err.code, None
            if err.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            detail = err.read().decode(errors="replace")[:400]
            raise SystemExit(f"{method} {url} -> HTTP {err.code}: {detail}") from err
        except urllib.error.URLError as err:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"{method} {url} -> {err}") from err
    raise SystemExit(f"{method} {url} -> exhausted retries")


def get(path, *, allow=()):
    return request("GET", path, allow=allow)


def paginate(path, key=None):
    """Yield every item across all pages of a list endpoint."""
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        _, payload = get(f"{path}{sep}per_page=100&page={page}")
        items = payload if key is None else payload.get(key, [])
        if not items:
            return
        yield from items
        if len(items) < 100:
            return
        page += 1


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def parse_ts(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def age_str(delta):
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def collect(now):
    """Return (stale_rows, healthy_count, scanned_count)."""
    cutoff = now - timedelta(hours=STALE_HOURS)
    stale = []
    healthy = 0
    scanned = 0

    repos = [
        r for r in paginate(f"/orgs/{ORG}/repos?type=all&sort=full_name") if not r["archived"]
    ]
    print(f"{len(repos)} non-archived repos in {ORG}", file=sys.stderr)

    for repo in repos:
        name = repo["name"]
        status, workflow = get(
            f"/repos/{ORG}/{name}/actions/workflows/{WORKFLOW_FILE}", allow=(404,)
        )
        if status == 404 or workflow is None:
            continue  # repo does not participate in the upgrade-main programme
        scanned += 1

        state = workflow.get("state", "unknown")
        _, runs = get(
            f"/repos/{ORG}/{name}/actions/workflows/{WORKFLOW_FILE}/runs"
            "?status=success&per_page=1&exclude_pull_requests=true"
        )
        entries = runs.get("workflow_runs", []) if runs else []
        last_ok = parse_ts(entries[0]["updated_at"]) if entries else None
        last_ok_url = entries[0]["html_url"] if entries else None

        reasons = []
        if state != "active":
            reasons.append(
                "workflow auto-disabled by GitHub after 60 days of repo inactivity"
                if state == "disabled_inactivity"
                else f"workflow state `{state}`"
            )
        if last_ok is None:
            reasons.append("no successful run on record")
        elif last_ok < cutoff:
            reasons.append(f"last success {age_str(now - last_ok)} ago")

        if reasons:
            stale.append(
                {
                    "repo": name,
                    "state": state,
                    "last_success": last_ok.strftime("%Y-%m-%d %H:%M UTC")
                    if last_ok
                    else "never",
                    "age": age_str(now - last_ok) if last_ok else "-",
                    "run_url": last_ok_url,
                    "reasons": reasons,
                    "sort_key": last_ok.timestamp() if last_ok else 0.0,
                }
            )
        else:
            healthy += 1

    stale.sort(key=lambda row: row["sort_key"])
    return stale, healthy, scanned


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
def render_body(stale, healthy, scanned, now):
    workflow_link = (
        f"https://github.com/{REPORT_REPO}/blob/main/"
        ".github/workflows/upgrade-main-staleness.yml"
    )
    lines = [
        ISSUE_MARKER,
        "",
        f"> Auto-managed by [`upgrade-main-staleness.yml`]({workflow_link}) — this body is"
        " rewritten on every run and the issue is closed automatically once every repo is"
        " healthy. Do not edit it by hand; edits are overwritten.",
        "",
        f"**Scanned:** {scanned} repos with an `upgrade-main` workflow "
        f"({healthy} healthy, {len(stale)} stale)  ",
        f"**Threshold:** no successful `upgrade-main` run in the last {STALE_HOURS}h  ",
        f"**Last checked:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"## {len(stale)} stale repo(s)",
        "",
        "| Repo | Workflow state | Last successful run | Age | Why it is flagged |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in stale:
        last = row["last_success"]
        if row["run_url"]:
            last = f"[{last}]({row['run_url']})"
        repo_link = f"https://github.com/{ORG}/{row['repo']}/actions/workflows/{WORKFLOW_FILE}"
        lines.append(
            f"| [{row['repo']}]({repo_link}) | `{row['state']}` | {last} | {row['age']} "
            f"| {'; '.join(row['reasons'])} |"
        )

    lines += [
        "",
        "### How to fix",
        "",
        "* **`disabled_inactivity`** — GitHub disables scheduled workflows in repos with no pushes"
        " for 60 days. Re-enable with"
        f" `gh workflow enable {WORKFLOW_FILE} --repo {ORG}/<repo>`. It will be disabled again"
        " after another 60 quiet days, and this monitor will say so.",
        "* **`disabled_manually`** — someone turned the workflow off. Re-enable it the same way,"
        " or archive the repo if it is genuinely dead (archived repos are skipped).",
        "* **Runs happening but never succeeding** — open the linked workflow page. Per-repo"
        " failure alerting (`alert-on-failure`) files an issue in the repo itself, so a row here"
        " means either that alert is also stale or the runs are being cancelled/skipped rather"
        " than failing.",
        "* **`no successful run on record`** — usually a repo that has never had a green upgrade"
        " run since adopting the workflow. Check the first run's logs.",
    ]
    return "\n".join(lines)


def find_report_issue():
    """The one open issue this monitor owns, or None.

    Ownership is decided by ISSUE_MARKER in the BODY, not by the title. The title
    alone is not safe to act on: this function's result gets its body rewritten
    and eventually closed, so a human-opened issue that happens to share the
    title would be silently clobbered. The marker is only ever written by
    `render_body`, so matching on it means we only ever touch our own issue. A
    title collision just means we open a second issue alongside the human's.
    """
    query = urllib.parse.quote(
        f'repo:{REPORT_REPO} is:issue is:open in:title "{ISSUE_TITLE}"', safe=""
    )
    # REPORT_TOKEN: with a least-privilege scan PAT (no Issues read), this
    # search would silently return nothing and we would open duplicates.
    _, payload = request("GET", f"/search/issues?q={query}", token=REPORT_TOKEN)
    for item in (payload or {}).get("items", []):
        if item["title"] == ISSUE_TITLE and ISSUE_MARKER in (item.get("body") or ""):
            return item
    return None


def reconcile(stale, healthy, scanned, now, dry_run):
    issue = find_report_issue()

    if not stale:
        if issue:
            msg = (
                f"{ISSUE_MARKER}\n\nAll {scanned} repos with an `upgrade-main` workflow have a"
                f" successful run within the last {STALE_HOURS}h as of"
                f" {now.strftime('%Y-%m-%d %H:%M UTC')}. Closing automatically."
            )
            print(f"closing #{issue['number']} - nothing stale")
            if not dry_run:
                request(
                    "POST",
                    f"/repos/{REPORT_REPO}/issues/{issue['number']}/comments",
                    {"body": msg},
                    token=REPORT_TOKEN,
                )
                request(
                    "PATCH",
                    f"/repos/{REPORT_REPO}/issues/{issue['number']}",
                    {"state": "closed", "state_reason": "completed"},
                    token=REPORT_TOKEN,
                )
        else:
            print(f"nothing stale across {scanned} repos - no issue to manage")
        return

    body = render_body(stale, healthy, scanned, now)
    if issue:
        print(f"updating #{issue['number']} - {len(stale)} stale")
        if not dry_run:
            request(
                "PATCH",
                f"/repos/{REPORT_REPO}/issues/{issue['number']}",
                {"body": body},
                token=REPORT_TOKEN,
            )
        return

    print(f"creating report issue - {len(stale)} stale")
    if not dry_run:
        _, created = request(
            "POST",
            f"/repos/{REPORT_REPO}/issues",
            {"title": ISSUE_TITLE, "body": body},
            token=REPORT_TOKEN,
        )
        print(created["html_url"])


def main():
    parser = argparse.ArgumentParser(description="upgrade-main staleness monitor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and print the report without creating/updating/closing the issue",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    stale, healthy, scanned = collect(now)

    if scanned == 0:
        # Almost always an auth problem: a token without cross-repo actions:read
        # sees zero upgrade-main workflows rather than erroring outright.
        sys.exit(
            "No repo with an upgrade-main workflow was visible. The token most likely cannot "
            "read other repositories' Actions data - provision ORG_ACTIONS_READ_TOKEN "
            "(fine-grained PAT, Actions: read-only on all org repos)."
        )

    report = (
        render_body(stale, healthy, scanned, now)
        if stale
        else f"### upgrade-main staleness\n\nAll {scanned} repos healthy.\n"
    )
    print(report)
    reconcile(stale, healthy, scanned, now, args.dry_run)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")


if __name__ == "__main__":
    main()
