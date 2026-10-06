// How the app puts things into words, as the Mac app does: times ("2:32 PM (12 min. ago)"), counts,
// how far the round has got, and the status lines the panel and Live Tracking both show (the Mac
// app's Health).
using System.Globalization;

namespace RoleRadar.Core;

public static class Describe
{
    public const string Who = "This PC"; // the checker on this computer: the Mac app's "The Mac"

    private static readonly CultureInfo English = CultureInfo.GetCultureInfo("en-US");

    /// <summary>The time now: the tests set it.</summary>
    public static Func<DateTimeOffset> Now { get; set; } = () => DateTimeOffset.Now;

    /// <summary>An ISO 8601 time from role-radar, in local time.</summary>
    public static DateTimeOffset? Date(string? text) =>
        !string.IsNullOrEmpty(text) && DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var when)
            ? when.ToLocalTime() : null;

    /// <summary>"2:32 PM".</summary>
    public static string Clock(DateTimeOffset when) => when.ToString("h:mm tt", English);

    /// <summary>"Sep 29, 3:50 PM", always with the day.</summary>
    public static string Full(DateTimeOffset when) => when.ToString("MMM d, h:mm tt", English);

    /// <summary>"2:32 PM" today, "Sep 28, 2:32 PM" before.</summary>
    public static string Short(DateTimeOffset when) => when.Date == Now().Date ? Clock(when) : Full(when);

    /// <summary>"12 min. ago", "in 5 min.", "never".</summary>
    public static string Ago(DateTimeOffset? when)
    {
        if (when is null)
        {
            return "never";
        }
        var seconds = (Now() - when.Value).TotalSeconds;
        var later = seconds < 0;
        seconds = Math.Abs(seconds);
        string text;
        if (seconds >= 7 * 86400) text = $"{(int)(seconds / (7 * 86400))} wk.";
        else if (seconds >= 86400) text = Plural((int)(seconds / 86400), "day");
        else if (seconds >= 3600) text = $"{(int)(seconds / 3600)} hr.";
        else if (seconds >= 60) text = $"{(int)(seconds / 60)} min.";
        else text = $"{(int)seconds} sec.";
        return later ? $"in {text}" : $"{text} ago";
    }

    /// <summary>"2:32 PM (12 min. ago)".</summary>
    public static string Stamp(string? text) => Date(text) is { } when ? $"{Short(when)} ({Ago(when)})" : "";

    /// <summary>"1,234".</summary>
    public static string Number(int count) => count.ToString("N0", English);

    /// <summary>"950", "1.2K", "34K", "1.5M".</summary>
    public static string Compact(int count)
    {
        foreach (var (size, suffix) in new[] { (1_000_000, "M"), (1_000, "K") })
        {
            if (count >= size)
            {
                var value = count / (double)size;
                var text = value < 10 ? value.ToString("0.0", English) : value.ToString("0", English);
                return (text.EndsWith(".0") ? text[..^2] : text) + suffix;
            }
        }
        return count.ToString(English);
    }

    /// <summary>"1 job", "3 jobs".</summary>
    public static string Plural(int count, string word) => $"{Number(count)} {word}{(count == 1 ? "" : "s")}";

    /// <summary>"1 hr, 5 min", "25 min".</summary>
    public static string Took(TimeSpan time)
    {
        var minutes = (int)Math.Round(time.TotalMinutes);
        var (hours, rest) = (minutes / 60, minutes % 60);
        return hours == 0 ? $"{rest} min" : rest == 0 ? $"{hours} hr" : $"{hours} hr, {rest} min";
    }

    // -- the round -----------------------------------------------------------------------------------

    public static string RunnerName(string? runner) => runner == "lambda" ? "Lambda" : Who;

    /// <summary>Between 0 and 1 while the round is going.</summary>
    public static double? RoundFraction(RoundInfo? round)
    {
        if (round is null || round.FinishedAt is not null || round.Stale || round.Total is not > 0)
        {
            return null;
        }
        return Math.Min(1, (round.Done ?? 0) / (double)round.Total.Value);
    }

    public static string RoundSummary(RoundInfo? round)
    {
        if (round is null)
        {
            return "No rounds recorded yet";
        }
        var count = $"{Number(round.Done ?? 0)} of {Number(round.Total ?? 0)}";
        var (end, start) = (Date(round.FinishedAt), Date(round.StartedAt));
        if (end is { } finished)
        {
            var text = $"Last round finished {Short(finished)}";
            if (start is { } began && finished - began >= TimeSpan.FromMinutes(1))
            {
                text += ", took " + Took(finished - began);
            }
            return text + $" · {Number(round.Total ?? 0)} checks";
        }
        if (round.Stale)
        {
            return $"{RunnerName(round.Runner)}'s last round stopped at {count}";
        }
        var started = start is { } since ? $" · started {Short(since)}" : "";
        return $"{RunnerName(round.Runner)} is checking: {count}{started}";
    }

    // -- the status lines (the Mac app's Health) ------------------------------------------------------

    public static bool AlertsOff(RunnerState? state, LiveState? live)
    {
        if (live is not null)
        {
            return live.AlertsOff;
        }
        return state is not null && !state.Switches.GetValueOrDefault("discord") && !state.Switches.GetValueOrDefault("email");
    }

    /// <summary>How many new jobs wait, and when they go out.</summary>
    public static string WaitingLine(RunnerState? state, LiveState? live)
    {
        var count = live?.Waiting.Count ?? state?.Waiting ?? 0;
        var jobs = count == 0 ? "No new jobs" : Plural(count, "new job");
        if (AlertsOff(state, live))
        {
            return jobs + " in Live Tracking · alerts are off";
        }
        if (live?.SendRequested == true)
        {
            return $"{jobs} · sending now";
        }
        var next = Date(live?.NextDigest ?? state?.NextDigest);
        return count == 0 || next is null ? jobs : $"{jobs} · next alert {Short(next.Value)}";
    }

    /// <summary>Whether every site's last check worked: (text, all well), or null before the first pass.</summary>
    public static (string Text, bool Ok)? FailingLine(RunnerState? state)
    {
        if (state?.LatestPass?.Failing is not { } failing)
        {
            return null;
        }
        return failing == 0 ? ("Every site's last check worked", true)
            : ($"{Plural(failing, "site")} failing (see role-radar status)", false);
    }
}
