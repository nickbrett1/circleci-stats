#!/usr/bin/env python3
"""CircleCI credit-usage collector for the Homepage widget (cycle-based).

The CircleCI Insights org-summary API only honours `reporting-window`
presets (last-24-hours / last-7-days / last-30-days / last-90-days); it does
NOT support arbitrary start/end dates or weekly grouping, and the per-project
time-series endpoint 404s. So this collector uses those trailing windows to
compute a **daily burn rate** and projects it across the credit cycle (which
starts on a configurable day of the month, default the 23rd).

Writes stats/current.json:
  cycle     - the credit-cycle window (start, end, elapsed, remaining, fraction)
  rates     - daily burn from trailing 7d and 30d windows
  buckets   - per bucket (oss 400k / private 30k): daily burn, estimated usage
              so far, projected end-of-cycle usage + %, and an on-track status
  projects  - per-project recent burn + projected usage (for the table)
  windows   - raw trailing-window org totals (context)

Requires env:
  CIRCLE_TOKEN - CircleCI API token (Circle-Token header).
  GH_TOKEN     - GitHub PAT (repo read) for the public/private flag.
  CYCLE_START_DAY - day of month the credit cycle starts (default 23).
  LIMIT_OSS / LIMIT_PRIVATE - monthly credit limits (default 400000 / 30000).
"""
import argparse
import calendar
import datetime as dt
import json
import os
import sys
import time
import urllib.request

ORG_SLUG = os.environ.get("CIRCLE_ORG", "gh/nickbrett1")
GH_OWNER = os.environ.get("GH_OWNER", "nickbrett1")
CYCLE_START_DAY = int(os.environ.get("CYCLE_START_DAY", "23"))

CIRCLE_API = "https://circleci.com/api/v2"
GITHUB_API = "https://api.github.com"

LIMITS = {
    "oss": int(os.environ.get("LIMIT_OSS", "400000")),
    "private": int(os.environ.get("LIMIT_PRIVATE", "30000")),
}


def circle(path: str, retries: int = 4) -> dict:
    req = urllib.request.Request(CIRCLE_API + path, headers={
        "Circle-Token": os.environ["CIRCLE_TOKEN"], "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))  # 2,4,8s backoff
                continue
            raise
    raise RuntimeError("unreachable")


def gh(path: str) -> dict:
    req = urllib.request.Request(GITHUB_API + path, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def summary(reporting_window: str) -> dict:
    return circle(f"/insights/{ORG_SLUG}/summary?reporting-window={reporting_window}")


def extract(d: dict) -> dict:
    """Pull org + per-project {credits, runs} from an org-summary response."""
    org_m = (d.get("org_data") or {}).get("metrics") or {}
    projects = {}
    for p in d.get("org_project_data") or []:
        m = p.get("metrics") or {}
        projects[p.get("project_name")] = {
            "credits": int(m.get("total_credits_used") or 0),
            "runs": int(m.get("total_runs") or 0),
        }
    return {"credits": int(org_m.get("total_credits_used") or 0),
            "runs": int(org_m.get("total_runs") or 0), "projects": projects}


def project_visibility(projects: list) -> dict:
    out = {}
    for name in projects:
        try:
            out[name] = bool(gh(f"/repos/{GH_OWNER}/{name}").get("private", False))
        except Exception as e:
            print(f"WARN: visibility {name} ({e}); assume public", file=sys.stderr)
            out[name] = False
        time.sleep(0.4)
    return out


def add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def cycle_info(today: dt.date, start_day: int) -> dict:
    start = today.replace(day=start_day)
    if start > today:
        start = (start - dt.timedelta(days=1)).replace(day=start_day)
    end = add_months(start, 1) - dt.timedelta(days=1)
    length = (add_months(start, 1) - start).days
    elapsed = (today - start).days
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "start_day": start_day, "length_days": length,
        "elapsed_days": elapsed, "remaining_days": length - elapsed,
        "fraction_elapsed": round(elapsed / length, 3),
    }


def status(projected_pct: float) -> str:
    if projected_pct >= 100:
        return "exceed"
    if projected_pct >= 80:
        return "at-risk"
    return "on-track"


def bucket_stats(projects: dict, vis: dict, limits: dict, window_days: int,
                 cycle: dict) -> dict:
    credits_oss = credits_priv = runs_oss = runs_priv = 0
    for name, p in projects.items():
        if vis.get(name, False):
            credits_priv += p["credits"]; runs_priv += p["runs"]
        else:
            credits_oss += p["credits"]; runs_oss += p["runs"]
    out = {}
    for key, (credits, runs, limit) in {
            "oss": (credits_oss, runs_oss, limits["oss"]),
            "private": (credits_priv, runs_priv, limits["private"])}.items():
        daily = credits / window_days if window_days else 0
        used_est = daily * cycle["elapsed_days"]
        projected = daily * cycle["length_days"]
        proj_pct = round(100 * projected / limit, 1) if limit else 0
        days_to_runout = (limit / daily) if daily > 0 else None
        out[key] = {
            "limit": limit, "credits_window": credits, "runs": runs,
            "daily_burn": round(daily, 1),
            "used_so_far_est": round(used_est, 0),
            "projected": round(projected, 0), "projected_pct": proj_pct,
            "status": status(proj_pct),
            "days_to_runout": round(days_to_runout) if days_to_runout else None,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stats/current.json")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    e7 = extract(summary("last-7-days")); time.sleep(0.6)
    e30 = extract(summary("last-30-days")); time.sleep(0.6)
    e24 = extract(summary("last-24-hours")); time.sleep(0.6)
    e90 = extract(summary("last-90-days"))

    vis = project_visibility(list(e7["projects"].keys()))

    cyc = cycle_info(today, CYCLE_START_DAY)
    buckets7 = bucket_stats(e7["projects"], vis, LIMITS, 7, cyc)
    buckets30 = bucket_stats(e30["projects"], vis, LIMITS, 30, cyc)

    # Per-project: use recent 7d burn to project end-of-cycle toward its bucket,
    # and estimate credits used so far this cycle (burn * days elapsed).
    projects = []
    for name, p in e7["projects"].items():
        is_private = vis.get(name, False)
        limit = LIMITS["private"] if is_private else LIMITS["oss"]
        daily = p["credits"] / 7
        proj = daily * cyc["length_days"]
        used = daily * cyc["elapsed_days"]
        projects.append({
            "name": name, "private": is_private,
            "recent7_credits": p["credits"], "recent7_runs": p["runs"],
            "daily_burn": round(daily, 1),
            "used_so_far": round(used, 0),
            "projected": round(proj, 0),
            "projected_pct": round(100 * proj / limit, 1) if limit else 0,
        })
    projects.sort(key=lambda x: x["projected"], reverse=True)

    current = {
        "generated_at": now.isoformat(),
        "limits": dict(LIMITS),
        "cycle": cyc,
        "rates": {
            "recent7_daily": round(e7["credits"] / 7, 1),
            "recent30_daily": round(e30["credits"] / 30, 1),
            "recent7_org": e7["credits"], "recent30_org": e30["credits"],
        },
        "buckets": {
            "oss": {**buckets7["oss"],
                    "recent30_credits": buckets30["oss"]["credits_window"],
                    "recent30_pct": round(100 * buckets30["oss"]["credits_window"] / LIMITS["oss"], 1)},
            "private": {**buckets7["private"],
                        "recent30_credits": buckets30["private"]["credits_window"],
                        "recent30_pct": round(100 * buckets30["private"]["credits_window"] / LIMITS["private"], 1)},
        },
        "windows": {
            "last-24-hours": {"credits": e24["credits"], "runs": e24["runs"]},
            "last-7-days": {"credits": e7["credits"], "runs": e7["runs"]},
            "last-30-days": {"credits": e30["credits"], "runs": e30["runs"]},
            "last-90-days": {"credits": e90["credits"], "runs": e90["runs"]},
        },
        "projects": projects,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(current, f, indent=2, sort_keys=True)
    print(f"cycle {cyc['start']}..{cyc['end']} ({cyc['length_days']}d, "
          f"{cyc['elapsed_days']} elapsed)")
    for b, s in current["buckets"].items():
        print(f"  {b}: burn {s['daily_burn']}/d -> projected {s['projected']:,} "
              f"({s['projected_pct']}% of {s['limit']:,}) = {s['status']}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
