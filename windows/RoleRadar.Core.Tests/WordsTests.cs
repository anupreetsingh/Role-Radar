using RoleRadar.Core;

namespace RoleRadar.Core.Tests;

/// <summary>What the app says, as the Mac app says it: times, counts, the round, the search, Setup's rules.</summary>
public class WordsTests
{
    private static readonly DateTimeOffset Noon = new(2026, 10, 5, 12, 0, 0, TimeSpan.Zero);

    public WordsTests() => Describe.Now = () => Noon;

    [Fact]
    public void TimesReadAsOnTheMac()
    {
        Assert.Equal("12 min. ago", Describe.Ago(Noon.AddMinutes(-12)));
        Assert.Equal("3 days ago", Describe.Ago(Noon.AddDays(-3)));
        Assert.Equal("1 day ago", Describe.Ago(Noon.AddHours(-30)));
        Assert.Equal("in 5 min.", Describe.Ago(Noon.AddMinutes(5).AddSeconds(10)));
        Assert.Equal("never", Describe.Ago(null));
        Assert.Equal("2:32 PM", Describe.Clock(new DateTimeOffset(2026, 10, 5, 14, 32, 0, TimeSpan.Zero)));
        Assert.Equal("12:05 AM", Describe.Clock(new DateTimeOffset(2026, 10, 5, 0, 5, 0, TimeSpan.Zero)));
        Assert.Equal("Sep 29, 3:50 PM", Describe.Full(new DateTimeOffset(2026, 9, 29, 15, 50, 0, TimeSpan.Zero)));
    }

    [Fact]
    public void CountsReadAsOnTheMac()
    {
        Assert.Equal("950", Describe.Compact(950));
        Assert.Equal("1.2K", Describe.Compact(1240));
        Assert.Equal("34K", Describe.Compact(34_000));
        Assert.Equal("2K", Describe.Compact(2_000));
        Assert.Equal("7,536", Describe.Number(7536));
        Assert.Equal("1 job", Describe.Plural(1, "job"));
        Assert.Equal("1 hr, 5 min", Describe.Took(TimeSpan.FromSeconds(3900)));
        Assert.Equal("25 min", Describe.Took(TimeSpan.FromMinutes(25)));
    }

    [Fact]
    public void TheRoundSaysWhoAndHowFar()
    {
        Assert.Equal("This PC's last round stopped at 10 of 7,536",
            Describe.RoundSummary(new RoundInfo { Runner = "laptop", Done = 10, Total = 7536, Stale = true }));
        Assert.StartsWith("This PC is checking: 2,210 of 7,536 · started ",
            Describe.RoundSummary(new RoundInfo { Runner = "laptop", Done = 2210, Total = 7536, StartedAt = "2026-10-05T11:56:00Z" }));
        Assert.Equal(0.5, Describe.RoundFraction(new RoundInfo { Done = 5, Total = 10 }));
        Assert.Null(Describe.RoundFraction(new RoundInfo { Done = 5, Total = 10, FinishedAt = "2026-10-05T11:00:00Z" }));
        Assert.Equal("No rounds recorded yet", Describe.RoundSummary(null));
    }

    [Fact]
    public void TheStatusLines()
    {
        var state = new RunnerState { Switches = { ["email"] = false, ["discord"] = false }, Waiting = 3 };
        Assert.Equal("3 new jobs in Live Tracking · alerts are off", Describe.WaitingLine(state, null));
        state.Switches["email"] = true;
        Assert.Equal("3 new jobs", Describe.WaitingLine(state, null));
        Assert.Equal(("3 sites failing (see role-radar status)", false),
            Describe.FailingLine(new RunnerState { LatestPass = new PassInfo { Failing = 3 } }));
        Assert.Null(Describe.FailingLine(new RunnerState()));
    }

    [Fact]
    public void TheSearchFindsTheStartOfWords()
    {
        var jobs = new List<Match>
        {
            new() { Title = "Software Engineer", Company = "Acme", Location = "Montréal" },
            new() { Title = "Maintainer", Company = "Beta" },
        };
        Assert.Equal([jobs[0]], JobSearch.Matching(jobs, "eng mont"));
        Assert.Empty(JobSearch.Matching(jobs, "ai"));
        Assert.Equal(jobs, JobSearch.Matching(jobs, "  "));
    }

    [Fact]
    public void SavedTitlesTickTheirBoxes()
    {
        var (ticked, own) = SetupRules.Boxes(["Software Engineer", "Rust wizard"], ["software engineer", "SRE"]);
        Assert.Equal(["Rust wizard", "software engineer"], ticked.Order(StringComparer.Ordinal));
        Assert.Equal(["Rust wizard"], own);
        Assert.Equal("no experience, 1+ or 2+ years", SetupRules.StillAlert(3));
        Assert.Equal("anything from no experience up to 5+ years", SetupRules.StillAlert(6));
        Assert.Equal(["Bengaluru", "Pune"], SetupRules.Lines("Bengaluru,\n Pune ,"));
        Assert.Equal("Canada, Australia and India", SetupRules.CountryNames(["Canada", "Australia", "India"]));
    }

    [Fact]
    public void ACompanyIsDescribedByItsSiteAndCountries()
    {
        Assert.Equal("Workday · US, CA", SetupRules.About(new CompanyRow { Site = "workday", Countries = ["US", "CA"] }));
        Assert.Equal("iCIMS · not tracked: it doesn't post jobs in your countries",
            SetupRules.About(new CompanyRow { Site = "icims", Why = "it doesn't post jobs in your countries" }));
        Assert.Equal("", SetupRules.About(new CompanyRow { Site = "generic", Off = true, Why = "you turned it off" }));
    }

    [Fact]
    public void TheProfileSavesAsSetupProfileTakesIt()
    {
        var profile = new SetupRules.Profile(["engineer"], ["senior"], [], ["IN", "US"], 2, "masters");
        Assert.Equal("""{"countries":["IN","US"],"exclude":["senior"],"locations":[],"max_experience_years":2,"roles":["engineer"]}""",
            profile.Json(withEducation: false));
        Assert.Contains("\"education\":\"masters\"", profile.Fingerprint);
    }

    [Fact]
    public void RepliesReadInSnakeCase()
    {
        var setup = Json.Read<SetupState>("""
            {"profession": "tech", "professions": [{"id": "tech", "name": "Tech", "about": "x", "groups": [],
             "skip_groups": [{"name": "Senior levels", "titles": ["senior"]}],
             "examples": {"target": {"title": "software engineer", "jobs": ["A"]},
                          "non_target": {"targets": ["t"], "words": ["w"], "reach": ["r"], "stopped": ["s"]}}}],
             "roles": [], "exclude": [], "locations": [], "countries": ["US"],
             "country_options": [{"code": "US", "name": "United States"}], "max_experience_years": null,
             "companies": 7, "companies_for": {"US": 7}, "companies_by_country": {"US": 5}, "companies_untagged": 2,
             "also": [], "email_ready": false, "discord_ready": true, "ready": true}
            """)!;
        Assert.Equal("senior", setup.Professions[0].SkipGroups[0].Titles[0]);
        Assert.Equal("s", setup.Professions[0].Examples!.NonTarget!.Stopped[0]);
        Assert.Equal(7, setup.CompaniesFor["US"]);
        Assert.Equal(2, setup.CompaniesUntagged);
        Assert.True(setup.DiscordReady && setup.Ready);
        Assert.Null(setup.MaxExperienceYears);
    }

    [Fact]
    public void AnInstalledAppReadsItsPlaceFromAppJson()
    {
        var folder = Directory.CreateTempSubdirectory().FullName;
        File.WriteAllText(Path.Combine(folder, "app.json"), """
            {"name": "Role Radar", "home": "Role Radar", "keychain": "Role Radar", "checks": true, "dev": false,
             "version": "2.3.0", "feed": "https://example.com/appcast-windows.xml", "update_key": "abc"}
            """);
        var place = Place.Load(folder, dataFolder: @"C:\Users\me\AppData\Local");
        Assert.Equal("Role Radar", place.Name);
        Assert.True(place.Installed && place.Checks && !place.Dev);
        Assert.Equal(Path.Combine(folder, "python", "Role Radar Checker.exe"), place.Python);
        Assert.Equal("2.3.0", place.Version);
        Assert.Equal("Role Radar", place.Env["ROLE_RADAR_KEYCHAIN"]);

        var dev = Place.Load(Directory.CreateTempSubdirectory().FullName, "/data", new Dictionary<string, string?> { ["RR_PYTHON"] = "/venv/python" });
        Assert.Equal("Role Radar Dev", dev.Name);
        Assert.True(dev.Dev && !dev.Checks && !dev.Installed);
        Assert.Equal("/venv/python", dev.Python);
    }
}
