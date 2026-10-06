using RoleRadar.Core;

namespace RoleRadar.Core.Tests;

/// <summary>The app against the real role-radar: what it sends is taken, and each reply has every field the
/// app reads (Shape). A change to role_radar that would leave the Windows app blank or failing fails here,
/// on the Mac as on Windows, with no Windows PC needed.</summary>
public class CliContractTests
{
    [Fact]
    public void AReplyLackingAFieldTheAppReadsFails()
    {
        const string reply = """
            {"waiting": [{"uid": "1", "title": "Engineer", "url": "https://x", "first_seen": "2026-10-01T00:00:00Z"}],
             "skipped": [], "sent": [], "send_requested": false, "alerts_off": false}
            """;
        var failure = Assert.ThrowsAny<Xunit.Sdk.XunitException>(() => Shape.AssertComplete<LiveState>(reply));
        Assert.Contains("LiveState.waiting[0].company", failure.Message);
        Shape.AssertComplete<LiveState>(reply.Replace("\"uid\"", "\"company\": \"Acme\", \"uid\""));
    }

    [Fact]
    public async Task SetupWorksOnTheRealCommands()
    {
        var (model, cli, _) = RealCli.Made();
        await model.StartAsync(); // setup init, setup show, switch --json (a test app starts no checker)
        Assert.Null(model.SetupError);
        Shape.AssertComplete<SetupState>(cli.Last("setup", "show"));
        Assert.Equal(["accounting", "healthcare", "tech"], model.Setup!.Professions.Select(p => p.Id).Order());
        Assert.False(model.Setup.Ready);

        Assert.Null(await model.SetupStepAsync(["profession"], new Dictionary<string, string> { ["profession"] = "tech" }));
        Shape.AssertComplete<SetupState>(cli.Last("setup", "profession"));
        Assert.Equal("tech", model.Setup!.Profession);
        Assert.NotEmpty(model.Setup.Roles);
        Assert.True(model.Setup.CompaniesFor["US"] > 1000, "Tech's company list should come with the app");

        var answers = new SetupRules.Profile(model.Setup.Roles, model.Setup.Exclude, [], ["US", "IN"], 2, "bachelors");
        Assert.Null(await model.SetupStepAsync(["profile"], answers.Json(withEducation: true)));
        Shape.AssertComplete<SetupState>(cli.Last("setup", "profile"));
        Assert.True(model.Setup!.Ready);
        Assert.Equal(["US", "IN"], model.Setup.Countries);
        Assert.Equal((2, "bachelors"), (model.Setup.MaxExperienceYears, model.Setup.Education));
        Assert.Equal(model.Setup.CompaniesFor["US+IN"], model.Setup.Companies);
        Assert.Equal(answers.Exclude, model.Setup.Exclude); // the boxes ticked come back as they were

        var found = await model.SetupCommandAsync(["find"], Json.Write(new Dictionary<string, object> { ["query"] = "stripe", ["limit"] = 5, ["which"] = "all" }));
        Shape.AssertComplete<CompanySearch>(found.Output);
        Assert.True(found.Read<CompanySearch>().Results.Single(c => c.Name == "Stripe").Tracked);

        var tracked = model.Setup!.Companies;
        Assert.Null(await model.SetupStepAsync(["track"], new Dictionary<string, object> { ["names"] = new[] { "Stripe" }, ["tracked"] = false }));
        var off = await model.SetupCommandAsync(["find"], Json.Write(new Dictionary<string, object> { ["query"] = "", ["limit"] = 50, ["which"] = "off" }));
        Assert.Equal(["Stripe"], off.Read<CompanySearch>().Results.Select(c => c.Name));
        Assert.Equal(tracked - 1, model.Setup!.Companies);
    }

    [Fact]
    public async Task LiveTrackingWorksOnTheRealCommands()
    {
        var (model, cli, place) = RealCli.Made();
        await model.StartAsync();
        RealCli.SeedLiveTracking(place);

        await model.RefreshAsync();
        Assert.Null(model.Error);
        Shape.AssertComplete<RunnerState>(cli.Last("switch", "--json"));
        Assert.Equal((5, 12), (model.State!.Round!.Done, model.State.Round.Total));
        Assert.Equal(12, model.State.Activity!.Checked);
        Assert.Equal(12, model.State.Activity.Hours[^1].Mac); // this computer's checks, in the latest hour

        await model.RefreshLiveAsync();
        Assert.Null(model.LiveError);
        Shape.AssertComplete<LiveState>(cli.Last("matches", "--json"));
        var live = model.Live!;
        Assert.Equal(["Backend Engineer", RealCli.LongTitle], live.Waiting.Select(m => m.Title).Order());
        Assert.Equal(["Data Engineer I"], live.Skipped.Select(m => m.Title));
        Assert.Equal(["Software Engineer, New Grad"], live.Sent.SelectMany(a => a.Jobs).Select(j => j.Title));

        var backend = live.Waiting.Single(m => m.Title == "Backend Engineer");
        await model.SkipAsync(backend, true); // Mark as Seen, then back
        Assert.Equal(["Backend Engineer", "Data Engineer I"], model.Live!.Skipped.Select(m => m.Title).Order());
        await model.MarkNewAsync(model.Live.Skipped.Where(m => m.Title == "Backend Engineer").ToList());
        Assert.Equal(["Backend Engineer", RealCli.LongTitle], model.Live!.Waiting.Select(m => m.Title).Order());

        await model.SetAsync("laptop", false);
        Assert.Null(model.Error);
        Assert.False(model.Switch("laptop"));
    }
}
