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

## Date-range / token caveat

The Insights org-summary endpoint may **ignore** `reporting-window`, `grouping`,
and `start-date`/`end-date` depending on token scope. The collector probes once:
if an old date-range query returns different credits than a recent one it
assumes the API honors dates and backfills ~6 months of weekly buckets;
otherwise it degrades gracefully to a single current snapshot (the weekly chart
stays empty until a properly-scoped token is available).
