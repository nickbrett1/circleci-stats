#!/usr/bin/env python3
"""CircleCI credit-usage collector for the Homepage widget.

Writes:
  stats/current.json — current-window snapshot: per-bucket totals (OSS / private)
                       plus per-project rows for the widget header + table.
  stats/history.json — weekly time series {weeks: [{week, org, projects}]},
                       idempotent (upsert by week key, never trims).

Requires env:
  CIRCLE_TOKEN — CircleCI API token with Insights access.
  GH_TOKEN     — GitHub PAT (repo read) used to tag each project private/public.

Buckets (configurable constants below; verify against your CircleCI plan):
  OSS     = 400,000 credits/mo  (public repos)
  PRIVATE =  30,000 credits/mo  (private repos)

CircleCI does not tag a repo private/public, so visibility is derived from the
GitHub API `private` flag per project.

Date-range caveat (from the design memo): the Insights org-summary endpoint may
ignore `reporting-window`, `grouping`, and `start-date`/`end-date` depending on
token scope (this environment's token returns trailing-30-days regardless). The
collector probes for that ONCE: if an old date-range query returns a different
credit total than a recent one, the API honors date ranges and we backfill
weekly buckets; otherwise we degrade gracefully to a single current snapshot
(history stays shallow until a properly-scoped token is available).
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
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


def summary(start_date: str = "", end_date: str = "") -> dict:
    params = {}
    if start_date:
        params["start-date"] = start_date
    if end_date:
        params["end-date"] = end_date
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return circle(f"/insights/{ORG_SLUG}/summary{qs}")


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


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


def probe_date_support(now: dt.datetime) -> bool:
    """True if the API honors date-range (old-range credits != recent credits)."""
    try:
        old = summary("2025-01-01", "2025-01-31")
        cur = summary()
        return extract(old)["credits"] != extract(cur)["credits"]
    except Exception as e:
        print(f"WARN: date-support probe failed ({e}); assuming not supported",
              file=sys.stderr)
        return False


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


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def build_current(extracted: dict, vis: dict, window: str, now: dt.datetime) -> dict:
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
        "window": window,
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
        "projects": projects,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stats/current.json")
    ap.add_argument("--history", default="stats/history.json")
    ap.add_argument("--backfill", type=int, default=26,
                    help="weeks of history to backfill (only when API honors dates; capped by ~13mo retention)")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    date_supported = probe_date_support(now)
    print(f"API date-range honored: {date_supported}")

    history = load_json(args.history, {"weeks": []})
    seen = {w["week"] for w in history.get("weeks", [])}

    if date_supported:
        # Backfill weekly buckets (oldest -> newest) so we append each once.
        weeks = []
        for k in range(args.backfill, 0, -1):
            end = now.date() - dt.timedelta(days=7 * (k - 1))
            weeks.append(end)
        weeks.append(now.date())
        for end in weeks:
            mon = monday_of(end)
            sun = mon + dt.timedelta(days=6)
            key = mon.isoformat()
            if key in seen:
                continue
            try:
                d = summary(mon.isoformat(), sun.isoformat())
                e = extract(d)
            except Exception as ex:
                print(f"WARN: week {key} failed ({ex})", file=sys.stderr)
                continue
            vis = project_visibility(list(e["projects"].keys()))
            entry = {"week": key,
                     "org": {"credits": e["credits"], "runs": e["runs"]},
                     "projects": {n: {**p, "private": vis.get(n, False)}
                                  for n, p in e["projects"].items()}}
            history.setdefault("weeks", []).append(entry)
            seen.add(key)
            print(f"  week {key}: {e['credits']:,} credits / {e['runs']} runs")
    else:
        # Snapshot-only: keep history as-is (no reliable time series).
        print("WARN: date-range not honored by this token; writing current "
              "snapshot only (history will stay shallow until a scoped token is set)",
              file=sys.stderr)

    history["weeks"].sort(key=lambda w: w["week"])

    # Current snapshot from the default (trailing-30 / month-to-date) query.
    d = summary()
    e = extract(d)
    vis = project_visibility(list(e["projects"].keys()))
    window = "trailing-30-days" if not date_supported else "month-to-date"
    current = build_current(e, vis, window, now)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(current, f, indent=2, sort_keys=True)
    with open(args.history, "w") as f:
        json.dump(history, f, indent=2, sort_keys=True)
    print(f"wrote {args.out} + {args.history}: "
          f"total {current['totals']['total_credits']:,} credits; "
          f"{len(history['weeks'])} weeks in history")


if __name__ == "__main__":
    main()
