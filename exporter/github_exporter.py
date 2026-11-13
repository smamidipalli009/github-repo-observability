#!/usr/bin/env python3
"""
GitHub metrics exporter → Prometheus Pushgateway

Collects DORA metrics + CI/CD health for a GitHub repository.
Runs on a configurable interval and pushes to Pushgateway.

Usage:
  export GITHUB_TOKEN=ghp_xxxx
  export GITHUB_OWNER=smamidipalli009
  export GITHUB_REPO=multi-language-appsec-pipeline
  python3 github_exporter.py
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Config from environment ---
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
OWNER          = os.environ.get("GITHUB_OWNER", "smamidipalli009")
REPO           = os.environ.get("GITHUB_REPO", "multi-language-appsec-pipeline")
PUSHGATEWAY    = os.environ.get("PUSHGATEWAY_URL", "localhost:9091")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "300"))   # seconds
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "30"))    # for DORA calculations

GITHUB_API = "https://api.github.com"
SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_get(path, params=None):
    url = f"{GITHUB_API}{path}"
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    remaining = int(r.headers.get("X-RateLimit-Remaining", 9999))
    if remaining < 100:
        log.warning("GitHub rate limit low: %d remaining", remaining)
    return r.json()


def gh_paginate(path, params=None, max_pages=5):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results = []
    for page in range(1, max_pages + 1):
        params["page"] = page
        data = gh_get(path, params)
        if not data:
            break
        results.extend(data)
        if len(data) < params["per_page"]:
            break
    return results


def since_days(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def collect(registry):
    repo_path = f"/repos/{OWNER}/{REPO}"
    repo_label = {"owner": OWNER, "repo": REPO}

    # ---- Active workflow runs -----------------------------------------------
    g_active = Gauge(
        "github_active_workflow_runs",
        "Workflow runs currently queued or in_progress",
        ["owner", "repo", "workflow", "status"],
        registry=registry,
    )
    try:
        all_workflow_names = set()
        status_counts = {"queued": {}, "in_progress": {}}
        for status in ("queued", "in_progress"):
            runs = gh_get(f"{repo_path}/actions/runs", {"status": status, "per_page": 50})
            for run in runs.get("workflow_runs", []):
                name = run["name"]
                all_workflow_names.add(name)
                status_counts[status][name] = status_counts[status].get(name, 0) + 1
        # Always emit 0 for workflows not currently active so stale bars clear
        known = gh_get(f"{repo_path}/actions/workflows").get("workflows", [])
        for wf in known:
            all_workflow_names.add(wf["name"])
        for status in ("queued", "in_progress"):
            for wf_name in all_workflow_names:
                g_active.labels(owner=OWNER, repo=REPO, workflow=wf_name, status=status).set(
                    status_counts[status].get(wf_name, 0)
                )
        log.info("active runs collected")
    except Exception as e:
        log.error("active runs: %s", e)

    # ---- Recent workflow runs (last LOOKBACK_DAYS) ---------------------------
    g_runs = Gauge(
        "github_workflow_runs_total",
        "Total workflow runs in lookback window by conclusion",
        ["owner", "repo", "workflow", "conclusion"],
        registry=registry,
    )
    g_duration = Gauge(
        "github_workflow_run_duration_seconds",
        "Average duration of completed workflow runs (lookback window)",
        ["owner", "repo", "workflow", "conclusion"],
        registry=registry,
    )
    g_last_run = Gauge(
        "github_workflow_last_run_timestamp",
        "Unix timestamp of the most recent run per workflow",
        ["owner", "repo", "workflow", "conclusion"],
        registry=registry,
    )

    try:
        all_runs = gh_get(
            f"{repo_path}/actions/runs",
            {"created": f">={since_days(LOOKBACK_DAYS)}", "per_page": 100},
        ).get("workflow_runs", [])

        # Bucket by (workflow_name, conclusion)
        counts = {}
        durations = {}
        last_ts = {}

        for run in all_runs:
            if run["status"] != "completed":
                continue
            key = (run["name"], run["conclusion"] or "unknown")
            counts[key] = counts.get(key, 0) + 1

            if run["created_at"] and run["updated_at"]:
                try:
                    start = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                    end   = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                    secs  = (end - start).total_seconds()
                    if secs > 0:
                        durations.setdefault(key, []).append(secs)
                except Exception:
                    pass

            if run["updated_at"]:
                try:
                    ts = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00")).timestamp()
                    if key not in last_ts or ts > last_ts[key]:
                        last_ts[key] = ts
                except Exception:
                    pass

        for (wf, conclusion), count in counts.items():
            g_runs.labels(owner=OWNER, repo=REPO, workflow=wf, conclusion=conclusion).set(count)
        for (wf, conclusion), secs_list in durations.items():
            avg = sum(secs_list) / len(secs_list)
            g_duration.labels(owner=OWNER, repo=REPO, workflow=wf, conclusion=conclusion).set(avg)
        for (wf, conclusion), ts in last_ts.items():
            g_last_run.labels(owner=OWNER, repo=REPO, workflow=wf, conclusion=conclusion).set(ts)

        log.info("workflow runs collected: %d total runs", len(all_runs))
    except Exception as e:
        log.error("workflow runs: %s", e)

    # ---- DORA metrics -------------------------------------------------------
    g_deploy_freq = Gauge(
        "github_deploy_frequency_per_day",
        "Successful workflow runs per day (30-day average)",
        ["owner", "repo"],
        registry=registry,
    )
    g_cfr = Gauge(
        "github_change_failure_rate_percent",
        "Percentage of workflow runs that failed (30-day window)",
        ["owner", "repo"],
        registry=registry,
    )
    g_mttr = Gauge(
        "github_mttr_hours",
        "Mean time to recovery: avg hours from failure to next success per workflow",
        ["owner", "repo"],
        registry=registry,
    )

    try:
        total = sum(counts.values())
        success = sum(v for (_, c), v in counts.items() if c == "success")
        failure = sum(v for (_, c), v in counts.items() if c == "failure")

        deploy_freq = success / LOOKBACK_DAYS if LOOKBACK_DAYS > 0 else 0
        cfr = (failure / total * 100) if total > 0 else 0

        g_deploy_freq.labels(**repo_label).set(round(deploy_freq, 4))
        g_cfr.labels(**repo_label).set(round(cfr, 2))

        # MTTR: for each workflow, find failure→success pairs and avg the gap
        mttr_gaps = []
        wf_runs = {}
        for run in sorted(all_runs, key=lambda r: r.get("updated_at", "")):
            if run["status"] != "completed":
                continue
            wf = run["name"]
            wf_runs.setdefault(wf, []).append(run)

        for wf, runs_list in wf_runs.items():
            last_failure_time = None
            for run in runs_list:
                ts = run.get("updated_at", "")
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if run["conclusion"] == "failure":
                    last_failure_time = t
                elif run["conclusion"] == "success" and last_failure_time:
                    gap = (t - last_failure_time).total_seconds() / 3600
                    if gap > 0:
                        mttr_gaps.append(gap)
                    last_failure_time = None

        avg_mttr = sum(mttr_gaps) / len(mttr_gaps) if mttr_gaps else 0
        g_mttr.labels(**repo_label).set(round(avg_mttr, 2))

        log.info("DORA: deploy_freq=%.2f/day cfr=%.1f%% mttr=%.1fh", deploy_freq, cfr, avg_mttr)
    except Exception as e:
        log.error("DORA metrics: %s", e)

    # ---- Open issues --------------------------------------------------------
    g_issues = Gauge(
        "github_open_issues_total",
        "Number of open issues",
        ["owner", "repo"],
        registry=registry,
    )
    try:
        repo_info = gh_get(f"{repo_path}")
        g_issues.labels(**repo_label).set(repo_info.get("open_issues_count", 0))
        log.info("issues: %d open", repo_info.get("open_issues_count", 0))
    except Exception as e:
        log.error("issues: %s", e)

    # ---- Open PRs -----------------------------------------------------------
    g_prs = Gauge(
        "github_open_prs_total",
        "Number of open pull requests",
        ["owner", "repo"],
        registry=registry,
    )
    g_pr_age = Gauge(
        "github_pr_age_hours",
        "Age of each open PR in hours",
        ["owner", "repo", "pr_number", "title"],
        registry=registry,
    )
    try:
        prs = gh_paginate(f"{repo_path}/pulls", {"state": "open"})
        g_prs.labels(**repo_label).set(len(prs))
        now = datetime.now(timezone.utc)
        for pr in prs:
            try:
                created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                age_h = (now - created).total_seconds() / 3600
                title = pr["title"][:40]
                g_pr_age.labels(owner=OWNER, repo=REPO, pr_number=str(pr["number"]), title=title).set(round(age_h, 1))
            except Exception:
                pass
        log.info("PRs: %d open", len(prs))
    except Exception as e:
        log.error("PRs: %s", e)

    # ---- Workflow definitions -----------------------------------------------
    g_wf_count = Gauge(
        "github_workflows_total",
        "Total number of workflow files in the repository",
        ["owner", "repo"],
        registry=registry,
    )
    g_wf_state = Gauge(
        "github_workflow_active",
        "1 if workflow is active, 0 if disabled",
        ["owner", "repo", "workflow", "path"],
        registry=registry,
    )
    try:
        workflows = gh_get(f"{repo_path}/actions/workflows").get("workflows", [])
        g_wf_count.labels(**repo_label).set(len(workflows))
        for wf in workflows:
            g_wf_state.labels(
                owner=OWNER, repo=REPO,
                workflow=wf["name"],
                path=wf["path"],
            ).set(1 if wf["state"] == "active" else 0)
        log.info("workflows: %d total", len(workflows))
    except Exception as e:
        log.error("workflows: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once():
    registry = CollectorRegistry()
    collect(registry)
    try:
        push_to_gateway(
            PUSHGATEWAY,
            job=f"github_{OWNER}_{REPO}",
            registry=registry,
        )
        log.info("pushed to gateway %s", PUSHGATEWAY)
    except Exception as e:
        log.error("push failed: %s", e)


def main():
    log.info("starting github exporter — %s/%s → %s (every %ds)", OWNER, REPO, PUSHGATEWAY, POLL_INTERVAL)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
