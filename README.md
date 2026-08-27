# circleci-stats

CircleCI credit-usage widget for the Homepage (gethomepage) landing page.

A scheduled GitHub Action (Mondays 05:45 UTC, plus manual dispatch) pulls the
org's CircleCI **Insights** credit usage, tags each project public/private via
the GitHub API, and writes:

- `stats/current.json` — current snapshot: OSS (400k/mo) vs Private (30k/mo)
  bucket totals + a per-project breakdown.
- `stats/history.json` — weekly time series (idempotent upsert by week).

Both are committed to `main` and served by GitHub Pages:
<https://nickbrett1.github.io/circleci-stats/>

Homepage embeds it with an iframe widget.

## Re-run manually

```sh
gh workflow run weekly-circleci-stats.yml --repo nickbrett1/circleci-stats
```

## Secrets

- `CIRCLE_TOKEN` — CircleCI API token (needs Insights access). Set as a repo
  secret; **never** hardcode.
- `GH_STATS_TOKEN` — fine-grained GitHub PAT with **read** access to
  repositories (for the public/private flag). Commit/push uses `GITHUB_TOKEN`.

## Bucket limits

Defaults are `OSS = 400,000` and `PRIVATE = 30,000` credits/mo. They are
configurable via the `LIMIT_OSS` / `LIMIT_PRIVATE` env vars in the workflow —
CircleCI changes these plans, so verify periodically.

## About the CircleCI Insights API (verified 2026-08-27)

- `reporting-window` (last-24-hours / last-7-days / last-30-days / last-90-days)
  **is honored** and returns genuinely different figures.
- `start-date` / `end-date` are **not supported** on the org summary (silently
  ignored, falls back to the default window), and the per-project `time-series`
  endpoint that would expose granular weekly buckets returns **404** with this
  org token. `grouping=week` is ignored on the summary.
- CircleCI's API tokens (Personal / Project / Org) all use the same
  `Circle-Token` header; there is **no separate token type** that unlocks date
  range / weekly grouping. So the widget uses the **trailing-30-days** snapshot
  for its bucket bars + table, plus a **cumulative multi-window strip**
  (24h / 7d / 30d / 90d) as a lightweight trend instead of a weekly area chart.
