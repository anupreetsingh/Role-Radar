// The app's state and what it does, as the Mac app's Model: what `role-radar switch --json` reports (the
// switches, who's checking, the round, activity), Live Tracking's lists (`matches --json`), and Setup's
// answers (`setup show`). The windows watch its events and call its actions; every action is a
// role-radar command (Cli.cs), and its reply is the new state. Its timers are the app's (App.xaml.cs).
using System.Text.Json;

namespace RoleRadar.Core;

public sealed class AppModel(ICli cli, Place place, Action? openAtLogin = null)
{
    public event Action? StateChanged; // State, Busy, Error
    public event Action? LiveChanged; // Live, LiveBusy, LiveError
    public event Action? SetupChanged; // Setup, SetupError
    public event Action? WindowWanted; // open the window: Setup's pages or Live Tracking, as ShowingSetup says

    public ICli Cli { get; } = cli;
    public Place Place { get; } = place;
    public bool LaunchedAtLogin { get; set; }

    public RunnerState? State { get; private set; }
    public HashSet<string> Busy { get; } = []; // switches being flipped
    public string? Error { get; set; }
    public LiveState? Live { get; private set; }
    public HashSet<string> LiveBusy { get; } = []; // match ids, "seen", "new" or "send" in flight
    public string? LiveError { get; private set; }
    public SetupState? Setup { get; private set; }
    public string? SetupError { get; private set; } // why Setup couldn't read or save its files: always shown
    public bool ShowingSetup { get; set; } // the window shows Setup's pages rather than Live Tracking

    private int liveActions; // a read that started before the latest action is out of date
    private string? liveOutput; // the lists as last read: the same again changes nothing

    // -- what it knows ------------------------------------------------------------------------------

    /// <summary>Setup is done: a profession, countries and roles.</summary>
    public bool Ready => Setup?.Ready == true;

    /// <summary>It starts checking only once Setup is done (and a dev build only if asked to check).</summary>
    public bool CanStart => Place.Checks && Ready;

    public string? Checking => State?.Checking;

    public bool Switch(string name) => State?.Switches.GetValueOrDefault(name) == true;

    /// <summary>Whether an alert channel is set up in Setup.</summary>
    public bool ChannelReady(string name) => Setup is not null && (name == "email" ? Setup.EmailReady : Setup.DiscordReady);

    /// <summary>Whether alerts can be sent at all: email or Discord is set up.</summary>
    public bool CanAlert => ChannelReady("email") || ChannelReady("discord");

    public List<Match> Waiting => Live?.Waiting ?? [];
    public List<Match> Skipped => Live?.Skipped ?? [];

    // -- starting up --------------------------------------------------------------------------------

    /// <summary>Create the app's files, then open Setup until it's done (or whenever someone opens the app,
    /// rather than it opening at login), and read the state.</summary>
    public async Task StartAsync()
    {
        var init = await SetupCommandAsync(["init"]);
        if (!init.Ok)
        {
            SetupError = init.Message;
            SetupChanged?.Invoke();
        }
        await LoadSetupAsync();
        if (!Ready || !LaunchedAtLogin)
        {
            ShowWindow(setup: !Ready);
        }
        await RefreshAsync();
    }

    /// <summary>Open the window: on Setup or Live Tracking, or (null) as it was.</summary>
    public void ShowWindow(bool? setup = null)
    {
        if (setup is { } page)
        {
            ShowingSetup = page;
        }
        WindowWanted?.Invoke();
    }

    /// <summary>The anonymous check-in, for the user counts on the project's GitHub page: not from a dev build.</summary>
    public Task CheckInAsync() => Place.Dev ? Task.CompletedTask : Cli.RunAsync(["checkin"]);

    // -- the switches -------------------------------------------------------------------------------

    /// <summary>Re-read the state, starting the checker first if it's switched on and not running.</summary>
    public async Task RefreshAsync()
    {
        await RunAsync(["switch", "--json", .. CanStart ? new[] { "--start" } : []]);
        // A channel that isn't set up can't send: keep its switch off, so every view says so.
        foreach (var name in new[] { "discord", "email" })
        {
            if (Switch(name) && !ChannelReady(name))
            {
                await RunAsync(["switch", name, "off", "--json"]);
            }
        }
    }

    /// <summary>Flip a switch: "laptop" (the checker), "discord" or "email" (alerts).</summary>
    public async Task SetAsync(string name, bool on)
    {
        Busy.Add(name);
        if (State is not null)
        {
            State.Switches[name] = on;
        }
        StateChanged?.Invoke();
        // --start: switching the checker on also starts it if it isn't running (not during Setup).
        await RunAsync(["switch", name, on ? "on" : "off", "--json", .. CanStart ? new[] { "--start" } : []]);
        Busy.Remove(name);
        StateChanged?.Invoke();
    }

    private async Task RunAsync(string[] args)
    {
        var result = await Cli.RunAsync(args);
        if (result.Ok)
        {
            try
            {
                State = result.Read<RunnerState>();
                Error = null;
            }
            catch (JsonException error)
            {
                Error = $"Unexpected reply from role-radar: {error.Message}";
            }
        }
        else
        {
            Error = result.Message;
            // After a failed switch, re-read the real state so the switches don't lie.
            var fresh = await Cli.RunAsync(["switch", "--json"]);
            if (fresh.Ok)
            {
                try
                {
                    State = fresh.Read<RunnerState>();
                }
                catch (JsonException)
                {
                    // Keep what was shown.
                }
            }
        }
        StateChanged?.Invoke();
    }

    /// <summary>Setup is done: start checking now and at every login (a dev build doesn't open at login).</summary>
    public async Task StartCheckingAsync()
    {
        if (!Place.Dev)
        {
            openAtLogin?.Invoke();
        }
        await SetAsync("laptop", true);
    }

    /// <summary>Ask the checker to finish the companies in flight and quit, without waiting: on quit.</summary>
    public void StopChecker() => Cli.Start(["stop"]);

    /// <summary>Try again after a failure: the state, and Setup's.</summary>
    public async Task RetryAsync()
    {
        await RefreshAsync();
        await LoadSetupAsync();
    }

    // -- Live Tracking --------------------------------------------------------------------------------

    public Task RefreshLiveAsync()
    {
        var seen = liveActions;
        return RunLiveAsync(["matches", "--json"], outdated: () => liveActions != seen);
    }

    /// <summary>Mark one match as seen, or put it back.</summary>
    public Task SkipAsync(Match match, bool on)
    {
        StartLiveAction(match.Id);
        SetSkipped(new HashSet<string> { match.Id }, on);
        return RunLiveAsync(["matches", on ? "skip" : "unskip", match.Company, match.Uid, "--json"], done: match.Id);
    }

    /// <summary>Mark matches as seen (null: all of them): they leave the stack, and are never sent.</summary>
    public Task MarkSeenAsync(IReadOnlyList<Match>? matches)
    {
        StartLiveAction("seen");
        SetSkipped((matches ?? Waiting).Select(m => m.Id).ToHashSet(), true);
        return RunLiveAsync(["matches", "skip", "--json", Picks(matches)], PickList(matches), done: "seen");
    }

    /// <summary>Put seen matches back among the new ones, while the next digest hasn't recorded them yet.</summary>
    public Task MarkNewAsync(IReadOnlyList<Match> matches)
    {
        StartLiveAction("new");
        SetSkipped(matches.Select(m => m.Id).ToHashSet(), false);
        return RunLiveAsync(["matches", "unskip", "--json", Picks(matches)], PickList(matches), done: "new");
    }

    /// <summary>Send matches now (null: all of them), alerts on or off; once sent, they leave the stack.</summary>
    public Task SendAsync(IReadOnlyList<Match>? matches)
    {
        StartLiveAction("send");
        var ids = (matches ?? Waiting).Select(m => m.Id).ToHashSet();
        var now = DateTimeOffset.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
        foreach (var match in Waiting.Where(m => ids.Contains(m.Id)))
        {
            match.SendAt = now;
        }
        LiveChanged?.Invoke();
        return RunLiveAsync(["matches", "send", "--json", Picks(matches)], PickList(matches), done: "send");
    }

    private void StartLiveAction(string key)
    {
        liveActions++;
        liveOutput = null; // what's shown changed already: take whatever comes back
        LiveBusy.Add(key);
    }

    /// <summary>Move matches between New and Seen straight away, before the command confirms it.</summary>
    private void SetSkipped(IReadOnlySet<string> ids, bool on)
    {
        if (Live is null)
        {
            return;
        }
        var from = on ? Live.Waiting : Live.Skipped;
        var moved = from.Where(m => ids.Contains(m.Id)).ToList();
        from.RemoveAll(m => ids.Contains(m.Id));
        var stamp = DateTimeOffset.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
        foreach (var match in moved)
        {
            match.SkippedAt = on ? stamp : null;
        }
        if (on)
        {
            Live.Skipped.InsertRange(0, moved);
        }
        else
        {
            Live.Waiting.AddRange(moved);
            Live.Waiting.Sort((a, b) => string.CompareOrdinal(b.Found, a.Found));
        }
        LiveChanged?.Invoke();
    }

    /// <summary>All of them (--all), or the picked ones as JSON on stdin: a process can only take so many arguments.</summary>
    private static string Picks(IReadOnlyList<Match>? matches) => matches is null ? "--all" : "--stdin";

    private static string? PickList(IReadOnlyList<Match>? matches) =>
        matches is null ? null : Json.Write(matches.Select(m => new[] { m.Company, m.Uid }).ToList());

    private async Task RunLiveAsync(string[] args, string? stdin = null, Func<bool>? outdated = null, string? done = null)
    {
        var result = await Cli.RunAsync(args, stdin);
        if (done is not null)
        {
            LiveBusy.Remove(done);
        }
        if (outdated?.Invoke() == true)
        {
            LiveChanged?.Invoke();
            return;
        }
        if (result.Ok)
        {
            if (result.Output == liveOutput)
            {
                if (LiveError is null && done is null)
                {
                    return; // nothing new
                }
                LiveError = null;
            }
            else
            {
                try
                {
                    Live = result.Read<LiveState>();
                    liveOutput = result.Output;
                    LiveError = null;
                }
                catch (JsonException error)
                {
                    LiveError = $"Unexpected reply from role-radar: {error.Message}";
                }
            }
        }
        else
        {
            LiveError = result.Message;
        }
        LiveChanged?.Invoke();
    }

    // -- Setup ----------------------------------------------------------------------------------

    public async Task LoadSetupAsync()
    {
        var result = await SetupCommandAsync(["show"]);
        if (result.Ok)
        {
            try
            {
                Setup = result.Read<SetupState>();
                SetupError = null;
            }
            catch (JsonException error)
            {
                SetupError = $"Unexpected reply from role-radar: {error.Message}";
            }
        }
        else
        {
            SetupError = result.Message;
        }
        SetupChanged?.Invoke();
    }

    /// <summary>Run `role-radar setup ARGS` with `data` on stdin, taking the setup it reports. Returns the
    /// error message, or null.</summary>
    public async Task<string?> SetupStepAsync(string[] args, object data)
    {
        var result = await SetupCommandAsync(args, data as string ?? Json.Write(data));
        if (!result.Ok)
        {
            return result.Message;
        }
        try
        {
            Setup = result.Read<SetupState>();
            SetupChanged?.Invoke();
        }
        catch (JsonException)
        {
            // A step that replies with something else (add, find) is read by its caller.
        }
        return null;
    }

    public Task<CliResult> SetupCommandAsync(string[] args, string? stdin = null) => Cli.RunAsync(["setup", .. args], stdin);
}
