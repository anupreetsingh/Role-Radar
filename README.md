# Role Radar

Watches company careers pages and collects new jobs matching your criteria into a digest
every 30 minutes. It's built for about 1,000 companies, each checked every 30 minutes. Your Mac does
the work while `role-radar start` is running, and an AWS Lambda takes over whenever it isn't.

## How it works

### The handoff

```
you run `role-radar start` on your Mac
  → it takes the lease (one item in DynamoDB) and renews it every minute
  → it checks companies as they come due, saving each one as it finishes
you quit it, close the lid, or lose the network
  → quitting releases the lease; otherwise it expires within 3 minutes
  → Lambda runs every 5 minutes: it finds the lease free, takes it, and checks
    whatever is due, for up to 10 minutes per run
you run `role-radar start` again
  → it asks Lambda to hand over; Lambda finishes the companies in flight and releases
  → the laptop takes the lease within about 15 seconds and carries on
```

Both sides share the same state and config, so each simply continues where the other
stopped:

| Piece | Lives in | Holds |
|---|---|---|
| State | DynamoDB, one table | Seen jobs, check and digest schedules, delivery receipts, the lease, an alert log |
| Companies config | S3 (`role-radar config push` uploads your local `companies.yaml`) | Companies, filters, settings |
| Alert channel secrets | SSM Parameter Store (SecureString) | Discord webhook, SMTP settings |
| Laptop runner | `role-radar start` (optionally started at login) | Works whenever it's running |
| Lambda runner | [deploy/aws/template.yaml](deploy/aws/template.yaml), every 5 minutes | Works whenever the laptop isn't |

### One pass

```
load config + every company's schedule → the companies that are due (schedule.py)
  for each due company (concurrent, bounded):
    scraper (auto-picked from URL) → public JSON API, or HTML fallback
    → detail page only if the filter needs a field the listing lacks (Workday, custom sites)
    → filter (filters.py)
    → diff vs. stored state (tracker.py): new / removed / repost / duplicate
    → queue the new matches for the next digest
    → save its jobs and next check time, in one transaction fenced on the lease
  if the digest is due (even when no company was due):
    collect pending matches across enabled companies → re-read the lease
    → one digest per channel → save each channel's delivery receipts
```

All HTTP goes through `http_client.py`. Each request checks robots.txt, waits until its
host's delay has passed since the previous request to that host, then takes one of the
global concurrency slots, and retries with exponential backoff on timeouts, 429 and 5xx
(it honours `Retry-After`). The host wait comes first, so requests queued behind a busy
shared host such as `boards-api.greenhouse.io` don't hold slots that other hosts could
use. Parsed robots.txt files are kept for 12 hours and matched per RFC 9309 (the most
specific rule wins; `*` and `$` wildcards), not urllib's first-match parser, which
blocked Eightfold's explicitly allowed APIs and ignored Google's paging rules. If one
company fails, that failure is recorded and the other companies still run.

Each pass logs how long it took, the number of requests and bytes downloaded
(compressed, as received), and the busiest hosts. Run with `-v` to see every host, and
why each job matched or not.

| Module | Role |
|---|---|
| `role_radar/cli.py` | The `role-radar` command |
| `role_radar/runner.py` | Holds the lease and runs passes: the laptop loop, `run --once`, Lambda |
| `role_radar/monitor.py` | One pass: due companies → scrape → filter → queue → save, then any due digest |
| `role_radar/digest.py` | Shared digest cadence, queued matches across companies, and delivery retries |
| `role_radar/schedule.py` | Which companies are due, and when each is next checked |
| `role_radar/lease.py` | The lease that decides which runner may work, and fencing |
| `role_radar/dynamo.py` | DynamoDB state store and lease (one table) |
| `role_radar/storage.py` | `StateStore` interface + JSON and in-memory implementations |
| `role_radar/config.py` | Loads and validates `companies.yaml` (JSON also accepted), including `runtime:` |
| `role_radar/backends.py` | Picks the store, lease, config source and secrets from `runtime:` |
| `role_radar/aws.py` | boto3 helpers: the config file in S3, secrets in SSM |
| `role_radar/models.py` | `JobPosting` dataclass, stable `uid` and `fingerprint` |
| `role_radar/scrapers/` | One class per ATS plus `generic.py`; registry in `scrapers/__init__.py` |
| `role_radar/filters.py` | Keyword, location and employment-type rules |
| `role_radar/tracker.py` | New-job, removal, repost and duplicate detection |
| `role_radar/notifications.py` | `Notifier` interface + Discord, email and console |
| `role_radar/instance.py`, `launchd.py` | One `start` per machine; the macOS login item |
| `lambda_handler.py` | The Lambda entry point |

### The lease: never duplicate, never skip

Only the lease holder checks companies. A runner takes the lease with a conditional write
that succeeds only if the lease has expired or is already its own, so two runners racing
for it can't both win. Every successful take bumps the lease's `epoch`, and the epoch is
the fencing token.

- **Saves are fenced.** Each company is saved in one DynamoDB transaction together with a
  check that the lease still has *this runner's* epoch. A runner that was paused past its
  expiry therefore can't overwrite the work of whoever took over, even before it notices;
  a laptop that slept mid-run is the usual case.
- **Alerts are fenced.** Before sending, a runner re-reads the lease.
- **Nothing is skipped.** A company counts as checked only once its save has committed.
  A runner that loses the lease stops without saving or sending anything more, and its
  unfinished companies are still due for the new holder.
- Expiry uses wall-clock time, because on macOS the monotonic clock stops while the
  laptop sleeps.

| What happens | Result |
|---|---|
| You quit `start` (Ctrl+C, `role-radar stop`, logging out) | Companies in flight finish and are saved, the lease is released, and Lambda takes over at its next run (within 5 minutes). |
| You close the lid or lose the network | Renewals stop, so the lease expires within 3 minutes and Lambda takes over at its next run after that (within 8 minutes in all). |
| You start `start` while Lambda is working | It asks Lambda to hand over. Lambda stops starting companies within about 10 seconds, finishes the ones in flight and releases. The laptop takes over at its next try (every 15 seconds). |
| The laptop wakes up after Lambda took over | Its unfinished work fails the fence, so nothing is saved or sent. It waits and takes the lease back when Lambda releases it. |
| A Lambda run crashes or times out | Its lease expires a minute after its time limit, and the next runner takes over. |

[tests/test_handoff.py](tests/test_handoff.py) plays each of these out against fake AWS
(moto), including a laptop whose clock runs slow, and runners racing for the lease.

**If a save fails after the alerts went out** (DynamoDB throttling that outlasts its
retries, say), the runner retries the save for about a minute and a half. If it still
fails, the runner remembers what it sent, so a later check of that company doesn't send
it again, and it leaves the company alone for 2 minutes.

**Known exception.** If a runner sends an alert and then crashes, is killed, or loses the
lease before that company's save commits, whoever checks the company next sends the
alert again. The previous version behaved the same way.

### Supported sources

| ATS | Endpoint used | Detail request? |
|---|---|---|
| BambooHR | `{sub}.bamboohr.com/careers/list` | no |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` | no |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | no |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` | no |
| Workday | `POST {host}/wday/cxs/{tenant}/{site}/jobs` (paginated) | only if the filter uses employment type, or uses location and the job is listed as "N Locations" |
| MathWorks | Official RSS job feed, including EDG | no |
| HRM Direct / ClearCompany | Public search-results table, including malformed job links | no |
| iCIMS | Public job cards across every visible results page | no |
| Jibe | Branded career site's public `/api/jobs` endpoint, paginated (e.g. Garmin) | no |
| Avature | Public `SearchJobs` result cards, paginated (e.g. Bloomberg) | no |
| COMSOL | Job links grouped under location headings | no |
| Recruiterbox / Trakstar | Public widget API (e.g. Wolfram) | no |
| Eightfold | The site's `/api/pcsx/search` (Microsoft, Qualcomm, PayPal...) or `/api/apply/v2/jobs` (Netflix), newest first | no |
| Oracle Cloud HCM | `{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions`, newest first (JPMorgan Chase, Oracle, TI...) | no |
| amazon.jobs | `search.json`, newest first, by category | no |
| Apple | Search pages' hydration JSON, newest first | no |
| Google | First results page of each query (robots.txt disallows paging), newest first | no |
| Meta | `/jobsearch/sitemap.xml` for the job IDs | each new job's page once, for its title and location (up to 2 minutes of pages a check) |
| TikTok | Public search API; the listing is filtered to US jobs locally | no |
| Custom | Embedded-ATS detection → JSON-LD `JobPosting` → link heuristic | only if the filter needs a location or employment type the listing lacks (read from the job page's JSON-LD) |

**Job descriptions are not stored or used for matching.** A job's title, location, ID
and URL are enough to alert on. Some feeds and APIs include descriptions in their
listing responses; these are discarded. Detail pages are fetched only when listing
metadata needed by a filter is missing.

The ATS is detected from the URL, or you can set it with `ats:`. Nothing tries to get
past CAPTCHAs, logins or robots.txt, and every request sends an honest User-Agent.
There is no browser automation. Branded sites can still be monitored through their
public feeds/APIs or supported HTML readers; a JavaScript landing page alone is not
evidence that the source works. See [company coverage](docs/company-coverage.md) for
the enabled employers, source limitations and expansion queue.

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
| Notification failed | Failed channels retry at the next digest (or company check if digests are disabled); successful channels are remembered and skipped |
| Filters broadened | Jobs that already exist and now match alert once |

Removed jobs are pruned from state after `retention_days`.

Delivery receipts are stored per channel in `notified_channels`; `notified_at` is set
after every configured channel succeeds. Partial failures count as undelivered alerts
and trigger the Lambda error alarm. Older state files still load, and previously
completed alerts are not replayed when another channel is added.

### Scheduling

Companies aren't all checked at once. Each one is checked when it's **due**: when
`check_interval_minutes` (30; Workday companies 240, via `check_interval_by_ats`) have
passed since its last check, or straight away if it has never been checked. Each company's state is saved as soon as that company finishes,
so an interrupted pass loses at most the companies still in flight. Those companies are
still due and get picked up next time.

- A check that ran more than half an interval late (a company's first check, or catching
  up after an outage) gets a random extra 0–30 minutes before its next check. Otherwise
  a burst of catch-up checks would come due together every half hour forever. With it,
  the checks spread out across the half hour after one round.
- A failing company is still rescheduled, so it isn't retried on every pass. From the
  third failure in a row its interval doubles each time, up to every 4 hours. One
  success resets it.
- `--all` checks every company whether it's due or not, and so do `--company` and
  `--baseline`.

#### Notification digests

`settings.digest_interval_minutes: 30` collects all new matching jobs across enabled
companies into one email and one Discord message, grouped by company with application
links. If the list exceeds Discord's message limit, the single message includes the
complete list as a text attachment. Empty intervals send nothing.

The schedule targets `:00` and `:30`. Lambda sends on its first run after the boundary,
after any checks in that run finish; its five-minute trigger can add a short delay.
The laptop uses the same schedule. Pending jobs, the next send time and delivery
receipts persist across restarts and handoffs. Failed channels retry at the next
interval without repeating delivery to successful channels. Previously sent jobs
are not replayed when digests are enabled.

Set the interval to `0` (also the default when omitted) to send immediately after
each company's check. Baselines and dry runs never send a digest.

## Configuration

Edit [config/companies.yaml](config/companies.yaml), then `role-radar config push` it once
the AWS side is set up. Keys under `defaults.filters` apply to every company, and a
company's own `filters` replace them **one key at a time**.

```yaml
- name: Continental Finance
  url: https://contfinco.bamboohr.com/careers
  filters:
    include_keywords: [software engineer, developer, machine learning, AI, data engineer]
    match_on: [title]            # title | location | employment_type | department
    locations: [Remote, Wilmington]   # optional
    employment_types: [full-time]     # optional
```

Matching is case-insensitive and respects word boundaries, so `AI` does not match
"Maintain". A space in a keyword also matches `-`, `/` and `_`, and `re:` lets you use a
regex. To change the logic itself, edit `JobFilter.evaluate` in `filters.py`. To make a
new field matchable, add it to `FIELD_GETTERS`. Descriptions can't be matched, and a
config that puts `description` in `match_on` or `exclude_on` is rejected at load time.

The default role list covers application development (including full stack, frontend
and backend), platforms/cloud, compilers/systems/embedded software, AI/research/data,
security, testing, product/technical delivery, and customer-facing engineering.
It targets potential opportunities for a spring-2026 MS CS graduate; matching a title
does not establish eligibility. Review the posting's experience, specialized skills,
degree, graduation window and work authorization requirements separately. Research
scientist and product/program roles in particular need this review. Graduation years
and "new grad" are not required in titles, so unlabelled early-career openings can match.

The defaults exclude senior and leadership titles, while permitting Product Manager,
Technical Program Manager and Technical Project Manager. "Member of Technical Staff"
is also allowed. Continental Finance inherits the expanded role list and retains its
"Mid/Senior Software Developer" exception for review, but now excludes purely senior
roles and the same leadership titles as the defaults. The location rules target the
US, Canada, Australia and India, including common region/city formats. Unqualified
Remote/Worldwide listings are retained for eligibility review; other remote regions
are not automatically included. Location-text matching is approximate and does not
establish work authorization. Employment type is unrestricted. Program names such as
Engineering Development Group (EDG) are included even without an engineer/developer
title.

To verify the enabled sources without sending alerts or changing job state, run
`python -m scripts.validate_companies --output docs/company-validation.json`.
The report records the configuration hash, check time, listing/match counts, partial
snapshots and any pending detail checks. Use `--company NAME` to check one source;
omit `--output` for an exploratory check that should not replace the full report.

A company's name is its key in the state store, so renaming a company re-baselines it.
Names can't start with `#`, because the table uses that prefix for its own rows.

### Adding an ATS

1. Create `role_radar/scrapers/myats.py` with `class MyATSScraper(BaseScraper)`, setting
   `name` and `domains`, and implement `fetch_jobs()`. If the listing lacks a field that
   filters use (location, employment type), also implement `missing_fields()` and
   `fetch_details()`. The detail page is then fetched only for companies whose filter
   needs that field.
2. Add the class to `SCRAPERS` in `role_radar/scrapers/__init__.py`.

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

The AWS backends need boto3: `pip install '.[aws]'`. The JSON store stays for tests,
dry runs and AWS-free local use.

**DynamoDB layout** (one table, `pk` + `sk`):

| pk | sk | Item |
|---|---|---|
| company name | job uid | A seen job (the fields of `SeenJob`) |
| `#schedule` | company name | `last_checked_at`, `next_check_at`, failure count |
| `#digest` | `#digest` | `next_send_at`, `last_attempt_at`, `interval_minutes` |
| `#lease` | `#lease` | Who may check companies now: `holder`, `epoch`, `expires_at` |
| `#alerts` | time + company + uid | Log of sent alerts, which expires after 30 days via TTL |
| `#runs` | runner | Each runner's last pass |

Reads are strongly consistent. Right after a handoff, the new runner must see everything
the previous one wrote. Each save writes only the rows that changed.

To write another backend, subclass `storage.StateStore`. Runs use `load_schedule()`
(every company's next check time), then `load_company()` and `save_company()` around
each company's check. `load_digest()` and `save_digest()` persist the shared digest
schedule; its writes must use the same lease fence as company saves. `load()` and
`save()` move a whole state at once, for migration.

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
| `role-radar doctor` | Checks setup without sending alerts or changing job state. Add `--stack role-radar --profile admin --region us-east-1` to inspect the deployed Lambda and EventBridge schedule. |
| `role-radar notifications test` | Sends a labeled test through the configured channels without changing job state. Add `--channel email` or `--channel discord` to test one. |
| `role-radar run --once` | One pass over the due companies, then exits. Add `--all`, `--company NAME`, `--dry-run` (print alerts, save nothing, no lease), `--baseline` (record everything as seen, no alerts) or `--local-config` (read the local file instead of the pushed copy). |
| `role-radar list-matches` | Prints every job matching right now. Reads no state, sends nothing. |
| `role-radar config push` | Validates your local `companies.yaml` and uploads it to `runtime.config_url`. |
| `role-radar migrate --from json:seen_jobs.json --to dynamodb:TABLE` | Copies state between stores (either direction). |
| `role-radar login-item on\|off` | Starts `role-radar start` whenever you log in to your Mac. It's a launchd agent with RunAtLoad and no KeepAlive, so quitting it stays quit until your next login. It logs to `~/Library/Logs/role-radar.log`. It needs the alert secrets in SSM, because a login item can't see your shell's environment variables. |
| `role-radar switch laptop\|lambda on\|off` | Turns a runner on or off, independently; no arguments shows both. The switches live in the state store. A laptop switched off releases the lease (so Lambda covers, if it's on) and idles until switched back on, picking up the change within a minute; a pass in progress stops starting companies within 30 s. Lambda switched off exits at once on each run. With both off, nothing is checked. `status` shows the switches. |
| `role-radar ui` | Opens a local page (127.0.0.1:8765) with the same two switches, the lease holder and each runner's last pass. `--port`, `--no-browser`. |
| `scripts/build_menubar.sh` | Builds and opens **Role Radar.app**, a macOS menu bar app (in `~/Applications`) with the same two switches as native toggles, who's checking right now, and each runner's last pass. The menu bar icon shows a laptop while the Mac is checking, a cloud while Lambda is, and a crossed-out antenna when nothing is. Switching the Mac on also starts `role-radar start` through the login item (installing it if needed). Needs Xcode or the Command Line Tools. |

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

python -m pytest                                  # tests (AWS is faked with moto)
role-radar list-matches                           # every job matching now (no state, no alerts)
role-radar run --once --dry-run -v                # check due companies, print alerts, save nothing
role-radar run --once --all --dry-run             # the same for every company, due or not
role-radar run --once --company "Continental Finance" --dry-run

cp .env.example .env                              # fill in a Discord webhook and/or SMTP settings
set -a; source .env; set +a
role-radar run --once                             # real pass over the due companies
role-radar run --once --baseline                  # mark everything current as seen, no alerts
```

With `secrets: env` and no notification settings, alerts go to stdout. With the default
`runtime:` (JSON storage, secrets from the environment), no AWS account is needed. With
secrets in SSM, no settings means alerts stay pending: they're retried, and on Lambda the
error alarm fires, rather than alerts vanishing into a log.

## Deploy to AWS

You need:

- An AWS account on the **Paid plan**. Accounts created on the Free plan close 6 months
  after sign-up, or when their credits run out, unless they're upgraded. The always-free
  allowances this project relies on apply on both plans.
- The AWS CLI, the AWS SAM CLI and Python 3.13, for `sam build`:
  `brew install awscli aws-sam-cli python@3.13`. Alternatively, `sam build --use-container`
  works with Docker instead of a local Python 3.13.
- Admin credentials for deploying, for example `aws configure --profile admin`. The
  laptop gets its own restricted user in step 4.

**1. Deploy the stack.** The Lambda starts running every 5 minutes right away, but it
does nothing until you push a config in step 7. First check your Lambda concurrency
quota. Reserving 1 for
this function needs at least 11, and some new accounts start at 10:

```bash
aws lambda get-account-settings --profile admin --query AccountLimit.ConcurrentExecutions
```

If it says 10, either ask for more (Service Quotas → AWS Lambda → Concurrent
executions; it's free), or answer `ReservedConcurrency: 0` below. The lease already stops
overlapping runs from doing the same work. Then, from the project directory:

```bash
sam build -t deploy/aws/template.yaml
sam deploy --guided --profile admin      # stack name: role-radar; AlertEmail: you@example.com
```

Accept the defaults, and allow SAM to create IAM roles. Your answers are saved in
`samconfig.toml` (git-ignored), so later deploys are just `sam build ... && sam deploy`.
To change a parameter later, run `sam deploy --guided` again. AWS then emails you to
confirm the alarm subscription; click the link.

The stack creates:

- The DynamoDB table (provisioned 25 RCU / 25 WCU, TTL, point-in-time recovery,
  deletion protection).
- The config bucket (versioned, private).
- The Lambda (arm64, 256 MB, 14-minute timeout, reserved concurrency 1, outside any VPC,
  so no NAT gateway) and its 5-minute EventBridge schedule.
- A CloudWatch error alarm with an email subscription, and a $5/month AWS Budget alert.
- `LaptopPolicy`, an IAM policy for the laptop user.

The table and bucket are kept even if the stack is deleted.

**2. Put the alert secrets in SSM** under `/role-radar/`, using the environment variable
names from [.env.example](.env.example). `read -rs` keeps the value out of your shell
history:

```bash
read -rs HOOK   # paste the Discord webhook URL, then Enter
aws ssm put-parameter --profile admin --type SecureString --name /role-radar/DISCORD_WEBHOOK_URL --value "$HOOK"
# Email instead of, or as well as, Discord: SMTP_HOST, SMTP_PORT, SMTP_SECURITY,
# SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO, the same way.
```

**3. Point your local config at the stack.** Copy the stack outputs
(`sam list stack-outputs --stack-name role-radar --profile admin`) into the `runtime:`
section of `config/companies.yaml`:

```yaml
runtime:
  storage: dynamodb
  table: <TableName output>
  config_url: <ConfigUrl output>
  secrets: ssm:/role-radar/
  region: us-east-1               # the stack's region
  profile: role-radar             # the laptop user's ~/.aws profile, from step 4
```

**4. Create the laptop's IAM user.** It can reach only this table, the config object and
the secrets:

```bash
aws iam create-user --user-name role-radar-laptop --profile admin
aws iam attach-user-policy --user-name role-radar-laptop --policy-arn <LaptopPolicyArn output> --profile admin
aws iam create-access-key --user-name role-radar-laptop --profile admin
aws configure --profile role-radar    # paste the key pair; same region as the stack
```

**5. Install the CLI:** `pipx install '.[aws]'`

**6. Record what's already open, before Lambda starts.** Lambda has been running every
5 minutes since step 1, but it does nothing until a config has been pushed (step 7). So
do this first. Each company's first check records every job that's currently open;
pick one:

- Move existing state over, if you have any:
  `role-radar migrate --from json:seen_jobs.json --to dynamodb:<TableName>`.
- Record everything that's open now without alerting (recommended for many companies):
  `role-radar run --once --baseline --local-config`. `--local-config` reads your local
  file, since nothing has been pushed yet.
- Or skip this and get alerted about current matches too (`notify_on_first_run: true`,
  the default).

The first full write covers every job at every company. At 25 WCU that's throttled
(throttled requests are retried), so 1,000 companies can take an hour or more.
Switching the table to on-demand for the day avoids that (see
[Throttling](#throttling-and-on-demand-capacity)). Switch back afterwards. If
`run --once --baseline` reports companies that failed to save, run it again. Both
commands hold the lease while they work.

**7. Push the config and run it:**

```bash
role-radar config push         # Lambda starts checking from its next run
role-radar start               # in a terminal; Ctrl+C to quit (Lambda takes over)
role-radar login-item on       # or: start it at every login (needs secrets in SSM, step 2)
role-radar status              # who has the lease, last passes, due companies, alerts
```

To see a handoff, quit `start` and run `role-radar status` a few minutes later. The
lease shows `held by lambda:…`, and afterwards `free since …`. Start it again and the
laptop takes over.

## Costs

After the credits run out, everything here stays within AWS's always-free allowances,
except S3 requests (fractions of a cent) and point-in-time recovery (about $0.20 per
GB-month of a table that's a few MB). Expect **about $0.01–0.05 a month**. The $5 budget
emails you at 80% of actual spend, or if the month is forecast to exceed $5.

| Service | This project's use | Always-free allowance (checked 2026-09-26) |
|---|---|---|
| Lambda (arm64, 256 MB) | 8,640 runs/month. When the laptop is off, about 10–60 s each: 20k–130k GB-s. Since Aug 1, 2025, cold-start INIT time is billed as duration too; it counts against the same allowance. | 1M requests + 400,000 GB-s per month ([pricing](https://aws.amazon.com/lambda/pricing/), [INIT billing](https://aws.amazon.com/blogs/compute/aws-lambda-standardizes-billing-for-init-phase/)) |
| DynamoDB (provisioned) | 25 RCU / 25 WCU, a few MB. A check costs about 4 WCU (transactional writes cost 2 per item) and 6 RCU | 25 WCU, 25 RCU, 25 GB per region ([pricing](https://aws.amazon.com/dynamodb/pricing/provisioned/), [transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)) |
| EventBridge Scheduler | 8,640 invocations/month | 14M per month ([pricing](https://aws.amazon.com/eventbridge/pricing/)) |
| CloudWatch | Under 1 GB of logs/month (kept 14 days), 1 alarm | 5 GB of logs, 10 alarms ([pricing](https://aws.amazon.com/cloudwatch/pricing/)) |
| SNS | A few alarm emails | 1,000 emails per month ([pricing](https://aws.amazon.com/sns/pricing/)) |
| AWS Budgets | 1 budget with email alerts | Monitoring and notifications are free ([pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/)) |
| SSM Parameter Store | A handful of standard SecureString parameters | Standard parameters and standard throughput are free ([pricing](https://aws.amazon.com/systems-manager/pricing/)) |
| S3 | One small file, about 17k GETs/month | None for new accounts: $0.0004 per 1,000 GETs ([pricing](https://aws.amazon.com/s3/pricing/)) |

The whole free tier comes with 30+ services on both the Free and Paid plans
([AWS Free Tier](https://aws.amazon.com/free/)). The table's 25/25 is shared with any
other provisioned tables in the same account and region.

### Throttling and on-demand capacity

Provisioned capacity is what keeps DynamoDB free. At 1,000 companies it averages about
3 WCU and 4 RCU per second, with bursts. DynamoDB banks up to 5 minutes of unused
capacity for bursts ([burst capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/burst-adaptive-capacity.html)),
and throttled requests are retried with backoff. If passes still slow down, or
the log shows saves failing, check the table's `WriteThrottleEvents` /
`ReadThrottleEvents` metrics in CloudWatch. If they're persistent, switch to on-demand:

```bash
sam deploy --guided --profile admin   # answer BillingMode: PAY_PER_REQUEST (and later back to PROVISIONED)
```

On-demand never throttles, but at this workload it costs about **$5 a month**
($0.625 per million writes, $0.125 per million reads in us-east-1). DynamoDB allows
switching to on-demand up to four times per 24 hours, and back to provisioned at any time
([switching](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-switching-capacity-modes.html)).

## Operating it

### Check automation and delivery

From a checkout, use `.venv/bin/python -m role_radar` in place of `role-radar` if the
command has not been installed. Start with:

```bash
role-radar doctor
# After authenticating to AWS, use your stack's actual profile and region:
role-radar doctor --stack role-radar --profile admin --region us-east-1
```

The stack inspection needs a profile with read access to CloudFormation, Lambda and
EventBridge Scheduler, in addition to the app's state, config and secrets. The laptop's
restricted policy alone does not include these deployment inspection permissions.
The command reports missing credentials, a disabled schedule, blocked Lambda
concurrency, stale run history, an unpushed config, and missing or incomplete channels.
It never sends an alert or prints secret values. Local JSON mode alone does not enable
Lambda; follow [Deploy to AWS](#deploy-to-aws) to connect the shared backends.

Once channel settings are populated in the configured source, verify delivery:

```bash
role-radar notifications test --channel discord
role-radar notifications test --channel email
role-radar status
```

These tests send a `[TEST]` notification to the configured recipients. They do not
mark real jobs as seen. `AlertEmail` in the SAM deployment only receives AWS error and
budget emails; job-alert email separately needs `SMTP_HOST`, `EMAIL_TO`, and the SMTP
provider's authentication settings. With `runtime.secrets: ssm:/role-radar/`, those
settings must be in SSM; values in a local `.env` file are not used by Lambda. For local
`secrets: env` runs, load `.env` into your shell as shown in the local setup instructions.

### Routine maintenance

- **Logs.** The laptop logs to the terminal, or to `~/Library/Logs/role-radar.log` for
  the login item (rotated at 5 MB). Lambda logs to CloudWatch:
  `aws logs tail /aws/lambda/role-radar-monitor --follow --profile admin`.
- **Error alarm.** It emails you when a Lambda run fails: alerts that couldn't be
  delivered, every company failing, a bad config, or missing permissions. Single broken
  sites don't trip it; `role-radar status` lists them.
- **Changing companies or filters.** Edit `config/companies.yaml`, then
  `role-radar config push`. A running laptop app picks it up within 5 minutes, and Lambda
  at its next run. `status` warns when your local file and the pushed copy differ.
- **Rotating a secret.** `aws ssm put-parameter --overwrite ...`. Both sides re-read the
  secrets on the next alert after a failed delivery, when they start fresh, and after
  five minutes of cached settings. This also picks up newly configured channels.
- **Updating the code.** `sam build -t deploy/aws/template.yaml && sam deploy` for
  Lambda, and `pipx install --force '.[aws]'` then restart `start` for the laptop.
- **Tearing down.** `sam delete --stack-name role-radar` removes the Lambda, schedule,
  alarm, budget and policy. It keeps the table and bucket; delete those by hand
  (turn off the table's deletion protection first) if you really want the state gone.

## Scaling to 1,000 companies

- The defaults allow 16 requests in flight overall, 40 companies in flight, and one
  request per second per host. The shared ATS APIs are exceptions:
  `boards-api.greenhouse.io` gets 0.25 s, and `api.lever.co` / `api.ashbyhq.com` get
  0.3 s. Override or add hosts under `settings.http.host_delays`; a key also covers its
  subdomains, and subdomains under a parent-domain key share one rate. An HTTP 429
  pauses every host sharing that rate for the Retry-After time (or 60 s). Most boards
  take one request, so a full round of 1,000 companies takes a few minutes, and the
  schedule spreads that round over the half hour.
- Workday is the exception: one request per 20 jobs, and every company's tenant sits
  on the same service, which answers bursts from one IP with HTTP 429. The config
  spaces all `myworkdayjobs.com` tenants as one host (0.5 s), checks at most two
  Workday companies at a time (`settings.company_concurrency_by_ats`), and checks
  them every four hours (`settings.check_interval_by_ats`). With ~150 Workday
  companies that is about 4,500 requests per round, under 0.5 requests/s on average.
- Detail requests are made only when a filter needs a field the listing lacks, only for
  unseen jobs that could still match, and at most `max_detail_requests` per company per
  check. Any left over are fetched at the next check.
- For very large Workday tenants, set `options.max_jobs` and narrow the listing with
  `options.applied_facets` (e.g. the US country ID `bc33aa3152ec42d4995f4791a106ed09`
  under the board's country facet, or its technology job families) or
  `options.search_text`. Some tenants report at most 2,000 jobs, and not every board
  lists newest first, so keep a faceted board under 2,000.
- When adding many companies at once, set `notify_on_first_run: false`, or run once with
  `--baseline`.

## Security

- Secrets never live in the repo or the config. They sit in SSM (or your local `.env`),
  and are read only when an alert is sent.
- The code never logs webhook URLs or credentials. `httpx` logs every request URL, so its
  logging is kept at WARNING. The same goes for botocore, whose debug output would
  include decrypted SSM values.
- Discord messages set `allowed_mentions: none`, so a job title containing `@everyone`
  can't ping your server.
- The Lambda's role and the laptop's policy can only reach this table, the config object
  and the `/role-radar/` parameters.
