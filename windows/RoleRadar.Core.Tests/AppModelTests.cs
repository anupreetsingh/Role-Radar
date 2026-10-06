using RoleRadar.Core;

namespace RoleRadar.Core.Tests;

public class AppModelTests
{
    private static Place Dev(bool checks = false) => new()
    {
        Name = "Role Radar Dev", Home = Path.GetTempPath(), Keychain = "Role Radar Dev", Python = "python", Dev = true,
        Checks = checks, Version = "0.0.0-dev",
    };

    private static (AppModel Model, FakeCli Cli) Made(bool checks = false, bool emailReady = true)
    {
        var cli = new FakeCli { EmailReady = emailReady };
        return (new AppModel(cli, Dev(checks)), cli);
    }

    [Fact]
    public async Task StartingOpensSetupUntilItsDone()
    {
        var (model, cli) = Made();
        cli.Ready = false;
        var opened = 0;
        model.WindowWanted += () => opened++;
        await model.StartAsync();
        Assert.Equal(["setup", "init"], cli.Calls[0].Args);
        Assert.Equal(1, opened);
        Assert.True(model.ShowingSetup);

        var (later, laterCli) = Made();
        later.LaunchedAtLogin = true; // Setup done, and opened at login: the tray only
        later.WindowWanted += () => opened++;
        await later.StartAsync();
        Assert.Equal(1, opened);
        Assert.Contains(laterCli.Calls, call => call.Args.SequenceEqual(["switch", "--json"])); // a dev build: no --start
    }

    [Fact]
    public async Task OnlyAnInstalledCheckerIsStarted()
    {
        var (model, cli) = Made(checks: true);
        await model.LoadSetupAsync();
        await model.RefreshAsync();
        Assert.Equal(["switch", "--json", "--start"], cli.Last("switch").Args);
    }

    [Fact]
    public async Task AChannelThatIsntSetUpIsSwitchedOff()
    {
        var (model, cli) = Made(emailReady: false);
        await model.LoadSetupAsync();
        await model.RefreshAsync();
        Assert.Equal(["switch", "email", "off", "--json"], cli.Last("switch").Args);
        Assert.False(model.Switch("email"));
    }

    [Fact]
    public async Task FlippingTheCheckerSavesTheSwitch()
    {
        var (model, cli) = Made();
        await model.RefreshAsync();
        Assert.Equal("laptop", model.Checking);
        await model.SetAsync("laptop", false);
        Assert.Equal(["switch", "laptop", "off", "--json"], cli.Last("switch").Args);
        Assert.Null(model.Checking);
        Assert.Empty(model.Busy);
    }

    [Fact]
    public async Task AFailedSwitchSaysWhyAndShowsTheRealState()
    {
        var (model, cli) = Made();
        cli.Failing = "laptop";
        await model.SetAsync("laptop", false);
        Assert.Equal("laptop", model.Error);
        Assert.True(model.Switch("laptop")); // re-read: still on
    }

    [Fact]
    public async Task MarkingJobsAsSeenMovesThemAtOnceAndSendsThePicks()
    {
        var (model, cli) = Made();
        await model.RefreshLiveAsync();
        Assert.Equal(3, model.Waiting.Count);
        var changed = 0;
        model.LiveChanged += () => changed++;
        var picked = new[] { model.Waiting[0], model.Waiting[2] };
        await model.MarkSeenAsync(picked);
        var (args, stdin) = cli.Last("matches");
        Assert.Equal(["matches", "skip", "--json", "--stdin"], args);
        Assert.Equal("""[["Stripe","stripe:1"],["Ramp","ramp:3"]]""", stdin);
        Assert.True(changed >= 2); // moved at once, then the reply
        Assert.Empty(model.LiveBusy);
    }

    [Fact]
    public async Task SendingAllUsesAllAndMarksThemOnTheirWay()
    {
        var (model, cli) = Made();
        await model.RefreshLiveAsync();
        var marked = false;
        model.LiveChanged += () => marked |= model.Waiting.All(m => m.SendAt is not null);
        await model.SendAsync(null);
        Assert.Equal(["matches", "send", "--json", "--all"], cli.Last("matches").Args);
        Assert.Null(cli.Last("matches").Stdin);
        Assert.True(marked);
    }

    [Fact]
    public async Task ASeenJobGoesBackToNew()
    {
        var (model, cli) = Made();
        await model.RefreshLiveAsync();
        var seen = model.Skipped[0];
        await model.SkipAsync(seen, false);
        Assert.Equal(["matches", "unskip", "Shopify", "shopify:4", "--json"], cli.Last("matches").Args);
    }

    [Fact]
    public async Task TheSameListsAgainChangeNothing()
    {
        var (model, _) = Made();
        await model.RefreshLiveAsync();
        var changed = 0;
        model.LiveChanged += () => changed++;
        await model.RefreshLiveAsync();
        Assert.Equal(0, changed);
    }

    [Fact]
    public async Task QuittingStopsTheCheckerWithoutWaiting()
    {
        var (model, cli) = Made();
        model.StopChecker();
        Assert.Equal(["stop"], cli.Started.Single());
        await Task.CompletedTask;
    }

    [Fact]
    public async Task StartCheckingOpensAtLoginOnlyWhenInstalled()
    {
        var registered = 0;
        var cli = new FakeCli();
        await new AppModel(cli, Dev(), () => registered++).StartCheckingAsync();
        Assert.Equal(0, registered); // a dev build never opens at login
        Assert.Equal(["switch", "laptop", "on", "--json"], cli.Last("switch").Args);
        var installed = new Place
        {
            Name = "Role Radar", Home = Path.GetTempPath(), Keychain = "Role Radar", Python = "python", Dev = false,
            Checks = true, Installed = true, Version = "2.2.1",
        };
        await new AppModel(cli, installed, () => registered++).StartCheckingAsync();
        Assert.Equal(1, registered);
    }

    [Fact]
    public async Task ASetupStepTakesTheSetupItReports()
    {
        var (model, cli) = Made();
        var problem = await model.SetupStepAsync(["profession"], new Dictionary<string, string> { ["profession"] = "tech" });
        Assert.Null(problem);
        Assert.Equal(["setup", "profession"], cli.Last("setup").Args);
        Assert.Equal("""{"profession":"tech"}""", cli.Last("setup").Stdin);
        Assert.True(model.Ready);
    }
}
