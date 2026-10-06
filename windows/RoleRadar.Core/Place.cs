// Where the app finds its Python, its files and its settings: the Mac app's Place.
//
// Installed (scripts/package_windows.sh), RoleRadar.exe sits beside app.json, which names the app, its
// files' folder in %LOCALAPPDATA%, its Credential Manager service and its update feed, and beside
// python\, the Python that runs Role Radar (role_radar: the checker, and the commands the app runs).
// Without app.json it's a build from the code (Visual Studio): "Role Radar Dev", an app of its own
// (its own files, checker and credentials) on the Python in $RR_PYTHON (or `python`), with the repo's
// role_radar, that checks no job sites unless RR_DEV_CHECKS=1.
using System.Reflection;
using System.Text.Json;

namespace RoleRadar.Core;

public sealed class Place
{
    public required string Name { get; init; } // "Role Radar", or "Role Radar Dev"
    public required string Home { get; init; } // its files: settings, state, logs
    public required string Keychain { get; init; } // its Credential Manager service: "<it>/SMTP_PASSWORD"...
    public required string Python { get; init; } // what runs `-m role_radar ...`
    public required bool Dev { get; init; }
    public required bool Checks { get; init; } // whether it may check job sites
    public bool Installed { get; init; }
    public string? Feed { get; init; } // the update feed, and the key updates must be signed with
    public string? UpdateKey { get; init; }
    public required string Version { get; init; }

    public string Config => System.IO.Path.Combine(Home, "companies.yaml");
    public string CheckerLog => System.IO.Path.Combine(Home, "checker.log");
    public string AppLog => System.IO.Path.Combine(Home, "app.log");

    /// <summary>Its checker's files and credentials are its own, apart from a checker run from the code.</summary>
    public IReadOnlyDictionary<string, string> Env => new Dictionary<string, string>
    {
        ["ROLE_RADAR_HOME"] = Home,
        ["ROLE_RADAR_KEYCHAIN"] = Keychain,
    };

    /// <summary>The place for the app in `folder` (where RoleRadar.exe is).</summary>
    public static Place Load(string folder, string? dataFolder = null, IDictionary<string, string?>? environment = null)
    {
        string? Env(string name) => environment is null ? Environment.GetEnvironmentVariable(name)
            : environment.TryGetValue(name, out var value) ? value : null;
        dataFolder ??= Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var version = typeof(Place).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "0.0.0";
        version = version.Split('+')[0]; // without the commit the SDK may add
        var info = System.IO.Path.Combine(folder, "app.json");
        if (!File.Exists(info))
        {
            const string name = "Role Radar Dev";
            return new Place
            {
                Name = name, Home = System.IO.Path.Combine(dataFolder, name), Keychain = name,
                Python = Env("RR_PYTHON") ?? "python", Dev = true, Checks = Env("RR_DEV_CHECKS") == "1",
                Version = version + "-dev",
            };
        }
        using var json = JsonDocument.Parse(File.ReadAllText(info));
        var root = json.RootElement;
        string? Text(string key) => root.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;
        bool Flag(string key) => root.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.True;
        var appName = Text("name") ?? "Role Radar";
        var dev = Flag("dev");
        return new Place
        {
            Name = appName,
            Home = System.IO.Path.Combine(dataFolder, Text("home") ?? appName),
            Keychain = Text("keychain") ?? appName,
            Python = System.IO.Path.Combine(folder, "python", "Role Radar Checker.exe"),
            Dev = dev,
            Checks = Flag("checks"),
            Installed = true,
            Feed = Text("feed"),
            UpdateKey = Text("update_key"),
            Version = (Text("version") ?? version) + (dev ? "-dev" : ""),
        };
    }
}
