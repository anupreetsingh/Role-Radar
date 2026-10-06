// What role-radar's commands reply, as JSON: the shapes the Mac app decodes (RunnerState, LiveState,
// SetupState...), with the same names in snake_case. Fields the app doesn't use are left out.
using System.Text.Json;

namespace RoleRadar.Core;

public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    public static T? Read<T>(string text) => JsonSerializer.Deserialize<T>(text, Options);

    /// <summary>What a command takes on stdin: the names given, as they are.</summary>
    public static string Write(object value) => JsonSerializer.Serialize(value);
}

/// <summary>`role-radar switch --json`: the switches, who's checking, the round, the last 24 hours.</summary>
public sealed class RunnerState
{
    public Dictionary<string, bool> Switches { get; set; } = new();
    public string? Checking { get; set; }
    public int? LaptopAppPid { get; set; }
    public Dictionary<string, RunInfo> LastRuns { get; set; } = new();
    public Activity? Activity { get; set; }
    public PassInfo? LatestPass { get; set; }
    public string? NextDigest { get; set; }
    public int? Waiting { get; set; } // matches waiting to be sent, not counting skipped ones
    public RoundInfo? Round { get; set; }
    public string? Storage { get; set; }
}

public sealed class RunInfo
{
    public string? FinishedAt { get; set; }
    public int? Checked { get; set; }
}

/// <summary>The last 24 hours, from each pass's counts (oldest hour first).</summary>
public sealed class Activity
{
    public List<ActivityHour> Hours { get; set; } = new();
    public int Checked { get; set; }
    public int Failed { get; set; }
    public int NewJobs { get; set; }
    public int Matches { get; set; }
    public int Alerts { get; set; }
}

public sealed class ActivityHour
{
    public string Start { get; set; } = "";
    public int Mac { get; set; } // the laptop's checks: this PC's, on Windows
    public int Lambda { get; set; }
}

/// <summary>The latest pass by any runner, with what it left behind.</summary>
public sealed class PassInfo
{
    public string? Runner { get; set; }
    public string? FinishedAt { get; set; }
    public int? Failing { get; set; } // sites whose last check failed
    public int? Pending { get; set; } // matches not sent yet
}

/// <summary>How far the latest round of checks has got, by whichever runner ran it.</summary>
public sealed class RoundInfo
{
    public string? Runner { get; set; }
    public string? StartedAt { get; set; }
    public string? FinishedAt { get; set; }
    public int? Total { get; set; }
    public int? Done { get; set; }
    public bool Stale { get; set; } // stopped reporting before it finished: the runner quit
}

/// <summary>`role-radar matches --json`: Live Tracking's lists, newest first.</summary>
public sealed class LiveState
{
    public List<Match> Waiting { get; set; } = new();
    public List<Match> Skipped { get; set; } = new();
    public List<SentAlert> Sent { get; set; } = new();
    public RoundInfo? Round { get; set; }
    public string? NextDigest { get; set; }
    public bool SendRequested { get; set; }
    public bool AlertsOff { get; set; }
}

public sealed class Match
{
    public string Company { get; set; } = "";
    public string Uid { get; set; } = "";
    public string Title { get; set; } = "";
    public string? Location { get; set; }
    public string Url { get; set; } = "";
    public string FirstSeen { get; set; } = "";
    public string? FoundAt { get; set; } // when it joined New jobs: later than first_seen for a job only a later search matched
    public string? SkippedAt { get; set; }
    public string? SendAt { get; set; } // sent from Live Tracking: on its way out

    public string Id => $"{Company}#{Uid}";
    public string Found => FoundAt ?? FirstSeen;
}

/// <summary>One alert that went out: when, by which runner, and its jobs.</summary>
public sealed class SentAlert
{
    public string SentAt { get; set; } = "";
    public string? By { get; set; }
    public List<SentJob> Jobs { get; set; } = new();
}

public sealed class SentJob
{
    public string? Company { get; set; }
    public string? Title { get; set; }
    public string? Location { get; set; }
    public string? Url { get; set; }
}

/// <summary>`role-radar setup show`.</summary>
public sealed class SetupState
{
    public string? Profession { get; set; }
    public List<Profession> Professions { get; set; } = new();
    public List<string> Roles { get; set; } = new();
    public List<string> Exclude { get; set; } = new();
    public List<string> Locations { get; set; } = new();
    public List<string> Countries { get; set; } = new();
    public List<Country> CountryOptions { get; set; } = new();
    public List<string> Cities { get; set; } = new();
    public int? MaxExperienceYears { get; set; }
    public string? Education { get; set; } // none, bachelors, masters or phd
    public int Companies { get; set; } // how many companies checks read: the profession's, in their countries, and their own
    public Dictionary<string, int> CompaniesFor { get; set; } = new(); // the same for every combination of countries, keyed "US+IN"
    public Dictionary<string, int> CompaniesByCountry { get; set; } = new();
    public int CompaniesUntagged { get; set; } // companies whose job locations name no country: tracked for every country
    public string? Email { get; set; }
    public List<string> Also { get; set; } = new(); // who else gets the alerts, besides `email`
    public bool EmailReady { get; set; }
    public bool DiscordReady { get; set; } // a Discord webhook is saved
    public bool Ready { get; set; }
}

public sealed class Profession
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string About { get; set; } = "";
    public List<TitleGroup> Groups { get; set; } = new(); // job titles that alert
    public List<TitleGroup> SkipGroups { get; set; } = new(); // words that rule a title out
    public Examples? Examples { get; set; }
}

public sealed class TitleGroup
{
    public string Name { get; set; } = "";
    public List<string> Titles { get; set; } = new();
}

/// <summary>For the ⓘ beside the roles: a target title and jobs it alerts for; and with some target
/// titles and non-target words ticked, jobs that reach you and jobs that don't.</summary>
public sealed class Examples
{
    public TargetExample? Target { get; set; }
    public NonTargetExample? NonTarget { get; set; }
}

public sealed class TargetExample
{
    public string Title { get; set; } = "";
    public List<string> Jobs { get; set; } = new();
}

public sealed class NonTargetExample
{
    public List<string> Targets { get; set; } = new();
    public List<string> Words { get; set; } = new();
    public List<string> Reach { get; set; } = new();
    public List<string> Stopped { get; set; } = new();
}

public sealed class Country
{
    public string Code { get; set; } = "";
    public string Name { get; set; } = "";
}

/// <summary>`role-radar setup find`: the companies whose name or job site has every word searched for.</summary>
public sealed class CompanySearch
{
    public int Total { get; set; }
    public List<CompanyRow> Results { get; set; } = new();
    public int Untracked { get; set; } // how many they've turned off
}

public sealed class CompanyRow
{
    public string Name { get; set; } = "";
    public string Url { get; set; } = "";
    public string? Site { get; set; } // the reader, e.g. "workday"
    public List<string> Countries { get; set; } = new();
    public bool Own { get; set; } // added by them, not from the profession's list
    public bool Readable { get; set; } // false: on a job site Role Radar can't read yet
    public bool Off { get; set; } // they turned it off
    public bool Tracked { get; set; }
    public string? Why { get; set; } // why it isn't tracked
}

/// <summary>`role-radar setup add`: a company they asked for, tracked or not.</summary>
public sealed class AddResult
{
    public string Status { get; set; } = ""; // "added", "listed" (already there) or "failed" (can't be tracked)
    public string Name { get; set; } = "";
    public int? Jobs { get; set; }
    public string? Url { get; set; } // failed: listed already, on a job site Role Radar can't read yet
    public string? Why { get; set; } // listed, but not tracked
    public bool? TurnedOn { get; set; }
}
