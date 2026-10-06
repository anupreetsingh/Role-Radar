"""How the app puts things into words, as the Mac app does: times ("2:32 PM (12 min. ago)"), counts,
how far the round has got, and the status lines the panel and Live Tracking both show (Health)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

WHO = "This PC"  # the checker on this computer: the Mac app's "The Mac"


def date(text: str | None) -> datetime | None:
    """An ISO 8601 time from role-radar, in local time."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def now() -> datetime:
    return datetime.now().astimezone()


def clock(when: datetime) -> str:
    """"2:32 PM"."""
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def full(when: datetime) -> str:
    """"Sep 29, 3:50 PM", always with the day."""
    return f"{when:%b} {when.day}, {clock(when)}"


def short(when: datetime) -> str:
    """"2:32 PM" today, "Sep 28, 2:32 PM" before."""
    return clock(when) if when.date() == now().date() else full(when)


def ago(when: datetime | None) -> str:
    """"12 min. ago", "in 5 min.", "never"."""
    if when is None:
        return "never"
    seconds = (now() - when).total_seconds()
    later, seconds = seconds < 0, abs(seconds)
    for size, unit in ((7 * 86400, "wk."), (86400, "day"), (3600, "hr."), (60, "min.")):
        if seconds >= size:
            count = int(seconds // size)
            text = f"{count} {unit}" + ("s" if unit == "day" and count != 1 else "")
            break
    else:
        text = f"{int(seconds)} sec."
    return f"in {text}" if later else f"{text} ago"


def stamp(text: str | None) -> str:
    """"2:32 PM (12 min. ago)"."""
    when = date(text)
    return f"{short(when)} ({ago(when)})" if when else ""


def number(count: int) -> str:
    """"1,234"."""
    return f"{count:,}"


def compact(count: int) -> str:
    """"950", "1.2K", "34K", "1.5M"."""
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if count >= size:
            value = count / size
            text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return text.removesuffix(".0") + suffix
    return str(count)


def plural(count: int, word: str) -> str:
    """"1 job", "3 jobs"."""
    return f"{number(count)} {word}{'' if count == 1 else 's'}"


def took(seconds: float) -> str:
    """"1 hr, 5 min", "25 min"."""
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return ", ".join(part for part in (f"{hours} hr" if hours else "", f"{minutes} min" if minutes or not hours else "") if part)


# -- the round ---------------------------------------------------------------------------------


def runner_name(runner: str | None) -> str:
    return "Lambda" if runner == "lambda" else WHO


def round_fraction(info: dict[str, Any] | None) -> float | None:
    """Between 0 and 1 while the round is going."""
    if not info or info.get("finished_at") or info.get("stale") or not info.get("total"):
        return None
    return min(1.0, (info.get("done") or 0) / info["total"])


def round_summary(info: dict[str, Any] | None) -> str:
    if not info:
        return "No rounds recorded yet"
    count = f"{number(info.get('done') or 0)} of {number(info.get('total') or 0)}"
    end, start = date(info.get("finished_at")), date(info.get("started_at"))
    if end:
        text = f"Last round finished {short(end)}"
        if start and (end - start) >= timedelta(minutes=1):
            text += ", took " + took((end - start).total_seconds())
        return text + f" · {number(info.get('total') or 0)} checks"
    if info.get("stale"):
        return f"{runner_name(info.get('runner'))}'s last round stopped at {count}"
    started = f" · started {short(start)}" if start else ""
    return f"{runner_name(info.get('runner'))} is checking: {count}{started}"


# -- the status lines (the Mac app's Health) ---------------------------------------------------


def alerts_off(state: dict[str, Any] | None, live: dict[str, Any] | None) -> bool:
    if live is not None:
        return bool(live.get("alerts_off"))
    switches = (state or {}).get("switches") or {}
    return bool(state) and not switches.get("discord") and not switches.get("email")


def waiting_line(state: dict[str, Any] | None, live: dict[str, Any] | None) -> str:
    """How many new jobs wait, and when they go out."""
    count = len(live["waiting"]) if live else (state or {}).get("waiting") or 0
    jobs = "No new jobs" if count == 0 else plural(count, "new job")
    if alerts_off(state, live):
        return jobs + " in Live Tracking · alerts are off"
    if live and live.get("send_requested"):
        return f"{jobs} · sending now"
    when = date((live or {}).get("next_digest") or (state or {}).get("next_digest"))
    return jobs if count == 0 or not when else f"{jobs} · next alert {short(when)}"


def failing_line(state: dict[str, Any] | None) -> tuple[str, bool] | None:
    """Whether every site's last check worked: (text, all well), or None before the first pass."""
    failing = ((state or {}).get("latest_pass") or {}).get("failing")
    if failing is None:
        return None
    if failing == 0:
        return "Every site's last check worked", True
    return f"{plural(failing, 'site')} failing (see role-radar status)", False
