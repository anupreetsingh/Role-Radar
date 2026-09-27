# Role Radar

Watches company careers pages and alerts you **once** when a new job matching your criteria is posted.

## How it works

```
config/companies.yaml ─┐
seen_jobs.json ────────┤  (seen jobs + each company's next check time)
                       ▼
  monitor.py ── picks the companies that are due (schedule.py) ─────────────┐
     │ for each due company (concurrent, bounded):                           │
     │   scraper (auto-picked from URL) → public JSON API, or HTML fallback  │
     │   → detail page only if the filter needs a field the listing lacks    │
     │     (Workday, custom sites) and the job could still match             │
     │   → filter (filters.py)                                               │
     │   → diff vs. stored state (tracker.py): new / removed / repost / dup  │
     │   → one notification for its new matches (notifications.py)           │
     │   → delivered jobs marked notified; its state and next check saved    │
     └───────────────────────────────────────────────────────────────────────┘
                       ▼
  GitHub Actions commits seen_jobs.json back (only if changed)
```

All HTTP goes through `http_client.py`. Each request checks robots.txt, waits until its
host's delay has passed since the previous request to that host, then takes one of the
global concurrency slots, and retries with exponential backoff on timeouts, 429 and 5xx
(it honours `Retry-After`). The host wait comes first, so requests queued behind a busy
shared host such as `boards-api.greenhouse.io` don't hold slots that other hosts could
use. If one company fails, that failure is logged and the other companies still run.

At the end of every run the log shows how long it took, the number of requests and bytes
downloaded (compressed, as received), and the busiest hosts. Run with `-v` to see every
host.

All modules live in the `role_radar/` package.

| Module | Role |
|---|---|
| `cli.py` | The `role-radar` command |
| `runner.py` | Holds the lease and runs passes: the laptop loop, `run --once`, Lambda |
| `monitor.py` | One pass: due companies → scrape → filter → alert → save |
| `schedule.py` | Which companies are due, and when each is next checked |
| `config.py` | Loads and validates `companies.yaml` (JSON also accepted), including `runtime:` |
| `models.py` | `JobPosting` dataclass, stable `uid` and `fingerprint` |
| `scrapers/` | One class per ATS plus `generic.py`; registry in `scrapers/__init__.py` |
| `filters.py` | Keyword, location and employment-type rules |
| `tracker.py` | New-job, removal, repost and duplicate detection |
| `storage.py` | `StateStore` interface + JSON and in-memory implementations |
| `lease.py` | The lease that decides which runner may work, and fencing |
| `dynamo.py` | DynamoDB `StateStore` and lease (one table) |
| `aws.py` | boto3 helpers: the config file in S3, secrets in SSM |
| `backends.py` | Picks the store, lease, config source and secrets from `runtime:` |
| `notifications.py` | `Notifier` interface + Discord, email and console |
| `instance.py` | One `role-radar start` per machine; how `stop` finds it |
| `launchd.py` | The macOS login item |

### Supported sources

| ATS | Endpoint used | Detail request? |
|---|---|---|
| BambooHR | `{sub}.bamboohr.com/careers/list` | no |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` | no |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | no |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` | no |
| Workday | `POST {host}/wday/cxs/{tenant}/{site}/jobs` (paginated) | only if the filter uses employment type, or uses location and the job is listed as "N Locations" |
| Custom | Embedded-ATS detection → JSON-LD `JobPosting` → link heuristic | only if the filter needs a location or employment type the listing lacks (read from the job page's JSON-LD) |

**Job descriptions are never downloaded or stored.** A job's title, location, ID and URL
are enough to alert on. Lever and Ashby put descriptions in their API responses anyway,
with no way to turn them off, so they're dropped while parsing.

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
| Notification failed | Job stays un-notified and is retried at the company's next check |
| Filters broadened | Jobs that already exist and now match alert once |

Removed jobs are pruned from state after `retention_days`.

### Scheduling

Companies aren't all checked at once. Each one is checked when it's **due**: when
`check_interval_minutes` (30) have passed since its last check, or straight away if it
has never been checked. Each company's state is saved as soon as that company finishes,
so an interrupted run loses at most the companies still in flight. Those companies are
still due and get picked up next time.

- A check that ran more than half an interval late (a company's first check, or catching
  up after an outage) gets a random extra 0–30 minutes before its next check. Otherwise
  a burst of catch-up checks would come due together every half hour forever. With it,
  the checks spread out across the half hour after one round.
- A failing company is still rescheduled, so it isn't retried every run. From the third
  failure in a row its interval doubles each time, up to every 4 hours. One success
  resets it.
- `--all` checks every company whether it's due or not, and so do `--company` and
  `--baseline`.

## Configuration

Edit [config/companies.yaml](config/companies.yaml). Keys under `defaults.filters` apply
to every company, and a company's own `filters` replace them **one key at a time**.

```yaml
- name: Continental Finance
  url: https://contfinco.bamboohr.com/careers
  filters:
    include_keywords: [software engineer, developer, machine learning, AI, data engineer]
    exclude_keywords: [manager, director, vice president, chief]
    match_on: [title]            # title | location | employment_type | department
    locations: [Remote, Wilmington]   # optional
    employment_types: [full-time]     # optional
```

Matching is case-insensitive and respects word boundaries, so `AI` does not match
"Maintain". A space in a keyword also matches `-`, `/` and `_`, and `re:` lets you use a
regex. To change the logic itself, edit `JobFilter.evaluate` in `filters.py`. To make a
new field matchable, add it to `FIELD_GETTERS`. Descriptions can't be matched, and a
config that puts `description` in `match_on` or `exclude_on` is rejected at load time.

For Continental Finance, `senior` is deliberately **not** excluded, because its current
tech opening is titled "Mid/Senior Software Developer". The filter excludes only
management and executive titles.

### Adding an ATS

1. Create `scrapers/myats.py` with `class MyATSScraper(BaseScraper)`, setting `name` and
   `domains`, and implement `fetch_jobs()`. If the listing lacks a field that filters
   use (location, employment type), also implement `missing_fields()` and
   `fetch_details()`. The detail page is then fetched only for companies whose filter
   needs that field.
2. Add the class to `SCRAPERS` in `scrapers/__init__.py`.

### Backends: state, config and secrets

The `runtime:` section of `companies.yaml` picks where things live, and environment
variables override each key. That way one codebase serves the laptop, Lambda and tests.

| Key | Env var | Values |
|---|---|---|
| `storage` | `ROLE_RADAR_STORAGE` | `json` (default): `state_file` on disk. `dynamodb`: the shared table |
| `state_file` | `ROLE_RADAR_STATE_FILE` | JSON state path (default `seen_jobs.json`) |
| `table` | `ROLE_RADAR_TABLE` | DynamoDB table name |
| `config_url` | `ROLE_RADAR_CONFIG_URL` | `s3://bucket/key`. Companies and settings are read from there; the local file then only supplies `runtime:` |
| `secrets` | `ROLE_RADAR_SECRETS` | `env` (default): environment variables. `ssm:/role-radar/`: SSM Parameter Store, fetched the first time an alert is sent |
| `region`, `profile` | `AWS_REGION`, `AWS_PROFILE` | Which AWS region and `~/.aws` profile to use |

The AWS backends need boto3: `pip install '.[aws]'`.

**DynamoDB layout** (one table, `pk` + `sk`):

| pk | sk | Item |
|---|---|---|
| company name | job uid | A seen job (the fields of `SeenJob`) |
| `#schedule` | company name | `last_checked_at`, `next_check_at`, failure count |
| `#lease` | `#lease` | Who may check companies now: `holder`, `epoch`, `expires_at` |
| `#alerts` | time + company + uid | Log of sent alerts, which expires after 30 days via TTL |
| `#runs` | runner | Each runner's last pass |

Reads are strongly consistent. Right after a handoff, the new runner must see everything
the previous one wrote.

**The lease and fencing.** Only the lease holder checks companies. It takes the lease
with a conditional write that succeeds only if the lease has expired or is already its
own, and every successful take bumps `epoch`. Each company is saved in one transaction
together with a check that the lease still has *this runner's* epoch. So a runner that
was paused past its expiry can't overwrite the work of whoever took over, even before it
notices; a laptop that slept mid-run is the usual case. Before sending alerts, a runner
also re-reads the lease. A runner that finds it has lost the lease stops without saving
or sending anything more, and its unfinished companies are still due for the new holder.
Expiry uses wall-clock time, because on macOS the monotonic clock stops while the laptop
sleeps.

To write another backend, subclass `storage.StateStore`. Runs use `load_schedule()`
(every company's next check time), then `load_company()` and `save_company()` around
each company's check. `load()` and `save()` move a whole state at once, for migration.

## Commands

Install the `role-radar` command. [pipx](https://pipx.pypa.io) keeps it in its own
environment:

```bash
pipx install '.[aws]'                 # from the project directory; drop [aws] for JSON-only use
```

| Command | What it does |
|---|---|
| `role-radar start` | Runs until you quit (Ctrl+C). Takes the lease and checks companies as they come due. If Lambda holds the lease, it asks Lambda to hand over and takes over once it has. |
| `role-radar stop` | Asks a running `start` (for example the login item) to finish the companies in flight, release the lease and quit. |
| `role-radar status` | Shows who holds the lease, each runner's last pass, which companies are due or failing, the latest alerts, and whether the pushed config matches your local file. |
| `role-radar run --once` | One pass over the due companies, then exits. Add `--all`, `--company NAME`, `--dry-run` (print alerts, save nothing, no lease) or `--baseline` (record everything as seen, no alerts). |
| `role-radar list-matches` | Prints every job matching right now. Reads no state, sends nothing. |
| `role-radar config push` | Validates your local `companies.yaml` and uploads it to `runtime.config_url`. |
| `role-radar migrate --from json:seen_jobs.json --to dynamodb:TABLE` | Copies state between stores (either direction). |
| `role-radar login-item on\|off` | Starts `role-radar start` whenever you log in to your Mac. It's a launchd agent with RunAtLoad and no KeepAlive, so quitting it stays quit until your next login. It logs to `~/Library/Logs/role-radar.log`. |

Every command takes `--config PATH` and `-v`. Without `--config`, the local companies
file is `$ROLE_RADAR_CONFIG_FILE`, else `./config/companies.yaml`, else
`~/.config/role-radar/companies.yaml`.

On Ctrl+C, SIGTERM or SIGHUP, `start` stops starting companies, lets the ones in flight
finish and save, and releases the lease. A second Ctrl+C quits at once; the lease then
expires within 3 minutes. Only one `start` runs per machine: a second one sees the
first and exits.

Exit codes: `0` ok · `1` every company failed, or an alert couldn't be delivered ·
`2` bad config or usage · `3` the lease was lost mid-pass · `4` another runner holds
the lease.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[aws]' -r requirements-dev.txt

python -m pytest                                  # tests
role-radar list-matches                           # every job matching now (no state, no alerts)
role-radar run --once --dry-run -v                # check due companies, print alerts, save nothing
role-radar run --once --all --dry-run             # the same for every company, due or not
role-radar run --once --company "Continental Finance" --dry-run

cp .env.example .env                              # fill in a Discord webhook and/or SMTP settings
set -a; source .env; set +a
role-radar run --once                             # real pass over the due companies
role-radar run --once --baseline                  # mark everything current as seen, no alerts
```

If no notification settings are found, alerts go to stdout. With the default
`runtime:` (JSON storage, secrets from the environment), no AWS account is needed.

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

## Scaling to 1,000 companies

- The defaults allow 16 requests in flight overall, 40 companies in flight, and one
  request per second per host. The shared ATS APIs are exceptions:
  `boards-api.greenhouse.io` gets 0.25 s, and `api.lever.co` / `api.ashbyhq.com` get
  0.3 s. Override or add hosts under `settings.http.host_delays`; a key also covers its
  subdomains. Most boards take one request, so 1,000 companies take a few minutes.
- Detail requests are made only when a filter needs a field the listing lacks, only for
  unseen jobs that could still match, and at most `max_detail_requests` per company per
  run. Any left over are picked up next run.
- For very large Workday tenants, set `options.search_text` and `options.max_jobs`.
- When adding many companies at once, set `notify_on_first_run: false`, or run once with
  `--baseline`.
