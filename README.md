# Role Radar

Watches company careers pages and collects new jobs matching your criteria into a digest
every 30 minutes. It's built for about 1,000 companies, each checked every 30 minutes. Your Mac does
the work while `role-radar start` is running, and an AWS Lambda takes over whenever it isn't.

There are three ways to run it:

- **Download the app.** Nothing to install: a Setup window asks your profession (Tech,
  Accounting & Finance or Healthcare), the job titles and countries you want, and sends alerts
  from your Gmail. See [Get the app](#get-the-app).
- **On your Mac only, from the code.** Everything, from the jobs it has seen to your email
  settings, stays on your Mac, and it checks while the menu bar app is open. No AWS account
  needed. See [Run it on your Mac](#run-it-on-your-mac).
- **Your Mac plus AWS.** A Lambda takes over whenever your Mac is closed or asleep, with
  state in DynamoDB; it fits in AWS's free tier. See
  [Add AWS](#add-aws-keep-checking-while-your-mac-is-off).

## Get the app

For a Mac with Apple Silicon (M1 or newer) and macOS 14 Sonoma or later:

1. Download `Role-Radar-<version>-apple-silicon.dmg` from the
   [Releases page](https://github.com/anupreetsingh/Role-Radar/releases/latest), open it, and
   drag **Role Radar** onto **Applications** in the window that opens. (Opened from anywhere
   else, the app offers to move itself there: it has to be in Applications to keep checking and
   to update itself.)
2. Open Role Radar. The app isn't signed with a paid Apple developer account, so macOS first
   says it can't check it: click **Done**, then open **System Settings → Privacy & Security**,
   scroll down, click **Open Anyway** next to Role Radar, and confirm. You only do this once.
3. Its window walks through Setup's pages:
   - **Profession:** Tech, Accounting & Finance or Healthcare. Each comes with its own list of
     companies, built into the app and refreshed with each version (Tech's has about 7,800,
     Accounting & Finance's about 3,900, Healthcare's about 1,800).
     Changing it later deletes nothing: the other profession's saved jobs stay, and every
     company's next check starts quietly, recording the jobs already open instead of alerting
     on them all (`settings.fresh_start_at` in `profile.yaml` is when you switched).
   - **Countries** and **Companies:** the countries you want (United States, Canada, Australia,
     India), and optionally cities. Only companies that post jobs in those countries are
     checked, and jobs alert from anywhere in them, or only from your cities. On the Companies
     page, **Find a company** searches the list: untick one to stop tracking it (with nothing
     typed, it lists those turned off). **Add a company you want** takes a name, and a careers
     page if known: one listed already says so (turned back on if it was off); otherwise it's
     tracked if Role Radar can read the page (read once to check), and either way it goes to the
     maintainer's suggestions box for a later version (see below).
   - **Roles:** target roles, the job titles to look for, and non-target roles, words that rule
     a title out (Senior, Lead, Staff, Director...). Both are boxes grouped by kind, all ticked
     to start with: untick any you don't want, or add your own.
   - **Qualifications:** skip jobs asking for a number of years or more ("3+" still alerts for
     no experience, 1+ and 2+), and your highest degree. Jobs that need a higher degree are
     skipped. A degree can also count in place of experience: if you skip 3+ years and have a
     Master's, a job asking for "3 years, or 1 year with a Master's" is still shown.
   - **Alerts (optional):** new jobs always collect in **Live Tracking**, newest on top, so
     you can just open the app to see them. To also get them sent every 10 minutes, set up
     email, Discord or both. Email needs your Gmail address and a Gmail
     [app password](https://myaccount.google.com/apppasswords), a 16-letter password just for
     Role Radar (it needs 2-Step Verification); alerts are sent from your Gmail to yourself,
     and to anyone you add (a friend, your school email), and each person sees only your
     address. Discord needs a channel's webhook URL (the channel's settings → Integrations →
     Webhooks). Both are kept in your Mac's Keychain.
4. **Start Checking.** The window turns into **Live Tracking**. Role Radar opens at login and
   checks while your Mac is on. New matches collect in Live Tracking, and go out every 10
   minutes if alerts are set up. **Edit Setup** there (or **Edit Setup…** in its menu) turns
   the window back into Setup's pages, to change the profession, roles, places or alerts.

Its files live in `~/Library/Application Support/Role Radar`, and its log is
`~/Library/Logs/com.roleradar.app.checker.log`.

**Updates.** The app updates itself with [Sparkle](https://sparkle-project.org): every six
hours (or with **Check for Updates…** in its menu) it reads the latest GitHub release's
`appcast.xml`, and offers a newer version if there is one. It installs an update only if it's
signed with the update key, so a copy from the first release on never needs downloading again.
Its settings, history and Keychain items live outside the app, so an update keeps them.

**Publishing a new version** (from the code, on an Apple Silicon Mac with uv and Xcode): bump
`__version__` in `role_radar/__init__.py` (every copy compares it with its own), then

```bash
sh scripts/package_app.sh                 # → dist/Role-Radar-<version>-apple-silicon.dmg
sh scripts/publish_update.sh NOTES.md     # signs it, writes appcast.xml, creates GitHub release v<version>
```

Publish as a normal release, not a pre-release: every copy reads the latest release. The update
key is made once on the Mac that publishes (`build/sparkle/<version>/bin/generate_keys --account
role-radar`, after `sh scripts/get_sparkle.sh`); its public half is `update_key` in
`scripts/package_app.sh`, and the private half stays in that Mac's Keychain. Keep a copy
somewhere safe (`generate_keys --account role-radar -x FILE`, then into a password manager):
without it, no copy of the app can take an update again, and everyone reinstalls.

**Suggestions box.** The Companies page's requests go to a Lambda function URL that keeps them in
a DynamoDB table, the same company once with how many asked (`deploy/suggestions`, its own stack,
on demand, so it costs nothing at this size). Set it up once with the admin login, then put its
`SuggestionsUrl` output in `role_radar/suggest.py` (`ENDPOINT`):

```bash
sam build -t deploy/suggestions/template.yaml && sam deploy --stack-name role-radar-suggestions \
    --resolve-s3 --capabilities CAPABILITY_IAM --profile role-radar-admin --region us-east-1
AWS_PROFILE=role-radar-admin role-radar suggestions            # what's waiting, most asked for first
AWS_PROFILE=role-radar-admin role-radar suggestions done ID    # dealt with: added in a version, or not
```

A request that can't be sent (offline, or before `ENDPOINT` is set) waits in `suggestions.jsonl`
beside the app's companies file and goes with the next one.

The script puts a Python (python-build-standalone, via uv) and Role Radar inside the app,
with the companies in `config/companies.yaml` as its directory of known employers and the icon in
`macos/AppIcon.icon` (open it in Xcode's Icon Composer to change it), and Sparkle (fetched once by
`scripts/get_sparkle.sh`), and signs it ad hoc. The packaged app runs its checker as its own launchd agent,
`com.roleradar.app.checker`, with its own files and Keychain items, so it never touches one
run from the code. To try a build as someone new would, `KEEP_APP=dist sh scripts/package_app.sh`
keeps a copy of the app in `dist/`, and `sh scripts/reset_app.sh` wipes the packaged app's
setup so its next launch is a first run.

## Run it on your Mac

Two files in `config/` decide what Role Radar does. Edit both as you like:

| File | In git | Holds |
|---|---|---|
| `companies.yaml` | yes | The companies to watch (about 7,800 to start from) and how often to check them |
| `accounting.yaml`, `healthcare.yaml` | yes | The Accounting & Finance and Healthcare professions' company lists, for the packaged app (built by `scripts/build_lists.py`) |
| `profile.yaml` | no | You: the roles and places you want, the most years of experience a job may ask for, and where state and alert settings live. [profile.example.yaml](config/profile.example.yaml) shows every setting |

**1. Get the code and install it** (Python 3.10 or newer). Keep it outside Desktop,
Documents and Downloads: macOS asks for permission to those folders again every time the
menu bar app is rebuilt, and holds the checker until you answer.

```bash
git clone https://github.com/<you>/Role-Radar.git ~/Projects/Role-Radar   # your fork, or this repo
cd ~/Projects/Role-Radar
python3 -m venv .venv && .venv/bin/pip install -e .
```

**2. Make your profile.**

```bash
cp config/profile.example.yaml config/profile.yaml
```

Change its `filters` to what you're looking for: `include_keywords` (a job title must
contain one), `exclude_keywords` (it mustn't contain any), `locations`, and
`max_experience_years` (see [Experience filter](#experience-filter)). Its `runtime:`
section, `storage: sqlite` and `secrets: keychain`, keeps everything on this Mac: the jobs
it has seen in `~/.role-radar/state.db`, and your alert settings in the Keychain. Git
ignores `profile.yaml`, so keep a copy somewhere safe.

**3. Choose companies.** Remove the ones you don't care about from `config/companies.yaml`
and add your own: a name and the careers page URL ([Supported sources](#supported-sources)
lists the job boards it reads). The checks come from your home internet connection, so a
shorter list is kinder to the sites; the full list includes about 820 Workday boards.

**4. Set up email alerts**, kept in the Keychain. With Gmail, create an
[app password](https://myaccount.google.com/apppasswords) (it needs 2-Step Verification),
then:

```bash
.venv/bin/role-radar secrets set EMAIL_TO you@gmail.com
.venv/bin/role-radar secrets set SMTP_HOST smtp.gmail.com
.venv/bin/role-radar secrets set SMTP_PORT 587
.venv/bin/role-radar secrets set SMTP_USERNAME you@gmail.com
.venv/bin/role-radar secrets set SMTP_PASSWORD        # asks for the app password, hidden
.venv/bin/role-radar notifications test               # sends a test email
```

For Discord instead, or as well: `role-radar secrets set DISCORD_WEBHOOK_URL` (it asks for
the webhook URL). `role-radar secrets` shows which settings are set, never their values.

**5. Try it.** `.venv/bin/role-radar list-matches --company Stripe` prints what matches at
one company right now, without saving or sending anything.

**6. Open the menu bar app.** It needs Xcode or the Command Line Tools
(`xcode-select --install`):

```bash
sh scripts/build_menubar.sh
```

The app starts the checker, which checks companies as they come due while the app is
open. The first check of each company records the jobs already open without alerting
(`notify_on_first_run: true` in `companies.yaml` alerts them too). After that, new
matches collect in **Live Tracking**, and go out every 10 minutes if alerts are set up.

**With an AI agent.** Point it at this section and tell it what you want, for example:
"Set up Role Radar for me: I'm looking for data analyst and analytics engineer roles in
Toronto or remote in Canada, with up to 3 years of experience; alerts to me@example.com."
It can do all of this except typing your email password: run
`role-radar secrets set SMTP_PASSWORD` yourself, so the password never goes into the chat.

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
| Companies config | S3 (`role-radar config push` uploads your `companies.yaml` with your `profile.yaml` applied) | Companies, filters, settings |
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
    → diff vs. stored state (tracker.py): new / removed / returned
    → queue the new matches for the next digest
    → save its jobs, its Live Tracking rows and next check time, in one transaction fenced on the lease
  every 15 s while companies are being checked, and once at the end (even when no company was due):
    if the digest is due (or "Send now" was pressed), collect pending matches across
    enabled companies, except companies being checked right then → record the skipped
    ones instead of sending them → re-read the lease → one digest per channel
    → save each channel's delivery receipts
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
| `role_radar/sqlite.py` | SQLite state store: the same rows, in one file on this Mac |
| `role_radar/storage.py` | `StateStore` interface + JSON and in-memory implementations |
| `role_radar/config.py` | Loads and validates `companies.yaml` (JSON also accepted), with `profile.yaml` applied |
| `role_radar/backends.py` | Picks the store, lease, config source and secrets from `runtime:` |
| `role_radar/keychain.py` | Alert settings in the macOS Keychain |
| `role_radar/aws.py` | boto3 helpers: the config file in S3, secrets in SSM |
| `role_radar/models.py` | `JobPosting` dataclass, stable `uid` and `fingerprint` |
| `role_radar/scrapers/` | One class per ATS plus `generic.py`; registry in `scrapers/__init__.py` |
| `role_radar/filters.py` | Keyword, location and employment-type rules |
| `role_radar/tracker.py` | New-job and removal detection |
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
| Rippling | `ats.rippling.com/api/v2/board/{board}/jobs` (a job with several locations is listed once per location; merged by ID) | only if the filter uses employment type |
| SmartRecruiters | The public career page's location groups, `careers.smartrecruiters.com/{company}/api/groups?page=N`, and each big group's "Show more jobs" pages (the posting API's robots.txt allows only LinkedIn) | no |
| Workable | `apply.workable.com/api/v1/widget/accounts/{account}` (every job in one request; 2 s apart, checked hourly) | no |
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

**Job descriptions are not stored or used for keyword matching.** A job's title,
location, ID and URL are enough to alert on. Detail pages are fetched only when listing
metadata needed by a filter is missing. The one exception is the
[experience filter](#experience-filter), which reads each new match's description once.

The ATS is detected from the URL, or you can set it with `ats:`. Nothing tries to get
past CAPTCHAs, logins or robots.txt, and every request sends an honest User-Agent.
There is no browser automation. Branded sites can still be monitored through their
public feeds/APIs or supported HTML readers; a JavaScript landing page alone is not
evidence that the source works. See [company coverage](docs/company-coverage.md) for
the enabled employers, source limitations and expansion queue.

Each company's `countries` lists where it posts jobs (US, CA, AU, IN), taken from its job
locations by `scripts/tag_countries.py`. Set `settings.countries` in the profile (e.g.
`[US, IN]`) to check only companies that post in one of those; companies without
`countries`, such as ones you add yourself, are always checked. The filters' `locations`
still decide which jobs alert. A company on a job site Role Radar can't read yet carries
`platform:` and stays `enabled: false`.

### Job identity and new-job rules

- **uid** = `company:ats:job_id`. If the ATS gives no job ID, it's a hash of the
  normalized company, title, location and URL (tracking query parameters are removed).
- A job alerts **only when it matches the filters and its uid has never been notified**
  (a company's first check records its open jobs as notified, so they never alert).
  A new uid is a new job, even with the same title and location as another posting:
  it may be another vacancy, or a search reopened after earlier applications were dropped.

| Case | Behaviour |
|---|---|
| New job | Recorded; alerted if it matches |
| Existing job unchanged | Nothing |
| Job removed | `removed_at` set, but only if the listing was complete (a hit page cap or a heuristic parse never counts as removal) |
| Removed job comes back with the same ID | Reactivated, no new alert |
| Reposted with a new ID | A new job: alerted if it matches |
| Two open postings with the same title and location | Each alerts; the notification lists both links |
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
`check_interval_minutes` (20; Workday companies 360, via `check_interval_by_ats`) have
passed since its last check, or straight away if it has never been checked. Between full
checks, `quick_check_by_ats` (Workday: every 20 minutes) reads only the listing's newest
page, one request, and compares its jobs with the ones it showed at the last check. Only
if some are new does it read the company's saved state, and further pages until one holds
a job it already knows. A quick check never marks jobs removed and leaves the full check's
schedule alone; boards that don't list newest first rely on the full check. Each company's state is saved as soon as that company finishes,
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

`settings.digest_interval_minutes: 10` collects all new matching jobs across enabled
companies into one email and one Discord message, newest first (as Live Tracking lists them), each
with its company; one role in several places is one entry. In Discord each
job's title is a link to apply. A Discord message holds about 6,000 characters, roughly
30-35 jobs; a longer digest shows what fits and says how many more are in the email.
With email switched off (or not set up), Discord sends the rest in more messages instead.
Empty intervals send nothing.

Discord and email each have an on/off switch (`role-radar switch discord off`, or the
menu bar app). A digest goes only to the channels switched on. With both off, sites are
still checked and new matches saved; they go out in the first digest after one is
switched back on. Jobs sent while a channel was off aren't sent to it later.

The schedule targets `:00` and `:30`. Lambda sends on its first run after the boundary,
after any checks in that run finish; its five-minute trigger can add a short delay.
The laptop uses the same schedule. Pending jobs, the next send time and delivery
receipts persist across restarts and handoffs. Failed channels retry at the next
interval without repeating delivery to successful channels. Previously sent jobs
are not replayed when digests are enabled.

The digest doesn't wait for a round of checks to end: while one is running it looks
every 15 seconds, and sends when it's due. A company being checked at that moment is
left for the next digest, so its matches aren't sent twice.

Set the interval to `0` (also the default when omitted) to send immediately after
each company's check. Baselines and dry runs never send a digest.

#### Live Tracking

The menu bar app's **Live Tracking** window (or `role-radar matches`) is the stack of new
jobs, so alerts are optional: open the app and the latest roles are on top. In the
downloadable app it's the same window as Setup: **Edit Setup** switches it to Setup's pages,
and finishing them switches it back. It has three sections, newest first:

- **New jobs.** A match appears as soon as its company's check is saved, not when the
  round ends. With alerts on, the digest sends them every 10 minutes and they leave the
  stack; with both channels off (or none set up), the stack keeps growing, and the first
  digest after one is switched on sends everything in it.
- **Seen**, folded until opened. Select jobs (the box beside each, or the one above them
  all) and **Mark as Seen**, or **Mark All as Seen** with none selected (`role-radar matches
  skip COMPANY UID`, `--pick COMPANY UID` for each of several, or `--all`). The next digest
  time records them as skipped instead of sending them (`dropped_for: skipped in Live
  Tracking`), alerts on or off. They stay listed, shaded, for a week, and **Mark as New**
  (`unskip`) puts one back meanwhile: at once if its skip isn't recorded yet, otherwise the
  next digest time undoes the record.
- **Sent alerts.** Each alert that went out, newest first: when, and the jobs it held.

**Send as Alert** sends the selected jobs now, or **Send All as Alert** all of them
(`role-radar matches send`, with `--pick` or `--all`), alerts on or off: to the channels
switched on, or every one set up when both are off. The Mac sends within a minute, Lambda at its next run, and sent jobs
leave the stack. The window also shows the round in progress (how many of the due companies
are done), and the last 24 hours' activity. It reads the lists every 5 seconds while it's open.

## Configuration

The companies live in [config/companies.yaml](config/companies.yaml), and what you're
looking for in `config/profile.yaml` (see [Run it on your Mac](#run-it-on-your-mac)). With
AWS, `role-radar config push` uploads both after each change. The `filters` in your profile
apply to every company, and a company's own `filters` replace them **one key at a time**.
(Without a profile, `defaults.filters` in `companies.yaml` plays that part.)

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
new field matchable, add it to `FIELD_GETTERS`. Keywords can't match descriptions, and a
config that puts `description` in `match_on` or `exclude_on` is rejected at load time.

### Experience filter

With `max_experience_years: 2` (the default filters set it), each new title match's
description is read once, before it's alerted. A match asking for more years is
recorded without alerting, with the reason (`dropped_for` on the stored job, and a
"not alerting" log line). How [experience.py](role_radar/experience.py) reads it:

- Preferred / nice-to-have sections and phrases ("3+ years preferred", "is a plus") and
  about-the-company text don't count.
- Alternatives count: "3+ years, or a Master's", "BS + 4 yrs or MS + 2 yrs" and
  "3+ years (2+ with a Master's)" fit 2 years; "MS and 3+ years" doesn't.
- The job needs the most years any requirement asks for, so "2+ years of Go" beside
  "4+ years of backend" needs 4.
- Unclear means keep: no years found, a description that can't be read, or "a master's
  may substitute for experience".

Where descriptions come from: the listing already has them for Ashby, Lever, amazon.jobs
(basic qualifications), Google (minimum qualifications), Meta (from the job page it
already reads) and TikTok. Greenhouse, Workday, Rippling, SmartRecruiters, Workable, Oracle HCM, Eightfold and Apple take one
request per new match, at most `max_detail_requests` a check (the rest wait for the next
check). Other sources have no description, so their matches are kept. Set
`max_experience_years: null` in a company's `filters` to skip reading its descriptions.

The default role list follows the candidate's resume: application development
(including full stack, frontend and backend), platforms/cloud, distributed systems,
compilers, performance and graphics, AI/LLM/ML and applied research, data engineering
and data science, GRC engineering, test automation, technical program and product
management, and forward-deployed and solutions engineering
([why](docs/company-coverage.md#resume-alignment-september-2026)).
It targets potential opportunities for a spring-2026 MS CS graduate; matching a title
does not establish eligibility. Review the posting's experience, specialized skills,
degree, graduation window and work authorization requirements separately. Program
roles in particular need this review. Graduation years and "new grad" are not
required in titles, so unlabelled early-career openings can match.

The defaults exclude senior and leadership titles, while permitting Technical Program
Manager and Product Manager. "Member of Technical Staff" is also allowed.
They also exclude off-profile titles the broad keywords catch: ERP/CRM platform
developers (ServiceNow, SAP, Salesforce and similar), technical-writing "developers",
and hardware or manufacturing titles (firmware, FPGA, ASIC, CNC, technician and
similar). Continental Finance inherits the role list and retains its
"Mid/Senior Software Developer" exception for review, but otherwise excludes the same
titles as the defaults. The location rules target the
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

The `runtime:` section of your profile (of `companies.yaml`, without one) picks where
things live, and environment variables override each key. That way one codebase serves
the Mac, Lambda and tests.

| Key | Env var | Values |
|---|---|---|
| `storage` | `ROLE_RADAR_STORAGE` | `sqlite`: this Mac only, in `state_file`. `dynamodb`: the table shared with Lambda. `json` (default): one file, for tests and dry runs |
| `state_file` | `ROLE_RADAR_STATE_FILE` | The local state file (default `~/.role-radar/state.db` for sqlite, `seen_jobs.json` for json) |
| `table` | `ROLE_RADAR_TABLE` | DynamoDB table name |
| `config_url` | `ROLE_RADAR_CONFIG_URL` | `s3://bucket/key`. Companies and settings are read from there; the local files then only supply `runtime:` |
| `secrets` | `ROLE_RADAR_SECRETS` | `env` (default): environment variables. `keychain`: this Mac's Keychain (`role-radar secrets set NAME`). `ssm:/role-radar/`: SSM Parameter Store, fetched the first time an alert is sent |
| `region`, `profile` | `AWS_REGION`, `AWS_PROFILE` | Which AWS region and `~/.aws` profile to use |

The AWS backends need boto3: `pip install '.[aws]'`. The SQLite store keeps the same rows
as the DynamoDB table below, in one file, so the checker and the menu bar app's commands
(switches, skips, Send Now) can write at the same time. It needs no lease: one `start`
runs per Mac, and `run --once` refuses to run beside it. The JSON store rewrites a single
file and suits one process only: tests and dry runs.

**DynamoDB layout** (one table, `pk` + `sk`):

| pk | sk | Item |
|---|---|---|
| company name | job uid | A seen job (the fields of `SeenJob`) |
| `#schedule` | company name | `last_checked_at`, `next_check_at`, failure count |
| `#digest` | `#digest` | `next_send_at`, `last_attempt_at`, `interval_minutes` |
| `#digest` | `#request` | `requested_at`: the latest "Send now" |
| `#lease` | `#lease` | Who may check companies now: `holder`, `epoch`, `expires_at` |
| `#alerts` | time + company + uid | Log of sent alerts, which expires after 30 days via TTL |
| `#runs` | runner | Each runner's last pass |
| `#queue` | company + uid | A match waiting for the digest, for Live Tracking; `skipped_at` if cleared, `send_at` if sent from there. Written with the company's save; an applied skip expires after 7 days via TTL |
| `#round` | runner | The runner's latest round: `started_at`, `total`, `done`, `finished_at` |

Reads are strongly consistent. Right after a handoff, the new runner must see everything
the previous one wrote. Each save writes only the rows that changed.

To write another backend, subclass `storage.StateStore`. Runs use `load_schedule()`
(every company's next check time), then `load_company()` and `save_company()` around
each company's check. `load_digest()` and `save_digest()` persist the shared digest
schedule; its writes must use the same lease fence as company saves. Each schedule row
also keeps `pending`, the company's matched jobs not yet sent, so the digest calls
`load_company()` only for companies that have some. `load_queue()`, `mark_skipped()`
and `request_digest()` serve Live Tracking. `load()` and
`save()` move a whole state at once, for migration.

## Commands

Install the `role-radar` command. [pipx](https://pipx.pypa.io) keeps it in its own
environment:

```bash
pipx install '.[aws]'                 # from the project directory; drop [aws] to run on your Mac only
```

| Command | What it does |
|---|---|
| `role-radar start` | Runs until you quit (Ctrl+C). Takes the lease and checks companies as they come due. If Lambda holds the lease, it asks Lambda to hand over and takes over once it has. |
| `role-radar stop` | Asks a running `start` (for example the login item) to finish the companies in flight, release the lease and quit. While the menu bar app is open, it starts the checker again within a minute, on the current code: that's a restart. |
| `role-radar status` | Shows who holds the lease, each runner's last pass, the last 24 hours' activity (checks by the Mac and by Lambda, new jobs, new matches, alerts sent), which companies are due or failing, how many haven't had their first successful check yet (after adding companies, it reaches zero once every baseline is saved), the latest alerts, and whether the pushed config matches your local file. |
| `role-radar doctor` | Checks setup without sending alerts or changing job state. Add `--stack role-radar --profile admin --region us-east-1` to inspect the deployed Lambda and EventBridge schedule. |
| `role-radar notifications test` | Sends a labeled test through the configured channels without changing job state. Add `--channel email` or `--channel discord` to test one. |
| `role-radar run --once` | One pass over the due companies, then exits. Add `--all`, `--company NAME`, `--dry-run` (print alerts, save nothing, no lease), `--baseline` (record everything as seen, no alerts) or `--local-config` (read the local file instead of the pushed copy). |
| `role-radar list-matches` | Prints every job matching right now. Reads no state, sends nothing. |
| `role-radar config push` | Validates your `companies.yaml` with your `profile.yaml` applied and uploads the result, one file, to `runtime.config_url`. That's also your profile's backup. |
| `role-radar config pull` | Writes `profile.yaml` back from what `config push` uploaded, for example on a new Mac: `role-radar config pull --url <ConfigUrl output>` with `AWS_PROFILE` set. Won't replace an existing profile without `--force`. |
| `role-radar secrets` | Shows which alert settings are in the Keychain (`runtime.secrets: keychain`). `secrets set NAME [VALUE]` saves one: leave out the value for passwords, and it's asked for, hidden. `secrets delete NAME` removes one. |
| `role-radar migrate --from sqlite:~/.role-radar/state.db --to dynamodb:TABLE` | Copies state between stores, in either direction: `sqlite:PATH`, `dynamodb:TABLE` or `json:PATH`. |
| `role-radar login-item on\|off` | Sets up the Mac's checker: `role-radar start` as a launchd agent, started now. The menu bar app owns it from then on (see below), so it has no RunAtLoad or KeepAlive: launchd never starts it by itself. It logs to `~/Library/Logs/role-radar.log`. It needs the alert settings in the Keychain or SSM, because a launchd agent can't see your shell's environment variables. |
| `role-radar switch laptop\|lambda on\|off` | Turns a runner on or off, independently; no arguments shows every switch. The switches live in the state store. A laptop switched off releases the lease (so Lambda covers, if it's on) and idles until switched back on, picking up the change within a minute; a pass in progress stops starting companies within 30 s. Lambda switched off exits at once on each run. With both off, nothing is checked. `status` shows the switches. |
| `role-radar switch discord\|email on\|off` | Turns an alert channel on or off; the next digest applies it. With both off, new matches are saved and sent once one is back on (see [Notification digests](#notification-digests)). |
| `role-radar matches` | Lists the matches waiting to be sent, skipped and sent (see [Live Tracking](#live-tracking)). `skip COMPANY UID` (or `--pick COMPANY UID` for each of several, or `--all`) clears matches: never sent; `unskip` puts them back while they're listed (a week); `send` sends matches now, alerts on or off (all of them without `--pick`). `--json` is what the menu bar app reads. |
| `role-radar ui` | Opens a local page (127.0.0.1:8765) with the same two switches, the lease holder and each runner's last pass. `--port`, `--no-browser`. |
| `role-radar setup` | What the packaged app's Setup window runs: `init`, `show`, `profession` (`{"profession": "tech"}` on stdin: `tech`, `accounting` or `healthcare`; brings its company list and titles), `profile` (target titles, non-target words, `countries`, cities as `locations`, experience and education; JSON on stdin), `prompt` (a ChatGPT/Claude prompt), `companies` (add companies of your own, e.g. from the AI's answer on stdin; `--replace`), `email` (Gmail address and app password, JSON on stdin, into the Keychain), `recipients` (who else gets the alerts, JSON on stdin). It only rewrites files it wrote itself. |
| `scripts/package_app.sh` | Builds `dist/Role-Radar-<version>-apple-silicon.dmg`: the app with its own Python, for someone else's Mac, in a disk image whose window shows it beside Applications (see [Get the app](#get-the-app)). The same file is the update. |
| `scripts/publish_update.sh` | Signs the packaged disk image with the update key, writes `appcast.xml`, and creates the GitHub release every copy of the app updates from (`DRY_RUN=1` stops before publishing). |
| `scripts/reset_app.sh` | Quits the packaged app and removes its checker, files, logs and Keychain items: its next launch is a first run. |
| `scripts/build_menubar.sh` | Builds and opens **Role Radar.app**, a macOS menu bar app (in `~/Applications`) with the same two runner switches as native toggles, who's checking right now, each runner's last pass, Discord and email alert switches, the round in progress, how many matches are waiting to be sent, how many sites are failing, and a **Live Tracking** button. That opens a window with the matches waiting, skipped and sent (see [Live Tracking](#live-tracking)), Send Now, and the Activity section: checks per hour over the last 24 hours (the Mac and Lambda stacked; hover a bar for its numbers), and the day's checks, new jobs, new matches and alerts sent. Each pass adds its counts to an hourly row in the state store (`#stats`, kept two days), so the app reads 24 small rows a minute. The menu bar icon shows a laptop while the Mac is checking, a cloud while Lambda is, and a crossed-out antenna when nothing is. The app owns the Mac's checker (the login item, installed if needed): while it's open and the Mac is switched on, it starts the checker and restarts it within a minute if it stops; quitting the app stops it, and Lambda takes over. So quitting and reopening the app restarts the checker on the current code, and the checker starts at login only if the app does (its Open at Login). Rebuilding with this script restarts it too. Needs Xcode or the Command Line Tools. |

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

## Development

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

With `secrets: env` and no notification settings, alerts go to stdout. With secrets in the
Keychain or SSM, no settings means alerts stay pending: they're retried, and on Lambda the
error alarm fires, rather than alerts vanishing into a log.

## Add AWS: keep checking while your Mac is off

This adds a Lambda that checks every 5 minutes whenever your Mac isn't, with the jobs
it has seen in DynamoDB, your config in S3 and your alert settings in SSM, all within
AWS's free tier. Set up [Run it on your Mac](#run-it-on-your-mac) first. You need:

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
- The Lambda (arm64, 1 GB, 14-minute timeout, reserved concurrency 1, outside any VPC,
  so no NAT gateway) and its 5-minute EventBridge schedule.
- CloudWatch error and falling-behind alarms with an email subscription, and a $5/month
  AWS Budget alert.
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

**3. Point your profile at the stack.** Copy the stack outputs
(`sam list stack-outputs --stack-name role-radar --profile admin`) into `config/profile.yaml`,
replacing its Mac-only `runtime:` section:

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

- Move what your Mac has seen so far over, if it has been running on its own:
  `role-radar migrate --from sqlite:~/.role-radar/state.db --to dynamodb:<TableName>`.
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
role-radar config push         # companies.yaml + profile.yaml; Lambda starts checking from its next run
role-radar start               # in a terminal; Ctrl+C to quit (Lambda takes over)
role-radar login-item on       # or: in the background, run by the menu bar app
role-radar status              # who has the lease, last passes, due companies, alerts
```

To see a handoff, quit `start` and run `role-radar status` a few minutes later. The
lease shows `held by lambda:…`, and afterwards `free since …`. Start it again and the
laptop takes over.

## Costs

After the credits run out, everything here stays within AWS's always-free allowances
except Lambda, S3 requests (fractions of a cent) and point-in-time recovery (about $0.20
per GB-month of the table). With ~4,500 companies, a Lambda pass takes a few minutes
(mostly waiting out the Workday request spacing), so while the laptop runner is off
Lambda uses roughly 1–1.6M GB-s a month at 1 GB: **about $10–16 a month** over the free
400,000 GB-s. While the laptop runs, Lambda costs next to nothing. The $5 budget emails
you at 80% of actual spend, or if the month is forecast to exceed $5.

| Service | This project's use | Always-free allowance (checked 2026-09-26) |
|---|---|---|
| Lambda (arm64, 1 GB) | 8,640 runs/month. When the laptop is off, with ~4,500 companies, about 2–3 minutes each: roughly 1–1.6M GB-s. Since Aug 1, 2025, cold-start INIT time is billed as duration too; it counts against the same allowance. | 1M requests + 400,000 GB-s per month ([pricing](https://aws.amazon.com/lambda/pricing/), [INIT billing](https://aws.amazon.com/blogs/compute/aws-lambda-standardizes-billing-for-init-phase/)) |
| DynamoDB (provisioned) | 25 RCU / 25 WCU, a few MB. A check costs about 4 WCU (transactional writes cost 2 per item) and 6 RCU | 25 WCU, 25 RCU, 25 GB per region ([pricing](https://aws.amazon.com/dynamodb/pricing/provisioned/), [transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)) |
| EventBridge Scheduler | 8,640 invocations/month | 14M per month ([pricing](https://aws.amazon.com/eventbridge/pricing/)) |
| CloudWatch | A few GB of logs/month (kept 14 days), 2 alarms, 2 custom metrics | 5 GB of logs, 10 alarms, 10 custom metrics ([pricing](https://aws.amazon.com/cloudwatch/pricing/)) |
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
  the login item the menu bar app runs (rotated at 5 MB). Lambda logs to CloudWatch:
  `aws logs tail /aws/lambda/role-radar-monitor --follow --profile admin`.
- **Error alarm.** It emails you when a Lambda run fails: alerts that couldn't be
  delivered, every company failing, a bad config, or missing permissions, and also
  when Lambda runs out of memory or times out. Single broken sites don't trip it;
  `role-radar status` lists them.
- **Falling-behind alarm.** Each pass publishes how many due companies it ran out of
  time for (`RoleRadar/LeftForLater`). If every pass for six hours left some, it emails
  you: alerts are running late. Right after adding many companies it can fire while
  their baselines are saved; otherwise raise the Lambda's `MemorySize` or check fewer
  companies.
- **Changing companies or filters.** Edit `config/companies.yaml` (companies) or
  `config/profile.yaml` (what you're looking for), then `role-radar config push`. A
  running laptop app picks it up within 5 minutes, and Lambda at its next run. `status`
  warns when your local files and the pushed copy differ.
- **A new Mac.** Clone the repo and install it, set up the `role-radar` AWS profile
  (`aws configure --profile role-radar`), then
  `AWS_PROFILE=role-radar role-radar config pull --url <ConfigUrl output>` writes your
  `profile.yaml` back from the last push. The jobs you've seen are in DynamoDB and your
  alert settings in SSM, so nothing else needs restoring.
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
  `boards-api.greenhouse.io` gets 0.25 s, `api.lever.co` / `api.ashbyhq.com` get
  0.3 s, `ats.rippling.com` and `smartrecruiters.com` get 0.5 s, and `apply.workable.com` 2 s
  (it rate-limits at 2 requests a second, so Workable boards are checked hourly). Override or add hosts under `settings.http.host_delays`; a key also covers its
  subdomains, and subdomains under a parent-domain key share one rate. An HTTP 429
  pauses every host sharing that rate for the Retry-After time (or 60 s). Most boards
  take one request, so a full round of 1,000 companies takes a few minutes, and the
  schedule spreads that round over the half hour.
- Workday is the exception: one request per 20 jobs, and every company's tenant sits
  on the same service, which answers bursts from one IP with HTTP 429. The config
  spaces all `myworkdayjobs.com` tenants as one host (0.5 s), checks at most two
  Workday companies at a time (`settings.company_concurrency_by_ats`), and checks
  them every six hours (`settings.check_interval_by_ats`). With ~820 Workday
  companies (most capped at their newest 200 jobs) that is about 9,000 requests per
  round, about 0.4 requests/s on average. Quick checks (`settings.quick_check_by_ats`)
  add about one request per company every 20 minutes, about 0.7 requests/s, and run
  before a pass's full checks.
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

- Secrets never live in the repo or the config. They sit in the Keychain, in SSM, or in
  your local `.env`, and are read only when an alert is sent.
- The code never logs webhook URLs or credentials. `httpx` logs every request URL, so its
  logging is kept at WARNING. The same goes for botocore, whose debug output would
  include decrypted SSM values.
- Discord messages set `allowed_mentions: none`, so a job title containing `@everyone`
  can't ping your server.
- The Lambda's role and the laptop's policy can only reach this table, the config object
  and the `/role-radar/` parameters.

## License

MIT: see [LICENSE](LICENSE).
