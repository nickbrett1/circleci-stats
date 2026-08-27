#!/usr/bin/env python3
"""CircleCI credit-usage collector for the Homepage widget.

Writes:
  stats/current.json — current snapshot (trailing 30 days): per-bucket totals
                       (OSS / private) + per-project rows + a multi-window
                       trend strip (24h / 7d / 30d / 90d cumulative credits).

Requires env:
  CIRCLE_TOKEN — CircleCI API token (Org / CCIPAT_ token; sent as Circle-Token).
  GH_TOKEN     — GitHub PAT (repo read) used to tag each project private/public.

Buckets (configurable constants below; verify against your CircleCI plan):
  OSS     = 400,000 credits/mo  (public repos)
  PRIVATE =  30,000 credits/mo  (private repos)

CircleCI does not tag a repo private/public, so visibility is derived from the
GitHub API `private` flag per project.

About the CircleCI Insights API (verified against a live org token):
  * `reporting-window` IS honored: last-24-hours / last-7-days / last-30-days /
    last-90-days return genuinely different figures.
  * `start-date` / `end-date` are NOT supported on the org summary (silently
    ignored -> returns the default window). The per-project `time-series`
    endpoint that would expose granular weekly buckets returns 404 with this
    token, and `grouping=week` is ignored on the summary.
  * So a continuous multi-week history is NOT obtainable via this API/token.
    The widget therefore uses the trailing-30-days snapshot for its buckets and
    table, and a cumulative multi-window strip for a lightweight trend.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request

ORG_SLUG = os.environ.get("CIRCLE_ORG", "gh/nickbrett1")
GH_OWNER = os.environ.get("GH_OWNER", "nickbrett1")

CIRCLE_API = "https://circleci.com/api/v2"
GITHUB_API = "https://api.github.com"

# Bucket limits (credits / month). Make configurable — CircleCI changes these.
LIMITS = {
    "oss": int(os.environ.get("LIMIT_OSS", 400000)),
    "private": int(os.environ.get("LIMIT_PRIVATE", 30000)),
}

# Named reporting windows (the ONLY date filtering the org Insights API honors).
WINDOWS = ["last-24-hours", "last-7-days", "last-30-days", "last-90-days"]
CURRENT_WINDOW = os.environ.get("CURRENT_WINDOW", "last-30-days")


def circle(path: str) -> dict:
    req = urllib.request.Request(CIRCLE_API + path, headers={
        "Circle-Token": os.environ["CIRCLE_TOKEN"],
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def gh(path: str) -> dict:
    req = urllib.request.Request(GITHUB_API + path, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def summary(reporting_window: str) -> dict:
    return circle(f"/insights/{ORG_SLUG}/summary?reporting-window={reporting_window}")


def extract(d: dict) -> dict:
    """Pull {credits, runs} out of an org-summary response (org + per-project)."""
    org_m = (d.get("org_data") or {}).get("metrics") or {}
    projects = {}
    for p in d.get("org_project_data") or []:
        m = p.get("metrics") or {}
        projects[p.get("project_name")] = {
            "credits": int(m.get("total_credits_used") or 0),
            "runs": int(m.get("total_runs") or 0),
        }
    return {
        "credits": int(org_m.get("total_credits_used") or 0),
        "runs": int(org_m.get("total_runs") or 0),
        "projects": projects,
    }


def project_visibility(projects: list) -> dict:
    """Map project name -> private bool, via the GitHub API."""
    out = {}
    for name in projects:
        try:
            r = gh(f"/repos/{GH_OWNER}/{name}")
            out[name] = bool(r.get("private", False))
        except Exception as e:
            print(f"WARN: visibility for {name} failed ({e}); assume public",
                  file=sys.stderr)
            out[name] = False
        time.sleep(0.2)
    return out


def build_current(extracted: dict, vis: dict, windows: dict, now: dt.datetime) -> dict:
    """Bucket the snapshot into OSS / private and emit widget-friendly json."""
    oss_c = oss_r = priv_c = priv_r = 0
    projects = []
    for name, p in extracted["projects"].items():
        is_private = vis.get(name, False)
        if is_private:
            priv_c += p["credits"]; priv_r += p["runs"]
        else:
            oss_c += p["credits"]; oss_r += p["runs"]
        projects.append({
            "name": name,
            "credits": p["credits"],
            "runs": p["runs"],
            "private": is_private,
        })
    projects.sort(key=lambda x: x["credits"], reverse=True)
    total = oss_c + priv_c
    return {
        "generated_at": now.isoformat(),
        "window": CURRENT_WINDOW,
        "limits": dict(LIMITS),
        "totals": {
            "total_credits": total,
            "total_runs": extracted["runs"],
            "oss_credits": oss_c,
            "private_credits": priv_c,
        },
        "oss": {
            "credits": oss_c, "runs": oss_r, "limit": LIMITS["oss"],
            "percent": round(100 * oss_c / LIMITS["oss"], 1) if LIMITS["oss"] else 0,
        },
        "private": {
            "credits": priv_c, "runs": priv_r, "limit": LIMITS["private"],
            "percent": round(100 * priv_c / LIMITS["private"], 1) if LIMITS["private"] else 0,
        },
        "windows": windows,
        "projects": projects,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stats/current.json")
    ap.add_argument("--history", default="stats/history.json")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)

    # Multi-window trend strip (org-level credits+runs per named window).
    windows = {}
    for w in WINDOWS:
        try:
            e = extract(summary(w))
            windows[w] = {"credits": e["credits"], "runs": e["runs"]}
        except Exception as ex:
            print(f"WARN: window {w} failed ({ex})", file=sys.stderr)
            windows[w] = {"credits": 0, "runs": 0}
        time.sleep(0.15)
    print("windows:", {k: v["credits"] for k, v in windows.items()})

    # Current snapshot from the CURRENT_WINDOW preset.
    e = extract(summary(CURRENT_WINDOW))
    vis = project_visibility(list(e["projects"].keys()))
    current = build_current(e, vis, windows, now)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(current, f, indent=2, sort_keys=True)
    # history.json retained as a stable {weeks:[]} shape for forward-compat
    # (populated only if a token ever exposes granular weekly buckets).
    try:
        with open(args.history) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {"weeks": []}
    with open(args.history, "w") as f:
        json.dump(history, f, indent=2, sort_keys=True)

    print(f"wrote {args.out} + {args.history}: "
          f"{CURRENT_WINDOW} total {current['totals']['total_credits']:,} credits")


if __name__ == "__main__":
    main()
