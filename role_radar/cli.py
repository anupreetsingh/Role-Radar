"""role-radar: the command line.

  role-radar start               run until you quit (Ctrl+C): take the lease and check
                                 companies as they come due; Lambda covers while it's off
  role-radar stop                ask a running `start` to quit (while the menu bar or Windows app
                                 is open, it starts it again within a minute, on the current code)
  role-radar status              lease holder, last passes, companies due, recent alerts
  role-radar doctor              check config, secrets and AWS deployment without sending alerts
  role-radar notifications test  send a test to configured notification channels
  role-radar run --once          one pass over the due companies
                                 [--all] [--company NAME] [--dry-run] [--baseline]
  role-radar list-matches        print every job matching right now (no state, no alerts)
  role-radar switch laptop|lambda|discord|email on|off
                                 turn a runner or alert channel on or off (no arguments: show all)
  role-radar ui                  a local page with on/off switches for the laptop and Lambda
  role-radar matches             matches waiting to be sent, skipped, and sent
                                 [skip|unskip COMPANY UID | skip --all | send] [--json]
  role-radar config push         upload the companies file, with your profile applied, to runtime.config_url
  role-radar config pull         restore a lost profile.yaml from what `config push` uploaded
  role-radar secrets [set|delete NAME]
                                 alert settings in this computer's keychain: the Mac's Keychain, or
                                 Windows Credential Manager (runtime.secrets: keychain)
  role-radar setup [init|show|profession|profile|prompt|companies|email|discord|recipients]
                                 the packaged app's first-run setup (JSON on stdin and stdout)
  role-radar migrate --from dynamodb:TABLE --to sqlite:PATH   (or json:PATH, either way)
  role-radar login-item on|off   set up the Mac's background checker (`role-radar start` under
                                 launchd), which the menu bar app starts and stops (macOS)

The local companies file is --config, else $ROLE_RADAR_CONFIG_FILE, else
./config/companies.yaml, else ~/.config/role-radar/companies.yaml. The
profile beside it (profile.yaml, or $ROLE_RADAR_PROFILE) holds your filters
and the `runtime:` section, which, overridden by ROLE_RADAR_* environment
variables, says where state, the pushed config and alert secrets live.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

import yaml

from role_radar import __version__, aws, launchd, suggest, winchecker
from role_radar.backends import AwsClients, ConfigSource, NotifierSource, open_backend, require_filters, resolve_runtime
from role_radar.config import PROFILE_SECTIONS, RuntimeSettings, combined, parse_raw, profile_path, split_profile
from role_radar.instance import InstanceLock
from role_radar.lease import Lease, LeaseKeeper
from role_radar.monitor import print_matches
from role_radar.runner import EXIT_LEASE_HELD, Runner
from role_radar.schedule import due_companies, interval_for, next_due
from role_radar.storage import CHANNELS, RUNNERS, SWITCHES, JsonStateStore, StateStore, alerts_off, from_iso, switch_on, utcnow

log = logging.getLogger("role-radar")

EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


@dataclass
class Context:
    config_path: Path | None  # the local companies file, if there is one
    runtime: RuntimeSettings
    clients: AwsClients


def default_config_path() -> Path | None:
    if os.environ.get("ROLE_RADAR_CONFIG_FILE"):
        return Path(os.environ["ROLE_RADAR_CONFIG_FILE"]).expanduser()
    for candidate in (Path("config/companies.yaml"), Path.home() / ".config" / "role-radar" / "companies.yaml"):
        if candidate.exists():
            return candidate
    return None


def context(args: argparse.Namespace) -> Context:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if path and not path.exists():
        raise FileNotFoundError(f"config file {path} doesn't exist")
    runtime = resolve_runtime(path)
    return Context(path, runtime, AwsClients(runtime))


def hostname() -> str:
    return socket.gethostname().split(".")[0] or "localhost"


def make_runner(ctx: Context, holder: str) -> Runner:
    return Runner(
        ConfigSource(ctx.runtime, ctx.config_path, ctx.clients),
        open_backend(ctx.runtime, holder, ctx.clients),
        NotifierSource(ctx.runtime, ctx.clients),
    )


def _handle_signals(stop: asyncio.Event) -> None:
    """First Ctrl+C / SIGTERM / SIGHUP: finish what's in flight, then release. Second: quit now.

    On Windows, which has neither signal, `role-radar stop` leaves a stop file instead (instance.py).
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set() and task:
            log.warning("Quitting now")
            task.cancel()
        else:
            log.info("Finishing the companies in progress, then releasing the lease (Ctrl+C again to quit now)")
            stop.set()

    if sys.platform == "win32":
        _watching.add(loop.create_task(_watch_for_stop(on_signal)))
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, on_signal)


_watching: set[asyncio.Task] = set()  # the stop file watchers, kept from the garbage collector


async def _watch_for_stop(on_stop: Callable[[], None], every: float = 1.0) -> None:
    """Windows: `role-radar stop` has asked this instance to quit, as SIGTERM does elsewhere."""
    lock = InstanceLock()
    while True:
        await asyncio.sleep(every)
        if lock.stop_requested():
            on_stop()


# -- commands -------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    lock = InstanceLock()
    if not lock.acquire():
        print(f"role-radar is already running (pid {lock.running_pid()}); `role-radar stop` quits it", file=sys.stderr)
        return 1
    try:
        ctx = context(args)
        runner = make_runner(ctx, f"laptop:{hostname()}")
        try:
            runner.config()  # a broken config should stop us here, not in the loop
        except (ValueError, FileNotFoundError):
            raise
        except Exception as exc:  # offline (e.g. just logged in, Wi-Fi not up yet): keep trying
            log.warning("Can't read the config from %s yet (%s); will keep trying", runner.source.description, exc)
        log.info("Role Radar started: state in %s, config from %s", describe(ctx.runtime), runner.source.description)

        async def serve() -> int:
            stop = asyncio.Event()
            _handle_signals(stop)
            return await runner.serve(stop)

        code = asyncio.run(serve())
        log.info("Stopped%s", "; Lambda takes over at its next run" if ctx.runtime.storage == "dynamodb" else "")
        return code
    finally:
        lock.release()


def cmd_stop(args: argparse.Namespace) -> int:
    pid = InstanceLock().stop_running()
    print(f"Stopped role-radar (pid {pid})." if pid else "role-radar isn't running.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ctx = context(args)
    if args.local_config:  # e.g. a baseline before the first `config push`
        if not ctx.config_path:
            raise ValueError("--local-config needs a local config file (--config PATH)")
        ctx = Context(ctx.config_path, replace(ctx.runtime, config_url=None), ctx.clients)
    if ctx.runtime.storage != "dynamodb" and not args.dry_run and InstanceLock().running_pid():
        raise ValueError("role-radar start is checking on this Mac, and a run now would clash with it. "
                         "Stop it first (role-radar stop, or switch the Mac off in the menu bar app).")
    runner = make_runner(ctx, f"cli:{hostname()}")
    options: dict[str, Any] = {
        "baseline": args.baseline,
        "check_all": args.all,
        "only": {c.lower() for c in args.company} or None,
    }
    if args.dry_run:  # reads state, takes no lease, prints alerts, saves nothing
        return asyncio.run(runner.pass_once(dry_run=True, **options)).exit_code

    async def once() -> int:
        stop = asyncio.Event()
        _handle_signals(stop)
        return await runner.run_once(stop, **options)

    return asyncio.run(once())


def cmd_list_matches(args: argparse.Namespace) -> int:
    ctx = context(args)
    config = ConfigSource(ctx.runtime, ctx.config_path, ctx.clients).load()
    only = {c.lower() for c in args.company}
    companies = [c for c in config.companies if c.checked and (not only or c.name.lower() in only)]
    return asyncio.run(print_matches(companies, config.settings))


def cmd_status(args: argparse.Namespace) -> int:
    ctx = context(args)
    runtime = ctx.runtime
    backend = open_backend(runtime, "status", ctx.clients)
    source = ConfigSource(runtime, ctx.config_path, ctx.clients)
    config = source.load()
    now = utcnow()

    print(f"State:          {describe(runtime)}")
    print(f"Switches:       {describe_switches(backend.store.load_switches(), runtime)}")
    print(f"Lease:          {describe_lease(backend.lease, runtime)}")
    pid = InstanceLock().running_pid()
    print(f"This machine:   role-radar start {'is running (pid %d)' % pid if pid else 'is not running'}")

    for runner, run in sorted(backend.store.last_runs().items(), key=lambda kv: kv[1].get("finished_at", ""), reverse=True):
        print(
            f"Last pass:      {runner}, {when(run.get('finished_at'))}: {run.get('checked', 0)} checked, "
            f"{run.get('alerts', 0)} alert(s), {run.get('failed', 0)} failed, "
            f"{run.get('undelivered', 0)} with undelivered alerts, {run.get('seconds', 0)}s"
        )

    from role_radar import ui

    day = ui.activity(backend.store, now)
    mac, cloud = (sum(h[who] for h in day["hours"]) for who in ("mac", "lambda"))
    print(f"Last 24 hours:  {day['checked']:,} checks (Mac {mac:,}, Lambda {cloud:,}), {day['failed']:,} failed, "
          f"{day['new_jobs']:,} new jobs, {day['matches']:,} new matches, {day['alerts']:,} alerts sent")

    enabled = [c for c in config.companies if c.checked]
    schedule = backend.store.load_schedule()
    interval = interval_for(config.settings)
    due = due_companies(enabled, schedule, now, interval)
    upcoming = next_due(enabled, schedule, now, interval)
    line = f"Companies:      {len(enabled)} enabled, {len(due)} due now"
    print(line + (f", next due {when(upcoming)}" if not due and upcoming else ""))
    # A company is saved once a check succeeds (the same test as CompanyRecord.is_new).
    unsaved = sum(1 for c in enabled if not ((m := schedule.get(c.name)) and (m.last_ok_at or (m.last_checked_at and not m.failures))))
    if unsaved:
        print(f"                {unsaved} not saved yet: waiting for a first successful check")
    names = {c.name for c in enabled}
    failing = sorted(((n, m) for n, m in schedule.items() if m.failures and n in names), key=lambda nm: -nm[1].failures)
    for name, meta in failing[:5]:
        print(f"  failing:      {name}: {meta.last_error} ({meta.failures} in a row, next try {when(meta.next_check_at)})")
    if len(failing) > 5:
        print(f"                ...and {len(failing) - 5} more")

    alerts = backend.store.recent_alerts(10)
    print("Recent alerts:" + ("" if alerts else "  none yet"))
    for a in alerts:
        where = f" ({a['location']})" if a.get("location") else ""
        by = f"  [{a['by']}]" if a.get("by") else ""
        print(f"  {when(a.get('notified_at'))}  {a.get('company')}: {a.get('title')}{where}{by}")

    if runtime.config_url and ctx.config_path:
        same = combined(ctx.config_path, profile_path(ctx.config_path)) == yaml.safe_load(source.read_text())
        note = "same as your local files" if same else "differs from your local files: run `role-radar config push`"
        print(f"Config:         {runtime.config_url} ({note})")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    from role_radar import ui

    ctx = context(args)
    backend = open_backend(ctx.runtime, "switch", ctx.clients)
    store = backend.store
    if args.name:
        if args.state is None:
            raise ValueError("say on or off, e.g. role-radar switch lambda off")
        store.save_switch(args.name, args.state == "on")
    if args.start and switch_on(store.load_switches(), "laptop") and not InstanceLock().running_pid():
        _start_checker(ctx)
        _wait_for_start()
    if args.json:
        state = ui.snapshot(backend, InstanceLock().running_pid, storage=ctx.runtime.storage)
        state["login_item"] = sys.platform == "darwin" and launchd.plist_path().exists()
        print(json.dumps(state))
        return 0
    switches = store.load_switches()
    print(f"Switches: {describe_switches(switches, ctx.runtime)}")
    if args.name == "laptop" and args.state:
        print("A running laptop app picks this up within a minute." if InstanceLock().running_pid()
              else "role-radar start isn't running on this Mac, so the switch applies when it next starts.")
    elif args.name == "lambda" and args.state:
        print("Lambda applies it at its next run (every 5 minutes).")
    elif args.name in CHANNELS and args.state:
        print("The next digest applies it.")
    if not any(switch_on(switches, r) for r in RUNNERS):
        print("Both runners are off: no companies will be checked.")
    if alerts_off(switches):
        print("Both alert channels are off: new matches are saved and sent once one is back on.")
    return 0


def cmd_matches(args: argparse.Namespace) -> int:
    """Live Tracking: list the matches, skip (clear) some (they're recorded, never sent) or undo that, or send some now."""
    from role_radar import ui

    ctx = context(args)
    store = open_backend(ctx.runtime, "matches", ctx.clients).store
    picked = [tuple(pick) for pick in args.pick or []] + ([(args.company, args.uid)] if args.company and args.uid else [])
    if args.stdin:  # the app's way for a big selection: a launched process takes at most 4,096 arguments
        given = json.load(sys.stdin)
        if not isinstance(given, list) or not all(isinstance(p, list) and len(p) == 2 for p in given):
            raise ValueError('--stdin takes JSON: [["COMPANY", "UID"], ...]')
        picked += [(str(company), str(uid)) for company, uid in given]
    if args.action in ("skip", "unskip", "send"):
        skip = args.action != "unskip"
        if args.all or (args.action == "send" and not picked):  # `send` alone sends them all, as before
            targets = [(m.company, m.uid) for m in store.load_queue() if m.seen != skip]
        elif picked:
            targets = picked
        else:
            raise ValueError(f"say which match, e.g. role-radar matches {args.action} Microsoft microsoft:eightfold:123 (or --all)")
        if args.action == "send":
            gone = [f"{company}: {uid}" for company, uid in targets if not store.mark_send(company, uid)]
            store.request_digest()
        else:
            gone = [f"{company}: {uid}" for company, uid in targets if not store.mark_skipped(company, uid, skip)]
        if gone:
            print(f"Not waiting any more (already sent, or skipped): {', '.join(gone)}", file=sys.stderr)
    elif args.company or args.all or picked:
        raise ValueError("say skip, unskip or send, e.g. role-radar matches skip COMPANY UID")

    state = ui.live(store)
    if args.json:
        print(json.dumps(state))
        return 0
    if args.action == "send":
        print("Asked for those matches to go out now: the Mac sends them within a minute"
              + (", Lambda at its next run." if ctx.runtime.storage == "dynamodb" else "."))
    if state["alerts_off"]:
        print("Both alert channels are off: matches collect here until you send them, or switch one back on.")
    else:
        print(f"Next digest:    {when(state['next_digest'])}" + (" (sending now)" if state["send_requested"] else ""))
    for title, rows in (("Waiting to be sent", state["waiting"]), ("Skipped", state["skipped"])):
        print(f"{title} ({len(rows)}):" + ("" if rows else "  none"))
        for m in rows:
            where = f" ({m['location']})" if m.get("location") else ""
            note = "" if title == "Waiting to be sent" else ("  [recorded]" if m["final"] else "  [until the next digest]")
            print(f"  {when(m['found_at'])}  {m['company']}: {m['title']}{where}  {m['uid']}{note}")
    print("Sent alerts:" + ("" if state["sent"] else "  none yet"))
    for alert in state["sent"][:5]:
        jobs = alert["jobs"]
        print(f"  {when(alert['sent_at'])}, {len(jobs)} job{'' if len(jobs) == 1 else 's'}:")
        for job in jobs:
            where = f" ({job['location']})" if job.get("location") else ""
            print(f"    {job.get('company')}: {job.get('title')}{where}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """What the packaged app's Setup window runs. Input on stdin (JSON, or the AI's answer), output JSON."""
    from role_radar import onboarding

    config = Path(args.config).expanduser() if args.config else default_config_path()
    if not config:
        raise ValueError("where? pass --config PATH (the companies file; the profile goes beside it)")
    action = args.action or "show"
    if action == "init":
        onboarding.init(config)
    elif action == "profession":
        onboarding.save_profession(config, json.load(sys.stdin).get("profession") or "")
    elif action == "profile":
        data = json.load(sys.stdin)
        onboarding.save_profile(config, data.get("roles") or [], data.get("exclude") or [], data.get("locations") or [],
                                data.get("max_experience_years"), data.get("countries"), data.get("education"))
    elif action == "prompt":
        print(onboarding.prompt(config))
        return 0
    elif action == "companies":
        report = onboarding.import_companies(config, sys.stdin.read(), replace=args.replace, check=not args.no_check)
        print(json.dumps(report.as_dict()))
        return 0
    elif action == "email":
        data = json.load(sys.stdin)
        onboarding.save_email(data.get("address") or "", data.get("password") or "")
    elif action == "discord":
        onboarding.save_discord(json.load(sys.stdin).get("webhook") or "")
    elif action == "recipients":
        onboarding.save_recipients(json.load(sys.stdin).get("also") or [])
    elif action == "find":
        data = json.load(sys.stdin)
        print(json.dumps(onboarding.find_companies(config, str(data.get("query") or ""), int(data.get("limit") or 50),
                                                   int(data.get("offset") or 0), str(data.get("which") or "all"))))
        return 0
    elif action == "track":
        data = json.load(sys.stdin)
        onboarding.set_tracked(config, list(data.get("names") or []), bool(data.get("tracked")))
    elif action == "add":
        data = json.load(sys.stdin)
        result = onboarding.add_company(config, str(data.get("name") or ""), str(data.get("url") or ""))
        if data.get("suggest", True) and result["status"] != "listed":  # for everyone's list, next version
            result["suggestion"] = suggest.suggest(config, result["name"], result.get("url") or str(data.get("url") or ""),
                                                   "added it to their own list" if result["status"] == "added"
                                                   else f"couldn't add it: {result.get('reason')}")
        print(json.dumps(result))
        return 0
    elif action == "suggest":
        data = json.load(sys.stdin)
        print(json.dumps(suggest.suggest(config, str(data.get("name") or ""), str(data.get("url") or ""),
                                         str(data.get("note") or ""))))
        return 0
    print(json.dumps(onboarding.show(config)))
    return 0


def cmd_checkin(args: argparse.Namespace) -> int:
    """The downloadable app's anonymous check-in, when it opens and every 6 hours (suggest.check_in)."""
    return 0 if suggest.check_in(context(args).config_path) else 1


def cmd_suggestions(args: argparse.Namespace) -> int:
    """The maintainer's view of the suggestions box (AWS credentials that can read its table)."""
    if args.action == "done":
        if not args.ids:
            raise ValueError("say which, e.g. role-radar suggestions done careers.example.com/jobs")
        suggest.mark_done(args.ids)
        print(f"Marked {len(args.ids)} as done.")
        return 0
    rows = suggest.list_suggestions(everything=args.all)
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("No suggestions waiting." if not args.all else "No suggestions yet.")
        return 0
    for row in rows:
        asked = row.get("requests", 1)
        print(f"{row.get('name')}  ({asked} asked{', done' if row.get('status') == 'done' else ''}; "
              f"last {row.get('last_at', '?')[:10]})")
        print(f"    id: {row['id']}")
        if row.get("url"):
            print(f"    {row['url']}")
        where = ", ".join(row.get("countries") or []) or "-"
        print(f"    for {', '.join(row.get('professions') or []) or '-'} in {where}")
        for note in (row.get("notes") or [])[-3:]:
            print(f"    note {note}")
    return 0


def _start_checker(ctx: Context) -> None:
    """Start `role-radar start` in the background: the login item on macOS (launchd), a process of its
    own on Windows (winchecker)."""
    if sys.platform == "darwin":
        if not launchd.start():
            _install_login_item(ctx)
    elif sys.platform == "win32":
        if not ctx.config_path:
            raise ValueError("the checker needs your config file: pass --config PATH")
        winchecker.start(ctx.config_path.resolve())
    else:
        raise ValueError("--start starts the checker on macOS and Windows; elsewhere, run role-radar start yourself")


def _wait_for_start(timeout: float = 10.0) -> None:
    """Give a just-started `role-radar start` a moment to take its lock, so the state shown includes it."""
    deadline = time.monotonic() + timeout
    while not InstanceLock().running_pid() and time.monotonic() < deadline:
        time.sleep(0.25)


def cmd_ui(args: argparse.Namespace) -> int:
    import webbrowser

    from role_radar import ui

    ctx = context(args)
    backend = open_backend(ctx.runtime, "ui", ctx.clients)
    server = ui.serve(backend, InstanceLock().running_pid, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Role Radar runner switches at {url} (Ctrl+C to quit)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def cmd_config_push(args: argparse.Namespace) -> int:
    """Upload the companies file with the profile applied, as one file, so Lambda needs only that."""
    ctx = context(args)
    path = Path(args.file).expanduser() if args.file else ctx.config_path
    if not path or not path.exists():
        raise FileNotFoundError("no local config file to push; pass --file PATH")
    profile = profile_path(path)
    raw = combined(path, profile)
    config = parse_raw(raw, str(path))  # never upload a broken file...
    require_filters(config, str(path))  # ...or one that would alert every job
    if not ctx.runtime.config_url:
        raise ValueError(
            f"nowhere to push to: set runtime.config_url in {profile} (or ROLE_RADAR_CONFIG_URL) "
            "to the ConfigUrl output of the SAM stack"
        )
    text = path.read_text(encoding="utf-8") if not profile.exists() else (
        f"# {path.name} with {profile.name} applied, uploaded by `role-radar config push`.\n"
        + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=120)
    )
    aws.S3Text(ctx.clients.s3, ctx.runtime.config_url).write(text)
    enabled = sum(c.checked for c in config.companies)
    print(
        f"Uploaded {path}{' with ' + str(profile) if profile.exists() else ''} to {ctx.runtime.config_url}: "
        f"{len(config.companies)} companies, {enabled} enabled. "
        "A running laptop app picks it up within 5 minutes, Lambda at its next run."
    )
    return 0


def cmd_config_pull(args: argparse.Namespace) -> int:
    """Write profile.yaml back from the pushed copy (e.g. on a new Mac)."""
    ctx = context(args)
    url = args.url or ctx.runtime.config_url
    if not url:
        raise ValueError("where from? pass --url s3://BUCKET/companies.yaml (the ConfigUrl output of the SAM stack)")
    if not ctx.config_path:
        raise ValueError("no local companies file to put the profile beside; pass --config PATH")
    runtime = replace(ctx.runtime, config_url=url)
    raw = yaml.safe_load(aws.S3Text(AwsClients(runtime).s3, url).read())
    profile = split_profile(raw if isinstance(raw, dict) else {})
    if not profile:
        raise ValueError(f"{url} has no runtime or filters to restore")
    target = profile_path(ctx.config_path)
    text = (f"# Restored from {url} by `role-radar config pull`. Your own settings: not in git.\n"
            + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=120))
    if target.exists() and target.read_text(encoding="utf-8") != text and not args.force:
        raise ValueError(f"{target} already exists; add --force to replace it")
    target.write_text(text, encoding="utf-8")
    print(f"Wrote {target} ({', '.join(k for k in PROFILE_SECTIONS if k in profile)}) from {url}.")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    """Alert settings in the Keychain: which are set (never their values), set one, or delete one."""
    from role_radar import keychain

    ctx = context(args)
    if ctx.runtime.secrets != "keychain":
        print(f"Note: runtime.secrets is {ctx.runtime.secrets!r}, so the Keychain isn't read. "
              "Set `secrets: keychain` under runtime: in your profile to use it.", file=sys.stderr)
    if args.action == "set":
        if args.value is None and not sys.stdin.isatty():
            raise ValueError(f"type the value in a terminal (role-radar secrets set {args.name}), or pass it after the name")
        keychain.write(args.name, args.value)
        where = "Windows Credential Manager" if sys.platform == "win32" else "the Keychain"
        print(f"Saved {args.name} in {where}. A running checker uses it for its next alert.")
    elif args.action == "delete":
        print(f"Deleted {args.name}." if keychain.delete(args.name) else f"{args.name} wasn't set.")
    else:
        stored = keychain.read_all()
        for name in keychain.NAMES:
            print(f"{name:20} {'set' if name in stored else '-'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from role_radar.diagnostics import diagnose

    ctx = context(args)
    runtime = replace(ctx.runtime, profile=args.profile or ctx.runtime.profile, region=args.region or ctx.runtime.region)
    checks = diagnose(runtime, ctx.config_path, AwsClients(runtime), args.stack)
    for check in checks:
        print(f"{'OK' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    return 0 if all(c.ok for c in checks) else 1


def cmd_notifications_test(args: argparse.Namespace) -> int:
    from role_radar.models import JobPosting
    from role_radar.notifications import ConsoleNotifier, notify_all

    ctx = context(args)
    channels = [n for n in NotifierSource(ctx.runtime, ctx.clients)() if not isinstance(n, ConsoleNotifier)]
    if args.channel:
        channels = [n for n in channels if n.name == args.channel]
    if not channels:
        raise ValueError("No requested notification channel configured; run role-radar doctor")
    sample = JobPosting(company="Role Radar", title="[TEST] Notification delivery check", source="test",
                        job_id="notification-test", url="https://example.com/role-radar-notification-test",
                        location="Test message; no job was discovered")
    ok = asyncio.run(notify_all(channels, [sample]))
    print("Test delivered to all selected channels." if ok else "Test failed for one or more channels; see the errors above.")
    return 0 if ok else 1


def open_store_spec(spec: str, ctx: Context, holder: str) -> tuple[StateStore, Lease | None]:
    """json:PATH or dynamodb:TABLE (either part after the colon defaults to the runtime settings)."""
    kind, _, where = spec.partition(":")
    if kind == "json":
        return JsonStateStore(Path(where).expanduser() if where else ctx.runtime.state_path), None
    if kind == "sqlite":
        from role_radar.sqlite import SqliteStateStore

        return SqliteStateStore(Path(where).expanduser() if where else replace(ctx.runtime, storage="sqlite").state_path), None
    if kind == "dynamodb":
        table = where or ctx.runtime.table
        if not table:
            raise ValueError("dynamodb: needs a table name, e.g. dynamodb:role-radar")
        from role_radar.dynamo import DynamoLease, DynamoStateStore

        lease = DynamoLease(ctx.clients.dynamodb, table, holder)
        return DynamoStateStore(ctx.clients.dynamodb, table, lease), lease
    raise ValueError(f"{spec!r}: expected json:PATH or dynamodb:TABLE")


def cmd_migrate(args: argparse.Namespace) -> int:
    ctx = context(args)
    holder = f"migrate:{hostname()}"
    source, _ = open_store_spec(args.source, ctx, holder)
    target, lease = open_store_spec(args.target, ctx, holder)
    state = source.load()
    jobs = sum(len(j) for j in state.companies.values())
    if lease and not lease.acquire(180):
        info = lease.read()
        print(f"{info.holder if info else 'Another runner'} has the lease on the target. Quit it (role-radar stop) and try again.", file=sys.stderr)
        return EXIT_LEASE_HELD
    try:
        # Held and renewed for as long as the copy takes; every company's write is fenced on it.
        with LeaseKeeper(lease) if lease else contextlib.nullcontext():
            existing = target.load()
            if (existing.companies or existing.meta) and not args.force:
                print(f"{args.target} already has state for {len(existing.companies or existing.meta)} companies; add --force to merge into it", file=sys.stderr)
                return 1
            target.save(state)
    finally:
        if lease:
            lease.release()
    print(f"Copied {len(state.companies)} companies ({jobs} jobs, {len(state.meta)} schedules) from {args.source} to {args.target}.")
    return 0


def cmd_login_item(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise ValueError("login items are macOS-only (on Linux, run `role-radar start` from a systemd user service)")
    if args.state == "off":
        print("Login item removed." if launchd.uninstall() else "The login item wasn't on.")
        return 0
    path = _install_login_item(context(args))
    log_file = launchd.log_file()
    print(
        f"Login item on ({path}). role-radar start is running now. The menu bar app starts it when it opens "
        f"and stops it when it quits.\nLog: {log_file}. `role-radar login-item off` removes it."
    )
    return 0


def _install_login_item(ctx: Context) -> Path:
    """Install and load the LaunchAgent, and start `role-radar start` now."""
    if not ctx.config_path:
        raise ValueError("the login item needs your config file: pass --config PATH")
    if not ctx.runtime.ssm_path and ctx.runtime.secrets != "keychain":
        raise ValueError(
            "the login item can't see DISCORD_WEBHOOK_URL / SMTP_* from your shell, so its alerts would "
            "only reach a log file. Keep them in the Keychain (runtime.secrets: keychain, then role-radar secrets set "
            "NAME) or in SSM Parameter Store (runtime.secrets: ssm:/role-radar/)"
        )
    config_path = ctx.config_path.resolve()
    log_file = launchd.log_file()
    program = [sys.executable, "-m", "role_radar", "start", "--config", str(config_path), "--log-file", str(log_file)]
    return launchd.install(program, launchd.passed_env(), working_dir=config_path.parent)


# -- output helpers -------------------------------------------------------------


def describe(runtime: RuntimeSettings) -> str:
    if runtime.storage == "dynamodb":
        return f"DynamoDB table {runtime.table}" + (f" ({runtime.region})" if runtime.region else "")
    if runtime.storage == "sqlite":
        return f"this Mac: {runtime.state_path}"
    return f"JSON file {runtime.state_path}"


def describe_switches(switches: dict[str, bool], runtime: RuntimeSettings | None = None) -> str:
    """Every switch, leaving out Lambda's without AWS."""
    names = [n for n in SWITCHES if n != "lambda" or not runtime or runtime.storage == "dynamodb"]
    return ", ".join(f"{name} {'on' if switch_on(switches, name) else 'OFF'}" for name in names)


def describe_lease(lease: Lease, runtime: RuntimeSettings) -> str:
    if runtime.storage != "dynamodb":
        return "not needed (state is on this Mac)"
    info = lease.read()
    now = time.time()
    if not info or not info.holder:
        return "free: Lambda takes it at its next run"
    request = f"; {info.requested_by} asked for it {when(info.requested_at)}" if info.requested_by else ""
    if info.held(now):
        return f"held by {info.holder} (epoch {info.epoch}), expires {when(info.expires_at)}{request}"
    return f"free since {when(info.expires_at)}, last held by {info.holder}{request}"


def when(value: str | float | datetime | None) -> str:
    """Local time plus how long ago / from now, e.g. 'Sat 22:41 (3m ago)'."""
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        value = from_iso(value)
    elif isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    delta = (value - utcnow()).total_seconds()
    if abs(delta) < 1:
        rel = "just now"
    else:
        rel = f"in {duration(delta)}" if delta > 0 else f"{duration(-delta)} ago"
    return f"{value.astimezone():%a %H:%M} ({rel})"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def setup_logging(verbose: bool, log_file: str | None = None) -> None:
    if log_file:
        Path(log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(Path(log_file).expanduser(), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs request URLs (webhook URLs are secrets); botocore at DEBUG logs
    # response bodies, which would include decrypted SSM parameters.
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="local companies file (default: ./config/companies.yaml or $ROLE_RADAR_CONFIG_FILE)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging (shows why each job matched or not)")

    parser = argparse.ArgumentParser(prog="role-radar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"role-radar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("start", parents=[common], help="run until quit, taking over from Lambda")
    p.add_argument("--log-file", help="log to this file (rotated at 5 MB) instead of the terminal")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", parents=[common], help="ask a running `start` to finish up and quit")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", parents=[common], help="lease holder, last passes, companies due, recent alerts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", parents=[common], help="read-only checks of setup and automation; never sends alerts")
    p.add_argument("--stack", help="SAM stack to inspect, usually role-radar")
    p.add_argument("--profile", help="AWS profile with deployment read access")
    p.add_argument("--region", help="AWS region of the stack")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("notifications", help="verify notification delivery")
    alerts_sub = p.add_subparsers(dest="action", required=True)
    test = alerts_sub.add_parser("test", parents=[common], help="send a labeled test alert without changing job state")
    test.add_argument("--channel", choices=["discord", "email"], help="test only this channel (default: all)")
    test.set_defaults(func=cmd_notifications_test)

    p = sub.add_parser("run", parents=[common], help="one pass over the due companies (needs --once)")
    p.add_argument("--once", action="store_true", required=True, help="run one pass and exit (`start` keeps running)")
    p.add_argument("--all", action="store_true", help="check every company, not just the due ones")
    p.add_argument("--company", action="append", default=[], metavar="NAME", help="only this company, due or not (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them; save nothing, take no lease")
    p.add_argument("--baseline", action="store_true", help="record every company's current jobs as seen, without alerting")
    p.add_argument("--local-config", action="store_true", help="read companies from the local file, not runtime.config_url")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list-matches", parents=[common], help="print every job matching right now")
    p.add_argument("--company", action="append", default=[], metavar="NAME", help="only this company (repeatable)")
    p.set_defaults(func=cmd_list_matches)

    p = sub.add_parser("switch", parents=[common], help="turn a runner (laptop, Lambda) or alert channel (Discord, email) on or off")
    p.add_argument("name", nargs="?", choices=SWITCHES)
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p.add_argument("--start", action="store_true",
                   help="if the laptop is switched on, make sure role-radar start is running (in the background: "
                        "the login item on macOS, a process of its own on Windows)")
    p.add_argument("--json", action="store_true", help="print the switches, who's checking and last passes as JSON")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("matches", parents=[common], help="matches waiting to be sent, skipped and sent; skip one or send now")
    p.add_argument("action", nargs="?", choices=["skip", "unskip", "send"],
                   help="skip: record a waiting match without ever sending it (unskip puts it back while it's "
                        "listed, 7 days); send: send matches now, alerts on or off (without a match, all that are waiting)")
    p.add_argument("company", nargs="?", help="the match's company, as listed")
    p.add_argument("uid", nargs="?", help="the match's id, as listed")
    p.add_argument("--pick", nargs=2, action="append", metavar=("COMPANY", "UID"),
                   help="a match to skip, unskip or send; give it once for each")
    p.add_argument("--stdin", action="store_true",
                   help='read the matches from stdin as JSON, [["COMPANY", "UID"], ...]: for more than --pick can take '
                        "(an app launching this passes at most 4,096 arguments)")
    p.add_argument("--all", action="store_true", help="skip or send every waiting match, or unskip every skipped one")
    p.add_argument("--json", action="store_true", help="print the lists as JSON (what the menu bar app reads)")
    p.set_defaults(func=cmd_matches)

    p = sub.add_parser("setup", parents=[common], help="the packaged app's first-run setup (JSON in and out)")
    p.add_argument("action", nargs="?",
                   choices=["init", "show", "profession", "profile", "prompt", "companies", "email", "discord",
                            "recipients", "find", "track", "add", "suggest"],
                   help="init: create the files; profession: pick one (JSON on stdin); profile: save titles, countries "
                        "etc. (JSON on stdin); prompt: print the AI prompt; "
                        "companies: add companies from the AI's answer (stdin); email: save Gmail settings (JSON on stdin); "
                        "discord: save a Discord webhook (JSON on stdin); "
                        "recipients: who else gets the alerts (JSON on stdin); "
                        'find: search the companies ({"query", "limit", "offset", "which": "off"}); '
                        'track: turn companies on or off ({"names": [...], "tracked": false}); '
                        'add: track a company they want ({"name", "url"}; also suggested for everyone\'s list '
                        'unless "suggest": false); '
                        'suggest: ask for a company on everyone\'s list ({"name", "url", "note"})')
    p.add_argument("--replace", action="store_true", help="companies: replace the list instead of adding to it")
    p.add_argument("--no-check", action="store_true", help="companies: don't read each new job board once first")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("checkin", parents=[common],
                       help="the downloadable app's anonymous check-in, for the user counts on the GitHub page")
    p.set_defaults(func=cmd_checkin)

    p = sub.add_parser("suggestions", parents=[common],
                       help="the companies people asked for in the app (the maintainer's AWS credentials)")
    p.add_argument("action", nargs="?", choices=["list", "done"], default="list",
                   help="list: what's waiting, most asked for first; done: mark some as dealt with")
    p.add_argument("ids", nargs="*", help="done: the suggestions' ids, as listed")
    p.add_argument("--all", action="store_true", help="list: those marked done too")
    p.add_argument("--json", action="store_true", help="list: as JSON")
    p.set_defaults(func=cmd_suggestions)

    p = sub.add_parser("ui", parents=[common], help="a local page with on/off switches for each runner")
    p.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765; 0 picks a free one)")
    p.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("config", parents=[common], help="manage the shared companies file")
    config_sub = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    push = config_sub.add_parser("push", parents=[common], help="validate the local file and upload it to runtime.config_url")
    push.add_argument("--file", help="companies file to upload (default: the local config file); its profile is applied")
    push.set_defaults(func=cmd_config_push)
    pull = config_sub.add_parser("pull", parents=[common], help="restore profile.yaml from the copy `config push` uploaded")
    pull.add_argument("--url", help="the pushed copy, s3://BUCKET/companies.yaml (default: runtime.config_url)")
    pull.add_argument("--force", action="store_true", help="replace an existing profile.yaml")
    pull.set_defaults(func=cmd_config_pull)

    p = sub.add_parser("secrets", parents=[common],
                       help="alert settings in this computer's keychain (runtime.secrets: keychain)")
    secrets_sub = p.add_subparsers(dest="action", metavar="ACTION")
    s_set = secrets_sub.add_parser("set", parents=[common], help="save one (without VALUE: typed hidden, twice)")
    s_set.add_argument("name", metavar="NAME", help="EMAIL_TO, SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, DISCORD_WEBHOOK_URL...")
    s_set.add_argument("value", nargs="?", metavar="VALUE", help="leave out for passwords, so they're never on a command line")
    s_delete = secrets_sub.add_parser("delete", parents=[common], help="remove one")
    s_delete.add_argument("name", metavar="NAME")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("migrate", parents=[common], help="copy state between stores, e.g. DynamoDB → this Mac (sqlite)")
    p.add_argument("--from", dest="source", required=True, metavar="SPEC", help="sqlite:PATH, dynamodb:TABLE or json:PATH")
    p.add_argument("--to", dest="target", required=True, metavar="SPEC", help="sqlite:PATH, dynamodb:TABLE or json:PATH")
    p.add_argument("--force", action="store_true", help="merge into a target that already has state")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("login-item", parents=[common], help="start `role-radar start` at login (macOS)")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_login_item)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, getattr(args, "log_file", None))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except (ValueError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except Exception as exc:  # AWS errors, missing credentials...: say what failed, not a traceback
        if args.verbose:
            log.exception("%s failed", args.command)
        else:
            log.error("%s failed: %s (add -v for details)", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
