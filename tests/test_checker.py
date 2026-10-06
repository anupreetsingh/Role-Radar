"""The checker for real, as the apps run it: a process of its own that reads a job site (one served
here, on this computer), puts the jobs that match in Live Tracking, and stops when asked. On Windows it
starts the way the Windows app starts it (`switch --start`: winchecker, a detached process, the msvcrt
lock); elsewhere as `role-radar start`, which the Mac app's launchd agent runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from role_radar.instance import InstanceLock

PROJECT = Path(__file__).resolve().parent.parent


def posting(job_id: str, title: str, city: str) -> dict:
    return {"@type": "JobPosting", "title": title, "identifier": {"@type": "PropertyValue", "value": job_id},
            "url": f"/careers/{job_id}", "datePosted": "2026-10-01",
            "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": city,
                                                          "addressRegion": "CO", "addressCountry": "US"}}}


CAREERS = ("<!doctype html><html><head><title>Careers</title><script type=\"application/ld+json\">"
           + json.dumps({"@context": "https://schema.org", "@graph": [
               posting("SE-1", "Software Engineer", "Denver"),
               posting("SE-2", "Senior Software Engineer", "Boulder"),  # ruled out: senior
               posting("OM-3", "Office Manager", "Denver"),  # not a role they want
           ]})
           + "</script></head><body></body></html>").encode()


class CareersSite(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        body, status = (CAREERS, 200) if self.path.split("?")[0] == "/careers" else (b"not here", 404)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), CareersSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/careers"
    server.shutdown()


def role_radar(*args: str, config: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "role_radar", *args, "--config", str(config)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_the_checker_finds_matching_jobs_and_stops_when_asked(tmp_path, site, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))  # the checker runs from its config's folder
    home = Path(os.environ["ROLE_RADAR_HOME"])
    config = tmp_path / "companies.yaml"
    config.write_text(f"""
runtime:
  storage: sqlite
settings:
  check_interval_minutes: 30
  notify_on_first_run: true   # the jobs open at the first check count as new
defaults:
  filters:
    include_keywords: [engineer]
    exclude_keywords: [senior]
companies:
  - name: Acme
    url: {site}
    ats: generic
""", encoding="utf-8")
    for channel in ("email", "discord"):  # as the apps leave them with no alerts set up: matches wait in Live Tracking
        assert role_radar("switch", channel, "off", "--json", config=config).returncode == 0

    checker = None
    if sys.platform == "win32":
        started = role_radar("switch", "laptop", "on", "--start", "--json", config=config)
        assert started.returncode == 0, started.stderr
    else:
        checker = subprocess.Popen([sys.executable, "-m", "role_radar", "start", "--config", str(config)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock = InstanceLock(home / "start.pid")
    try:
        found: list[dict] = []
        deadline = time.monotonic() + 60
        while not found and time.monotonic() < deadline:
            time.sleep(1)
            reply = role_radar("matches", "--json", config=config)
            assert reply.returncode == 0, reply.stderr
            found = json.loads(reply.stdout)["waiting"]
        log = (home / "checker.log").read_text(encoding="utf-8") if (home / "checker.log").exists() else ""
        assert [(m["company"], m["title"]) for m in found] == [("Acme", "Software Engineer")], log
        assert lock.running_pid(), "the checker should still be running, waiting for its next check"

        stopped = role_radar("stop", config=config)
        assert stopped.returncode == 0, stopped.stderr
        assert lock.running_pid() is None
        if checker:
            assert checker.wait(timeout=30) == 0
    finally:
        if lock.running_pid():  # a failed test leaves no checker behind
            role_radar("stop", config=config)
        if checker and checker.poll() is None:
            checker.kill()
