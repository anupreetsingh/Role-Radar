using System.IO;

namespace RoleRadar.Platform;

/// <summary>The app's own log (app.log, beside its files): a problem in the window or the tray says what
/// happened. The checker keeps its own (checker.log).</summary>
public static class Log
{
    private static string? path;
    private static readonly object Lock = new();

    public static void Start(string file)
    {
        path = file;
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        if (File.Exists(file) && new FileInfo(file).Length > 1_000_000)
        {
            File.Move(file, file + ".1", overwrite: true);
        }
    }

    public static void Info(string message) => Write("INFO", message);

    public static void Error(string message, Exception? error = null) => Write("ERROR", error is null ? message : $"{message}: {error}");

    private static void Write(string level, string message)
    {
        if (path is null)
        {
            return;
        }
        lock (Lock)
        {
            try
            {
                File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {level,-5} {message}{Environment.NewLine}");
            }
            catch (IOException)
            {
                // The log can't be written: nothing to tell it to.
            }
        }
    }
}
