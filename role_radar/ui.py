"""`role-radar ui`: a local page with on/off switches for the laptop and Lambda runners.

Serves on 127.0.0.1 only. The page reads and writes the same switches as
`role-radar switch`, in the shared state store, so a change reaches a running
laptop app within a minute and Lambda at its next run. `snapshot()` is also
what the menu bar app reads (`role-radar switch --json`), alert switches included,
and `live()` what its Live Tracking window reads (`role-radar matches --json`).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from role_radar.backends import Backend
from role_radar.storage import (
    RUNNERS, SWITCHES, QueuedMatch, StateStore, alerts_off, from_iso, hour_start, stats_hour, switch_on, to_iso, utcnow,
)

log = logging.getLogger(__name__)

# Browsers can't add this header to a cross-site request without a CORS preflight,
# which this server never approves, so other sites can't flip the switches.
WRITE_HEADER = "X-Role-Radar"


ACTIVITY_HOURS = 24
PASS_FIELDS = ("finished_at", "checked", "failed", "seconds", "failing", "pending")
SENT_ROWS = 150  # alert log rows Live Tracking reads: the latest alerts, each with its jobs
ROUND_STALE = timedelta(minutes=2)  # a round not updated for this long was cut short (the runner quit)


def snapshot(backend: Backend, laptop_pid: Callable[[], int | None], now: datetime | None = None,
             storage: str = "dynamodb") -> dict[str, Any]:
    """What the menu bar app shows. `storage`: with anything but dynamodb there's no Lambda."""
    stored = backend.store.load_switches()
    switches = {name: switch_on(stored, name) for name in SWITCHES}
    lease = backend.lease.read()
    holder = lease.holder if lease and lease.holder and lease.held(time.time()) else None
    pid = laptop_pid()
    runs = backend.store.last_runs()
    latest = max(runs.items(), key=lambda run: run[1].get("finished_at", ""), default=None)
    return {
        "switches": switches,
        "checking": checking(switches, holder, pid, lambda_too=storage == "dynamodb"),
        "storage": storage,
        "lease_holder": holder,
        "laptop_app_pid": pid,
        "last_runs": runs,
        "activity": activity(backend.store, now or utcnow()),
        "latest_pass": {"runner": latest[0].split(":", 1)[0], **{k: latest[1].get(k) for k in PASS_FIELDS}} if latest else None,
        "next_digest": backend.store.load_digest().next_send_at,
        "alerts_off": alerts_off(stored),
        "waiting": sum(1 for m in backend.store.load_queue() if not m.seen),
        "round": latest_round(backend.store.load_rounds(), now or utcnow()),
    }


def live(store: StateStore, now: datetime | None = None) -> dict[str, Any]:
    """Live Tracking: matches waiting to be sent (newest first), skipped, and the alerts sent; the round in progress."""
    queue = store.load_queue()
    digest = store.load_digest()
    waiting = [m for m in queue if not m.seen]
    skipped = [m for m in queue if m.seen]
    return {
        "waiting": [_match(m) for m in sorted(waiting, key=lambda m: (m.first_seen, m.company, m.uid), reverse=True)],
        "skipped": [_match(m) for m in sorted(skipped, key=lambda m: m.skipped_at or m.done_at or "", reverse=True)],
        "sent": sent_alerts(store.recent_alerts(SENT_ROWS), SENT_ROWS),
        "round": latest_round(store.load_rounds(), now or utcnow()),
        "next_digest": digest.next_send_at,
        "send_requested": digest.requested,
        "alerts_off": alerts_off(store.load_switches()),
    }


def sent_alerts(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The alert log (newest first) as alerts: when each went out, by whom, and its jobs by company.

    A digest's jobs share one notified_at, and are listed newest first, as the
    digest lists them (rows logged before first_seen was kept come last, by company).
    When the log holds more than `limit` rows, the oldest alert read may be missing
    jobs, so it's left out.
    """
    alerts: dict[str, dict[str, Any]] = {}
    for row in rows:
        alert = alerts.setdefault(row["notified_at"], {"sent_at": row["notified_at"],
                                                       "by": (row.get("by") or "").split(":", 1)[0] or None, "jobs": []})
        alert["jobs"].append({k: row.get(k) for k in ("company", "title", "location", "url", "first_seen")})
    out = list(alerts.values())
    if len(rows) >= limit and len(out) > 1:
        out.pop()
    for alert in out:
        alert["jobs"].sort(key=lambda job: ((job["company"] or "").lower(), (job["title"] or "").lower()))
        alert["jobs"].sort(key=lambda job: job["first_seen"] or "", reverse=True)
    return out


def _match(m: QueuedMatch) -> dict[str, Any]:
    return {"company": m.company, "uid": m.uid, "title": m.title, "location": m.location, "url": m.url,
            "first_seen": m.first_seen, "skipped_at": m.skipped_at, "final": bool(m.done_at), "send_at": m.send_at}


def latest_round(rounds: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The round updated last, by any runner. `stale`: it stopped reporting before it finished."""
    if not rounds:
        return None
    runner, info = max(rounds.items(), key=lambda item: item[1].get("updated_at", ""))
    updated = info.get("updated_at")
    stale = not info.get("finished_at") and (not updated or now - from_iso(updated) > ROUND_STALE)
    return {"runner": runner.split(":", 1)[0], **{k: info.get(k) for k in ("started_at", "updated_at", "finished_at", "total", "done")},
            "stale": stale}


def activity(store: StateStore, now: datetime) -> dict[str, Any]:
    """The last 24 hours, oldest hour first: checks per hour by the Mac and by Lambda, and totals.

    `run --once` (runner "cli") counts as the Mac: it runs on it.
    """
    hours = [stats_hour(now - timedelta(hours=h)) for h in range(ACTIVITY_HOURS - 1, -1, -1)]
    by_hour = {hour: {"start": to_iso(hour_start(hour)), "mac": 0, "lambda": 0} for hour in hours}
    totals = dict.fromkeys(("checked", "failed", "new_jobs", "matches", "alerts"), 0)
    for row in store.load_stats(hours[0]):
        if row["hour"] not in by_hour:
            continue
        by_hour[row["hour"]]["lambda" if row["runner"] == "lambda" else "mac"] += row.get("checked", 0)
        for name in totals:
            totals[name] += row.get(name, 0)
    return {"hours": list(by_hour.values()), **totals}


def checking(switches: dict[str, bool], lease_holder: str | None, laptop_pid: int | None,
             lambda_too: bool = True) -> str | None:
    """Which runner is checking sites: "laptop", "lambda", or None.

    Both on: the laptop while its app runs (it takes the lease from Lambda),
    otherwise Lambda. One on: that one, if it can run. Both off: nobody.
    Without AWS (`lambda_too` false) there's only the laptop.
    """
    if lease_holder and lambda_too:
        return lease_holder.split(":", 1)[0]
    if switches["laptop"] and laptop_pid:
        return "laptop"
    return "lambda" if switches["lambda"] and lambda_too else None


def make_handler(backend: Backend, laptop_pid: Callable[[], int | None]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, snapshot(backend, laptop_pid))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/switch" or self.headers.get(WRITE_HEADER) != "1":
                self._json(403, {"error": "forbidden"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                runner, on = body.get("runner"), body.get("on")
                if runner not in RUNNERS or not isinstance(on, bool):
                    raise ValueError(f"expected runner in {RUNNERS} and on: true/false")
                backend.store.save_switch(runner, on)
                log.info("Switched %s %s", runner, "on" if on else "off")
                self._json(200, snapshot(backend, laptop_pid))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # e.g. DynamoDB unreachable
                self._json(502, {"error": f"{type(exc).__name__}: {exc}"})

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # quiet the default per-request lines
            log.debug(format, *args)

    return Handler


def serve(backend: Backend, laptop_pid: Callable[[], int | None], port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(backend, laptop_pid))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Role Radar runners</title>
<style>
  :root { color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --text: #1b1f24; --muted: #5f6b7a;
          --line: #dde2e8; --on: #1f883d; --off: #9aa4b2; --warn: #b35900; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f1216; --card: #171b21; --text: #e6e9ee; --muted: #9aa4b2; --line: #2a313b;
            --on: #3fb950; --off: #4b5563; --warn: #e3a008; }
  }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 520px; margin: 48px auto; padding: 0 16px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 24px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: 16px 18px; margin-bottom: 12px; display: flex; align-items: center; gap: 16px; }
  .card .text { flex: 1; }
  .name { font-weight: 600; }
  .detail { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .detail.warn { color: var(--warn); }
  .switch { position: relative; width: 50px; height: 28px; flex: none; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; inset: 0; background: var(--off); border-radius: 28px;
            cursor: pointer; transition: background .15s; }
  .slider::before { content: ""; position: absolute; width: 22px; height: 22px; left: 3px; top: 3px;
                    background: #fff; border-radius: 50%; transition: transform .15s; }
  input:checked + .slider { background: var(--on); }
  input:checked + .slider::before { transform: translateX(22px); }
  input:focus-visible + .slider { outline: 2px solid var(--on); outline-offset: 2px; }
  input:disabled + .slider { opacity: .5; cursor: wait; }
  #lease, #error { color: var(--muted); font-size: 13px; margin-top: 16px; }
  #error { color: var(--warn); }
</style>
</head>
<body>
<main>
  <h1>Role Radar runners</h1>
  <p class="sub">Turn each runner on or off. The laptop app picks up a change within a minute; Lambda at its next run.</p>
  <div class="card">
    <div class="text"><div class="name">Laptop</div><div class="detail" id="laptop-detail">…</div></div>
    <label class="switch"><input type="checkbox" id="laptop" aria-label="Laptop runner"><span class="slider"></span></label>
  </div>
  <div class="card">
    <div class="text"><div class="name">Lambda</div><div class="detail" id="lambda-detail">…</div></div>
    <label class="switch"><input type="checkbox" id="lambda" aria-label="Lambda runner"><span class="slider"></span></label>
  </div>
  <div id="lease"></div>
  <div id="error"></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  return Math.round(s / 3600) + " h ago";
}
function lastRun(runs, prefix) {
  return Object.entries(runs).filter(([k]) => k.startsWith(prefix))
    .map(([, v]) => v.finished_at).sort().pop();
}
function render(s) {
  for (const r of ["laptop", "lambda"]) { $(r).checked = s.switches[r]; $(r).disabled = false; }
  const ld = $("laptop-detail");
  if (!s.switches.laptop) { ld.textContent = "Off: the laptop app idles and leaves checks to Lambda."; ld.className = "detail"; }
  else if (!s.laptop_app_pid) { ld.textContent = "On, but role-radar start isn't running on this Mac."; ld.className = "detail warn"; }
  else { ld.textContent = "On · app running (pid " + s.laptop_app_pid + ") · last pass " + ago(lastRun(s.last_runs, "laptop")); ld.className = "detail"; }
  $("lambda-detail").textContent = s.switches.lambda
    ? "On · runs every 5 minutes when the laptop isn't · last pass " + ago(lastRun(s.last_runs, "lambda"))
    : "Off: Lambda exits at once without checking anything.";
  $("lease").textContent = s.checking === "laptop" ? "The Mac is checking sites."
    : s.checking === "lambda" ? "Lambda is checking sites." : "Nothing is checking sites.";
  $("error").textContent = "";
}
async function load() {
  try { const r = await fetch("/api/state"); render(await r.json()); }
  catch (e) { $("error").textContent = "Couldn't load state: " + e; }
}
async function flip(runner) {
  const box = $(runner); box.disabled = true;
  try {
    const r = await fetch("/api/switch", { method: "POST", headers: { "Content-Type": "application/json", "X-Role-Radar": "1" },
                                           body: JSON.stringify({ runner, on: box.checked }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
    render(data);
  } catch (e) { box.checked = !box.checked; box.disabled = false; $("error").textContent = "Couldn't switch " + runner + ": " + e.message; }
}
$("laptop").addEventListener("change", () => flip("laptop"));
$("lambda").addEventListener("change", () => flip("lambda"));
load(); setInterval(load, 15000);
</script>
</body>
</html>
"""
