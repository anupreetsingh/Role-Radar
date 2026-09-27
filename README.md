# Role Radar

Watches company careers pages and alerts you **once** when a new job matching your criteria is posted.

## How it works

```
config/companies.yaml ─┐
seen_jobs.json ────────┤
                       ▼
  monitor.py ── for each company (concurrent, bounded) ─────────────────────┐
     │   scraper (auto-picked from URL) → public JSON API, or HTML fallback  │
     │   → pre-filter on title/location (cheap)                              │
     │   → fetch detail pages only for promising, unseen jobs                │
     │   → full filter (filters.py)                                          │
     │   → diff vs. stored state (tracker.py): new / removed / repost / dup  │
     └───────────────────────────────────────────────────────────────────────┘
                       ▼
  one batched notification per channel (notifications.py)
                       ▼
  delivered jobs marked notified → seen_jobs.json saved (only if changed)
                       ▼
  GitHub Actions commits seen_jobs.json back (only if changed)
```

All HTTP goes through `http_client.py`. Each request checks robots.txt, waits for a
global concurrency slot and for the per-domain throttle, and retries with exponential
backoff on timeouts, 429 and 5xx (it honours `Retry-After`). If one company fails, that
failure is logged and the other companies still run.

| Module | Role |
|---|---|
| `monitor.py` | CLI entry point and orchestration |
| `config.py` | Loads and validates `companies.yaml` (JSON also accepted) |
| `models.py` | `JobPosting` dataclass, stable `uid` and `fingerprint` |
| `scrapers/` | One class per ATS plus `generic.py`; registry in `scrapers/__init__.py` |
| `filters.py` | Keyword, location and employment-type rules |
| `tracker.py` | New-job, removal, repost and duplicate detection |
| `storage.py` | `StateStore` interface + JSON implementation |
| `notifications.py` | `Notifier` interface + Discord, email and console |

### Supported sources

| ATS | Endpoint used | Detail request? |
|---|---|---|
| BambooHR | `{sub}.bamboohr.com/careers/list` + `/careers/{id}/detail` | yes (for description and date) |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | no |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | no |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` | no |
| Workday | `POST {host}/wday/cxs/{tenant}/{site}/jobs` (paginated) | yes |
| Custom | Embedded-ATS detection → JSON-LD `JobPosting` → link heuristic | JSON-LD on job page |

The ATS is detected from the URL, or you can set it with `ats:`. Nothing tries to get
past CAPTCHAs, logins or robots.txt. Pages that only render with JavaScript aren't
supported, so point the config at the ATS the page loads its jobs from.

### Job identity and new-job rules

- **uid** = `company:ats:job_id`. If the ATS gives no job ID, it's a hash of the
  normalized company, title, location and URL (tracking query parameters are removed).
- **fingerprint** = hash of company, title and location. It's used to catch the same
  role showing up again under a different ID.
- A job alerts **only when it matches the filters and has never been notified**.

| Case | Behaviour |
|---|---|
| New job | Recorded; alerted if it matches |
| Existing job unchanged | Nothing |
| Job removed | `removed_at` set, but only if the listing was complete (a hit page cap or a heuristic parse never counts as removal) |
| Removed job comes back with the same ID | Reactivated, no new alert |
| Reposted with a new ID within `repost_window_days` | Recorded as `duplicate_of`, no alert |
| Two open postings with the same title and location | Second one suppressed as a duplicate |
| Same title in several locations | Separate jobs, grouped into **one** entry in the notification |
| Notification failed | Job stays un-notified and is retried next run |
| Filters broadened | Jobs that already exist and now match alert once |

Removed jobs are pruned from state after `retention_days`.

## Configuration

Edit [config/companies.yaml](config/companies.yaml). Keys under `defaults.filters` apply
to every company, and a company's own `filters` replace them **one key at a time**.

```yaml
- name: Continental Finance
  url: https://contfinco.bamboohr.com/careers
  filters:
    include_keywords: [software engineer, developer, machine learning, AI, data engineer]
    exclude_keywords: [manager, director, vice president, chief]
    match_on: [title]            # title | description | location | employment_type | department
    locations: [Remote, Wilmington]   # optional
    employment_types: [full-time]     # optional
```

Matching is case-insensitive and respects word boundaries, so `AI` does not match
"Maintain". A space in a keyword also matches `-`, `/` and `_`, and `re:` lets you use a
regex. To change the logic itself, edit `JobFilter.evaluate` in `filters.py`. To make a
new field matchable, add it to `FIELD_GETTERS`.

For Continental Finance, `senior` is deliberately **not** excluded, because its current
tech opening is titled "Mid/Senior Software Developer". The filter excludes only
management and executive titles.

### Adding an ATS

1. Create `scrapers/myats.py` with `class MyATSScraper(BaseScraper)`, setting `name` and
   `domains`, and implement `fetch_jobs()`. Implement `fetch_details()` too if the
   listing is thin.
2. Add the class to `SCRAPERS` in `scrapers/__init__.py`.

### Swapping the storage backend

Subclass `storage.StateStore` and implement `load() -> MonitorState` and
`save(MonitorState)`, for example with SQLite or DynamoDB, then pass it to
`monitor.run()` in place of `JsonStateStore`.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest                          # tests
python monitor.py --list-matches          # show every job matching now (no state, no alerts)
python monitor.py --dry-run -v            # full run, alerts printed to stdout, state not saved
python monitor.py --company "Continental Finance" --dry-run

cp .env.example .env                      # fill in a Discord webhook and/or SMTP settings
set -a; source .env; set +a
python monitor.py                         # real run: sends alerts, updates seen_jobs.json
python monitor.py --baseline              # mark everything current as seen, no alerts
```

If no notification env vars are set, alerts go to stdout. The exit code is `1` when
every company failed or no notification channel delivered, and `0` otherwise.

## GitHub Secrets

Go to the repo, then **Settings → Secrets and variables → Actions → New repository
secret**. Add only the secrets for the channels you use.

| Secret | Example |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord channel → Edit → Integrations → Webhooks → Copy URL |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_SECURITY` | `starttls` (or `ssl` for port 465) |
| `SMTP_USERNAME` | `you@gmail.com` |
| `SMTP_PASSWORD` | A Gmail **App Password** (not your account password) |
| `EMAIL_FROM` | `you@gmail.com` |
| `EMAIL_TO` | `you@gmail.com,other@example.com` |

With the CLI: `gh secret set DISCORD_WEBHOOK_URL` (it prompts for the value, which
keeps it out of your shell history).

Secrets are only passed to the "Run scraper" step, and GitHub masks their values in logs.
The code never logs webhook URLs or credentials, and it turns `httpx` request logging down
to WARNING because that logger prints full URLs. Discord messages set
`allowed_mentions: none`, so a job title containing `@everyone` can't ping your server.

## Deploy with GitHub Actions

1. Push this project to a GitHub repo, with `seen_jobs.json` committed.
2. Add the secrets listed above.
3. **Settings → Actions → General → Workflow permissions → Read and write permissions**,
   so the workflow can push state.
4. **Actions → Job Monitor → Run workflow**. Tick **baseline** on the first run if you
   don't want alerts for jobs that are already open.
5. After that it runs every 30 minutes (`cron: "*/30 * * * *"`, UTC).

### Persisting state: why commit it back

| Option | Verdict |
|---|---|
| **Commit `seen_jobs.json` to the repo** | ✅ **Used here.** Durable, versioned, readable, no extra infrastructure. |
| `actions/cache` | ❌ Caches are immutable per key and evicted after 7 days unused, so it's best-effort and can drop state and re-alert everything. |
| Artifacts | ❌ Downloading the previous run's artifact needs API calls, and artifacts expire. |
| External DB/S3/Gist | ✅ Good at scale. Implement a `StateStore` subclass. |

The workflow commits safely:

- The state file is written with sorted keys and no per-run timestamps, and it isn't
  rewritten when nothing changed. Most runs therefore produce **no commit**
  (`git diff --quiet` exits early).
- `concurrency: job-monitor` stops two runs from racing on the file.
- Commits use `[skip ci]` and the built-in `GITHUB_TOKEN`, which doesn't trigger other
  workflows anyway. Pushes do `pull --rebase` and retry, so a manual push in between
  doesn't break the run.
- The commit step runs even if the scraper exits non-zero, so progress from companies that
  succeeded isn't lost.

Things to know about GitHub scheduling:

- Scheduled runs can start several minutes late when GitHub is busy.
- In public repos, scheduled workflows are disabled after 60 days without repository
  activity.
- If you edit `seen_jobs.json` locally, pull first.
- In a public repo, the state file (job titles and URLs only, no secrets) is public too.

## Scaling to 50–100 companies

- The defaults allow 8 concurrent requests overall and 1 request per second per host.
  Most boards take one request.
- Detail requests are made only for unseen jobs that pass the pre-filter, capped at
  `max_detail_requests` per company per run. Any left over are picked up next run.
- For very large Workday tenants, set `options.search_text` and `options.max_jobs`.
- When adding many companies at once, set `notify_on_first_run: false`, or run once with
  `--baseline`.
