"""role-radar: the command line.

  role-radar start               run until you quit (Ctrl+C): take the lease and check
                                 companies as they come due; Lambda covers while it's off
  role-radar stop                ask a running `start` to quit (while the menu bar app is
                                 open, it starts it again within a minute, on the current code)
  role-radar status              lease holder, last passes, companies due, recent alerts
  role-radar doctor              check config, secrets and AWS deployment without sending alerts
  role-radar notifications test  send a test to configured notification channels
  role-radar run --once          one pass over the due companies
                                 [--all] [--company NAME] [--dry-run] [--baseline]
  role-radar list-matches        print every job matching right now (no state, no alerts)
  role-radar switch laptop|lambda|discord|email on|off
                                 turn a runner or alert channel on or off (no arguments: show all)
  role-radar ui                  a local page with on/off switches for the laptop and Lambda
  role-radar config push         upload the local companies file to runtime.config_url
  role-radar migrate --from json:seen_jobs.json --to dynamodb:TABLE
  role-radar login-item on|off   set up the Mac's background checker (`role-radar start` under
                                 launchd), which the menu bar app starts and stops (macOS)

The local companies file is --config, else $ROLE_RADAR_CONFIG_FILE, else
./config/companies.yaml, else ~/.config/role-radar/companies.yaml. Its
`runtime:` section, overridden by ROLE_RADAR_* environment variables, says
where state, the pushed config and alert secrets live.
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
from typing import Any

from role_radar import __version__, aws, launchd
from role_radar.backends import AwsClients, ConfigSource, NotifierSource, open_backend, resolve_runtime
from role_radar.config import RuntimeSettings, parse_config
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
    """First Ctrl+C / SIGTERM / SIGHUP: finish what's in flight, then release. Second: quit now."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set() and task:
            log.warning("Quitting now")
            task.cancel()
        else:
            log.info("Finishing the companies in progress, then releasing the lease (Ctrl+C again to quit now)")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, on_signal)


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
    companies = [c for c in config.companies if c.enabled and (not only or c.name.lower() in only)]
    return asyncio.run(print_matches(companies, config.settings))


def cmd_status(args: argparse.Namespace) -> int:
    ctx = context(args)
    runtime = ctx.runtime
    backend = open_backend(runtime, "status", ctx.clients)
    source = ConfigSource(runtime, ctx.config_path, ctx.clients)
    config = source.load()
    now = utcnow()

    print(f"State:          {describe(runtime)}")
    print(f"Switches:       {describe_switches(backend.store.load_switches())}")
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

    enabled = [c for c in config.companies if c.enabled]
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
        same = ctx.config_path.read_text(encoding="utf-8") == source.read_text()
        note = "same as your local copy" if same else f"differs from {ctx.config_path}: run `role-radar config push`"
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
        if sys.platform != "darwin":
            raise ValueError("--start uses the macOS login item")
        if not launchd.start():
            _install_login_item(ctx)
        _wait_for_start()
    if args.json:
        state = ui.snapshot(backend, InstanceLock().running_pid)
        state["login_item"] = sys.platform == "darwin" and launchd.plist_path().exists()
        print(json.dumps(state))
        return 0
    switches = store.load_switches()
    print(f"Switches: {describe_switches(switches)}")
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
    ctx = context(args)
    path = Path(args.file).expanduser() if args.file else ctx.config_path
    if not path or not path.exists():
        raise FileNotFoundError("no local config file to push; pass --file PATH")
    text = path.read_text(encoding="utf-8")
    config = parse_config(text, str(path), as_json=path.suffix == ".json")  # never upload a broken file
    if not ctx.runtime.config_url:
        raise ValueError(
            f"nowhere to push to: set runtime.config_url in {path} (or ROLE_RADAR_CONFIG_URL) "
            "to the ConfigUrl output of the SAM stack"
        )
    aws.S3Text(ctx.clients.s3, ctx.runtime.config_url).write(text)
    enabled = sum(c.enabled for c in config.companies)
    print(
        f"Uploaded {path} to {ctx.runtime.config_url}: {len(config.companies)} companies, {enabled} enabled. "
        "A running laptop app picks it up within 5 minutes, Lambda at its next run."
    )
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
        return JsonStateStore(Path(where or ctx.runtime.state_file).expanduser()), None
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
    log_file = launchd.log_dir() / "role-radar.log"
    print(
        f"Login item on ({path}). role-radar start is running now. The menu bar app starts it when it opens "
        f"and stops it when it quits.\nLog: {log_file}. `role-radar login-item off` removes it."
    )
    return 0


def _install_login_item(ctx: Context) -> Path:
    """Install and load the LaunchAgent, and start `role-radar start` now."""
    if not ctx.config_path:
        raise ValueError("the login item needs your config file: pass --config PATH")
    if not ctx.runtime.ssm_path:
        raise ValueError(
            "the login item can't see DISCORD_WEBHOOK_URL / SMTP_* from your shell, so its alerts would "
            "only reach a log file. Put them in SSM Parameter Store and set runtime.secrets: ssm:/role-radar/"
        )
    config_path = ctx.config_path.resolve()
    log_file = launchd.log_dir() / "role-radar.log"
    program = [sys.executable, "-m", "role_radar", "start", "--config", str(config_path), "--log-file", str(log_file)]
    return launchd.install(program, launchd.passed_env(), working_dir=config_path.parent)


# -- output helpers -------------------------------------------------------------


def describe(runtime: RuntimeSettings) -> str:
    if runtime.storage == "dynamodb":
        return f"DynamoDB table {runtime.table}" + (f" ({runtime.region})" if runtime.region else "")
    return f"JSON file {runtime.state_file}"


def describe_switches(switches: dict[str, bool]) -> str:
    return ", ".join(f"{name} {'on' if switch_on(switches, name) else 'OFF'}" for name in SWITCHES)


def describe_lease(lease: Lease, runtime: RuntimeSettings) -> str:
    if runtime.storage != "dynamodb":
        return "not needed (JSON state is local to this machine)"
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
                   help="if the laptop is switched on, make sure role-radar start is running (via the login item)")
    p.add_argument("--json", action="store_true", help="print the switches, who's checking and last passes as JSON")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("ui", parents=[common], help="a local page with on/off switches for each runner")
    p.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765; 0 picks a free one)")
    p.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("config", parents=[common], help="manage the shared companies file")
    config_sub = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    push = config_sub.add_parser("push", parents=[common], help="validate the local file and upload it to runtime.config_url")
    push.add_argument("--file", help="file to upload (default: the local config file)")
    push.set_defaults(func=cmd_config_push)

    p = sub.add_parser("migrate", parents=[common], help="copy state between stores, e.g. JSON file → DynamoDB")
    p.add_argument("--from", dest="source", required=True, metavar="SPEC", help="json:PATH or dynamodb:TABLE")
    p.add_argument("--to", dest="target", required=True, metavar="SPEC", help="json:PATH or dynamodb:TABLE")
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
