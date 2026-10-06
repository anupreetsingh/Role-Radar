using RoleRadar.Core;

namespace RoleRadar.Core.Tests;

/// <summary>Answers the app's commands from fixed state, the way role-radar would, and records them.</summary>
public sealed class FakeCli : ICli
{
    public List<(string[] Args, string? Stdin)> Calls { get; } = [];
    public List<string[]> Started { get; } = [];
    public Dictionary<string, bool> Switches { get; } = new() { ["laptop"] = true, ["lambda"] = false, ["discord"] = false, ["email"] = true };
    public bool EmailReady { get; set; } = true;
    public bool Ready { get; set; } = true;
    public string? Failing { get; set; } // a command that fails with this message

    public static readonly string Now = DateTimeOffset.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");

    public LiveState Live { get; } = new()
    {
        Waiting =
        [
            Job(1, "Stripe", "Software Engineer, New Grad", "San Francisco, CA"),
            Job(2, "Databricks", "Data Engineer I", "Bengaluru, India"),
            Job(3, "Ramp", "Backend Engineer", "Montréal, QC"),
        ],
        Skipped = [Job(4, "Shopify", "Developer", "Toronto, ON", skipped: Now)],
        NextDigest = Now,
    };

    public static Match Job(int index, string company, string title, string location, string? skipped = null) => new()
    {
        Company = company, Uid = $"{company.ToLowerInvariant()}:{index}", Title = title, Location = location,
        Url = $"https://example.com/{index}", FirstSeen = DateTimeOffset.UtcNow.AddMinutes(-index).ToString("yyyy-MM-ddTHH:mm:ssZ"),
        SkippedAt = skipped,
    };

    public Task<CliResult> RunAsync(IReadOnlyList<string> args, string? stdin = null)
    {
        Calls.Add((args.ToArray(), stdin));
        if (Failing is not null && args.Contains(Failing))
        {
            return Task.FromResult(new CliResult(false, Message: Failing!));
        }
        var reply = args[0] switch
        {
            "switch" => Switch(args),
            "matches" => System.Text.Json.JsonSerializer.Serialize(Live, Json.Options), // snake_case, as role-radar writes it
            "setup" => Json.Write(new Dictionary<string, object?>
            {
                ["ready"] = Ready, ["email_ready"] = EmailReady, ["discord_ready"] = false, ["profession"] = "tech",
                ["roles"] = new[] { "engineer" }, ["countries"] = new[] { "US" },
            }),
            _ => "{}",
        };
        return Task.FromResult(new CliResult(true, reply));
    }

    private string Switch(IReadOnlyList<string> args)
    {
        if (args.Count > 2 && args[2] is "on" or "off")
        {
            Switches[args[1]] = args[2] == "on";
        }
        return Json.Write(new Dictionary<string, object?>
        {
            ["switches"] = Switches, ["checking"] = Switches["laptop"] ? "laptop" : null, ["laptop_app_pid"] = 42,
            ["latest_pass"] = new Dictionary<string, object?> { ["runner"] = "laptop", ["failing"] = 3 },
        });
    }

    public void Start(IReadOnlyList<string> args) => Started.Add(args.ToArray());

    public (string[] Args, string? Stdin) Last(string command) => Calls.Last(call => call.Args[0] == command);
}
