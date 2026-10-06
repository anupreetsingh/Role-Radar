"""The shared code's Windows parts: the checker as a process of its own, stopping it without SIGTERM,
and alert settings in Windows Credential Manager. Most run anywhere, with Windows stood in for; those
marked `windows_only` use the real thing (the CI's Windows job)."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

from role_radar import cli, keychain, suggest, winchecker
from role_radar.instance import InstanceLock

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


# -- stopping the checker ------------------------------------------------------------


def test_stop_asks_with_a_file_on_windows(tmp_path, monkeypatch):
    lock = InstanceLock(tmp_path / "start.pid")
    pids = iter([4242, 4242, None, None])
    monkeypatch.setattr(InstanceLock, "running_pid", lambda self: next(pids))
    monkeypatch.setattr("role_radar.instance.sys.platform", "win32")
    monkeypatch.setattr("role_radar.instance.os.kill", lambda *a: pytest.fail("Windows has no SIGTERM"))
    assert lock.stop_running(timeout=5) == 4242
    assert lock.stop_file == tmp_path / "start.stop" and lock.stop_file.exists()
    assert lock.stop_requested() and not lock.stop_requested()  # taken once


def test_a_new_instance_ignores_an_old_stop_request(tmp_path):
    lock = InstanceLock(tmp_path / "start.pid")
    lock.stop_file.touch()  # left by a stop that came after the last instance had gone
    assert lock.acquire()
    try:
        assert not lock.stop_requested()
    finally:
        lock.release()


def test_the_running_instance_quits_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_RADAR_HOME", str(tmp_path))
    asked = []

    async def watch() -> None:
        watcher = asyncio.create_task(cli._watch_for_stop(lambda: asked.append(1), every=0.01))
        InstanceLock().stop_file.touch()
        while not asked:
            await asyncio.sleep(0.01)
        watcher.cancel()

    asyncio.run(asyncio.wait_for(watch(), 5))
    assert asked == [1] and not InstanceLock().stop_file.exists()


@windows_only
def test_instance_lock_on_windows(tmp_path):
    first, second = InstanceLock(tmp_path / "start.pid"), InstanceLock(tmp_path / "start.pid")
    assert first.acquire() and not second.acquire()
    assert second.running_pid() == os.getpid()  # the PID can be read while it's locked
    first.release()
    assert second.running_pid() is None


# -- starting it -----------------------------------------------------------------------


def test_the_checker_runs_windowless_on_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_RADAR_HOME", str(tmp_path / "home"))
    python = tmp_path / "python.exe"
    python.touch()
    (tmp_path / "pythonw.exe").touch()
    monkeypatch.setattr(winchecker.sys, "executable", str(python))
    started = []

    def popen(program, **options):
        if options["creationflags"] & winchecker.CREATE_BREAKAWAY_FROM_JOB:
            raise OSError("access denied")  # the app's job doesn't allow it
        started.append((program, options))

    monkeypatch.setattr(winchecker.subprocess, "Popen", popen)
    config = tmp_path / "companies.yaml"
    winchecker.start(config)
    ((program, options),) = started
    assert program[0] == str(tmp_path / "pythonw.exe")  # no console window
    assert program[1:6] == ["-m", "role_radar", "start", "--config", str(config)]
    assert program[-2:] == ["--log-file", str(tmp_path / "home" / "checker.log")]
    assert options["creationflags"] == winchecker.FLAGS and options["cwd"] == str(tmp_path)


def test_the_apps_own_python_is_used_as_is(monkeypatch):
    monkeypatch.setattr(winchecker.sys, "executable", r"C:\Programs\Role Radar\Role Radar.exe")
    assert winchecker.program(Path("c.yaml"))[0] == r"C:\Programs\Role Radar\Role Radar.exe"


def test_switch_start_starts_the_windows_checker(tmp_path, monkeypatch):
    config = tmp_path / "companies.yaml"
    config.write_text("runtime:\n  storage: sqlite\n  state_file: %s\ncompanies: []\n" % (tmp_path / "state.db"))
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.launchd, "start", lambda: pytest.fail("not launchd on Windows"))
    started = []
    monkeypatch.setattr(winchecker, "start", lambda path: started.append(path))
    monkeypatch.setattr(cli, "_wait_for_start", lambda: None)
    monkeypatch.setattr(InstanceLock, "running_pid", lambda self: None)
    assert cli.main(["switch", "--start", "--json", "--config", str(config)]) == 0
    assert started == [config.resolve()]


# -- alert settings in Credential Manager ------------------------------------------------


def test_settings_go_to_credential_manager_on_windows(monkeypatch):
    from role_radar import wincred

    saved: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(keychain.sys, "platform", "win32")
    monkeypatch.setattr(keychain.subprocess, "run", lambda *a, **k: pytest.fail("no `security` on Windows"))
    monkeypatch.setattr(wincred, "read", lambda target: saved.get(target, (None,))[0])
    monkeypatch.setattr(wincred, "write", lambda target, value, user="": saved.__setitem__(target, (value, user)))
    monkeypatch.setattr(wincred, "delete", lambda target: saved.pop(target, None) is not None)
    monkeypatch.setenv("ROLE_RADAR_KEYCHAIN", "Role Radar")

    keychain.write("SMTP_PASSWORD", "abcdabcdabcdabcd")
    assert saved == {"Role Radar/SMTP_PASSWORD": ("abcdabcdabcdabcd", "SMTP_PASSWORD")}
    assert keychain.read_all() == {"SMTP_PASSWORD": "abcdabcdabcdabcd"}
    keychain.write("EMAIL_TO", 'quotes "and" backslashes \\ are fine here')
    with pytest.raises(ValueError, match="line breaks"):
        keychain.write("EMAIL_TO", "two\nlines")
    assert keychain.delete("SMTP_PASSWORD") and not keychain.delete("SMTP_PASSWORD")


@windows_only
def test_credential_manager_round_trip():
    from role_radar import wincred

    target = f"Role Radar Test/{uuid.uuid4().hex}"
    assert wincred.read(target) is None
    try:
        wincred.write(target, "https://discord.com/api/webhooks/1/é-ok", user="DISCORD_WEBHOOK_URL")
        assert wincred.read(target) == "https://discord.com/api/webhooks/1/é-ok"
        wincred.write(target, "replaced")
        assert wincred.read(target) == "replaced"
    finally:
        assert wincred.delete(target)
    assert not wincred.delete(target)


# -- the check-in -----------------------------------------------------------------------------


def test_check_in_names_the_system(monkeypatch):
    monkeypatch.setattr(suggest.sys, "platform", "win32")
    monkeypatch.setattr(suggest.platform, "version", lambda: "10.0.26100")
    assert suggest.os_version() == "Windows 10.0.26100"
    monkeypatch.setattr(suggest.sys, "platform", "darwin")
    monkeypatch.setattr(suggest.platform, "mac_ver", lambda: ("15.5", ("", "", ""), "arm64"))
    assert suggest.os_version() == "15.5"
