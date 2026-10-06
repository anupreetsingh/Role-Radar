using System.Diagnostics;
using System.IO; // in a WPF project too (RoleRadar.Tests links this file), which leaves it out
using RoleRadar.Core;

namespace RoleRadar.Core.Tests;

/// <summary>The real role-radar, for checking the app against what it really replies: the repo's role_radar
/// on a Python that runs it ($RR_PYTHON, else the repo's .venv, else python), with the professions' company
/// lists written once for the tests, and a test app's own files and credentials ("Role Radar Tests").</summary>
public static class RealCli
{
    public static readonly string Project = FindProject();
    private static readonly Lazy<string> Lists = new(Prepare);

    public static string Python
    {
        get
        {
            if (Environment.GetEnvironmentVariable("RR_PYTHON") is { Length: > 0 } python)
            {
                return python;
            }
            var venv = OperatingSystem.IsWindows() ? Path.Combine(".venv", "Scripts", "python.exe") : Path.Combine(".venv", "bin", "python");
            return File.Exists(Path.Combine(Project, venv)) ? Path.Combine(Project, venv) : OperatingSystem.IsWindows() ? "python" : "python3";
        }
    }

    /// <summary>An app model on the real role-radar, in a folder of its own, and what each command replied.</summary>
    public static (AppModel Model, RecordingCli Cli, Place Place) Made()
    {
        _ = Lists.Value;
        var place = new Place
        {
            Name = "Role Radar Tests", Home = Directory.CreateTempSubdirectory("role-radar-tests-").FullName,
            Keychain = "Role Radar Tests", Python = Python, Dev = true, Checks = false, Version = "0.0.0-test",
        };
        var cli = new RecordingCli(new ProcessCli(place));
        return (new AppModel(cli, place), cli, place);
    }

    /// <summary>A title long enough to need cutting short wherever a job is listed.</summary>
    public const string LongTitle = "Software Engineer II, Machine Learning Infrastructure and Developer Productivity (Remote, United States)";

    // Live Tracking's lists and the sidebar's numbers, as a checker leaves them: a sent alert, two new jobs and a
    // seen one, a round half done, an hour of checks.
    private const string Seed = """
        import sys
        from role_radar.sqlite import SqliteStateStore
        from role_radar.storage import QueuedMatch, SeenJob, stats_hour, to_iso, utcnow
        store = SqliteStateStore(sys.argv[1])
        now = to_iso(utcnow())
        record = store.load_company("Stripe")
        record.jobs["stripe:1"] = SeenJob("Software Engineer, New Grad", "https://example.com/1", "f1", now,
                                          "San Francisco, CA", matched=True, notified_at=now)
        record.alerted.append("stripe:1")
        store.save_company(record)
        store.repair_queue([QueuedMatch("Ramp", "ramp:2", "Backend Engineer", "https://example.com/2", now, "New York, NY", queued_at=now),
                            QueuedMatch("Datadog", "datadog:4", sys.argv[2], "https://example.com/4", now,
                                        "Remote - United States; New York, NY; Boston, MA; Denver, CO", queued_at=now),
                            QueuedMatch("Figma", "figma:3", "Data Engineer I", "https://example.com/3", now, queued_at=now)], [])
        store.mark_skipped("Figma", "figma:3", True)
        store.record_run("laptop:test", {"finished_at": now, "checked": 12, "failed": 0, "failing": 1, "pending": 1, "holder": "laptop:test"})
        store.record_stats("laptop", stats_hour(utcnow()), {"checked": 12, "failed": 0, "new_jobs": 3, "matches": 2, "alerts": 1})
        store.record_round("laptop:test", {"started_at": now, "total": 12, "done": 5, "updated_at": now})
        """;

    /// <summary>Give the app's state what a checker leaves: Live Tracking's lists, a round, some activity.</summary>
    public static void SeedLiveTracking(Place place) => RunPython(Seed, Path.Combine(place.Home, "state.db"), LongTitle);

    /// <summary>Run Python code with role_radar, `args` after it; fails the test if it fails.</summary>
    public static void RunPython(string code, params string[] args)
    {
        _ = Lists.Value;
        var info = new ProcessStartInfo(Python) { RedirectStandardError = true, UseShellExecute = false };
        foreach (var arg in new[] { "-c", code }.Concat(args))
        {
            info.ArgumentList.Add(arg);
        }
        using var process = Process.Start(info)!;
        var errors = process.StandardError.ReadToEnd();
        process.WaitForExit();
        Assert.True(process.ExitCode == 0, errors);
    }

    /// <summary>role_radar from the repo, its lists in a folder of their own, and none of the developer's ROLE_RADAR_*
    /// settings: every command the tests run (child processes) takes these.</summary>
    private static string Prepare()
    {
        foreach (var name in Environment.GetEnvironmentVariables().Keys.Cast<string>().Where(n => n.StartsWith("ROLE_RADAR_")))
        {
            Environment.SetEnvironmentVariable(name, null);
        }
        Environment.SetEnvironmentVariable("PYTHONPATH", Project);
        var lists = Directory.CreateTempSubdirectory("role-radar-lists-").FullName;
        var info = new ProcessStartInfo(Python) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        info.ArgumentList.Add(Path.Combine(Project, "scripts", "write_lists.py"));
        info.ArgumentList.Add(lists);
        using var process = Process.Start(info) ?? throw new InvalidOperationException($"Couldn't run {Python}");
        process.StandardOutput.ReadToEnd();
        var errors = process.StandardError.ReadToEnd();
        process.WaitForExit();
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException($"write_lists.py failed: {errors}");
        }
        Environment.SetEnvironmentVariable("ROLE_RADAR_LISTS", lists);
        return lists;
    }

    private static string FindProject()
    {
        for (var folder = new DirectoryInfo(AppContext.BaseDirectory); folder is not null; folder = folder.Parent)
        {
            if (File.Exists(Path.Combine(folder.FullName, "role_radar", "__init__.py")))
            {
                return folder.FullName;
            }
        }
        throw new InvalidOperationException("Not inside the Role Radar repo: no role_radar/ above " + AppContext.BaseDirectory);
    }
}

/// <summary>Runs commands with another ICli, keeping each reply, and knowing whether any is still running.</summary>
public sealed class RecordingCli(ICli inner) : ICli
{
    private int running;

    public List<(string[] Args, CliResult Result)> Replies { get; } = [];
    public bool Running => Volatile.Read(ref running) > 0; // a command hasn't replied yet

    public async Task<CliResult> RunAsync(IReadOnlyList<string> args, string? stdin = null)
    {
        Interlocked.Increment(ref running);
        try
        {
            var result = await inner.RunAsync(args, stdin);
            lock (Replies)
            {
                Replies.Add((args.ToArray(), result));
            }
            return result;
        }
        finally
        {
            Interlocked.Decrement(ref running);
        }
    }

    public void Start(IReadOnlyList<string> args) => throw new InvalidOperationException("The tests start no checker");

    /// <summary>What the latest command starting with `args` printed.</summary>
    public string Last(params string[] args) => Replies.Last(reply => reply.Args.Take(args.Length).SequenceEqual(args)).Result.Output;
}
