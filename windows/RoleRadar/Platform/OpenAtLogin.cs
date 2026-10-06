using Microsoft.Win32;

namespace RoleRadar.Platform;

/// <summary>Opening at login, in the tray only: the app's entry in this user's Run key (Settings > Apps >
/// Startup lists it). An installed app only: a dev build never opens at login.</summary>
public static class OpenAtLogin
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    public const string Flag = "--login"; // how the app knows it was opened at login

    public static bool IsOn(string name)
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKey);
        return key?.GetValue(name) is not null;
    }

    public static void Set(string name, bool on)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey);
        if (on)
        {
            key.SetValue(name, $"\"{Environment.ProcessPath}\" {Flag}");
        }
        else
        {
            key.DeleteValue(name, throwOnMissingValue: false);
        }
    }
}
