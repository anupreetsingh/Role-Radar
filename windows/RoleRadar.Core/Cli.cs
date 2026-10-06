// Running role-radar commands: `<Python> -m role_radar ARGS --config <the app's companies file>`, with
// the app's own files and credentials (Place.Env), as the Mac app's Model.cli does. Every read and
// write goes through them, so the rules live in one place, role_radar, shared with the Mac app.
using System.ComponentModel;
using System.Diagnostics;
using System.Text;

namespace RoleRadar.Core;

/// <summary>A command's result: what it printed, or why it failed (the last line it printed to stderr).</summary>
public sealed record CliResult(bool Ok, string Output = "", string Message = "")
{
    /// <summary>What it printed, as JSON. JsonException if it isn't.</summary>
    public T Read<T>() where T : class => Json.Read<T>(Output) ?? throw new System.Text.Json.JsonException("empty reply");
}

public interface ICli
{
    /// <summary>Run `role-radar ARGS` and wait for it.</summary>
    Task<CliResult> RunAsync(IReadOnlyList<string> args, string? stdin = null);

    /// <summary>Start `role-radar ARGS` and don't wait: it outlives the app (the stop on quit).</summary>
    void Start(IReadOnlyList<string> args);
}

public sealed class ProcessCli(Place place) : ICli
{
    private static readonly UTF8Encoding Utf8 = new(encoderShouldEmitUTF8Identifier: false); // a BOM isn't JSON

    private ProcessStartInfo Info(IReadOnlyList<string> args)
    {
        Directory.CreateDirectory(place.Home);
        var info = new ProcessStartInfo(place.Python)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = place.Home,
        };
        foreach (var arg in new[] { "-m", "role_radar" }.Concat(args).Concat(["--config", place.Config]))
        {
            info.ArgumentList.Add(arg);
        }
        foreach (var (name, value) in place.Env)
        {
            info.Environment[name] = value;
        }
        info.Environment["PYTHONIOENCODING"] = "utf-8"; // a Python from the code; the app's own runs in UTF-8 mode
        return info;
    }

    public async Task<CliResult> RunAsync(IReadOnlyList<string> args, string? stdin = null)
    {
        var info = Info(args);
        info.RedirectStandardOutput = info.RedirectStandardError = info.RedirectStandardInput = true;
        info.StandardOutputEncoding = info.StandardErrorEncoding = info.StandardInputEncoding = Utf8;
        using var process = new Process { StartInfo = info };
        try
        {
            process.Start();
        }
        catch (Win32Exception error)
        {
            return new CliResult(false, Message: $"Couldn't run {place.Python}: {error.Message}");
        }
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        try
        {
            if (stdin is not null)
            {
                await process.StandardInput.WriteAsync(stdin).ConfigureAwait(false);
            }
            process.StandardInput.Close();
        }
        catch (IOException)
        {
            // It ended before reading it all: its exit status and stderr say why.
        }
        await process.WaitForExitAsync().ConfigureAwait(false);
        if (process.ExitCode == 0)
        {
            return new CliResult(true, await output.ConfigureAwait(false));
        }
        var lines = (await errors.ConfigureAwait(false)).Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        return new CliResult(false, Message: lines.Length > 0 ? lines[^1] : $"exit {process.ExitCode}");
    }

    public void Start(IReadOnlyList<string> args)
    {
        try
        {
            using var _ = Process.Start(Info(args));
        }
        catch (Win32Exception)
        {
            // Nothing to stop with: the checker stops at sign-out all the same.
        }
    }
}
