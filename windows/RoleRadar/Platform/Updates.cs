using System.Runtime.InteropServices;
using RoleRadar.Core;

namespace RoleRadar.Platform;

/// <summary>Updates for the installed app, with WinSparkle (https://winsparkle.org), the Windows Sparkle.
///
/// app.json names the feed (the latest GitHub release's appcast-windows.xml) and the public key updates
/// must be signed with: the Mac app's, so scripts/publish_update.sh signs both with one key. It looks for
/// a newer version every six hours, and "Check for Updates…" looks now; it shows its own windows,
/// downloads the installer, checks its signature, and runs it silently (/S) once the app has quit. The
/// installer then opens the new version. A dev build has no updater.</summary>
public sealed class Updates
{
    private const string Dll = "WinSparkle.dll";
    private const int CheckEvery = 6 * 3600; // seconds

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int CanQuit();

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate void QuitNow();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_set_appcast_url([MarshalAs(UnmanagedType.LPStr)] string url);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern int win_sparkle_set_eddsa_public_key([MarshalAs(UnmanagedType.LPStr)] string key);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Unicode)]
    private static extern void win_sparkle_set_app_details(string company, string app, string version);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_set_automatic_check_for_updates(int on);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_set_update_check_interval(int seconds);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_set_can_shutdown_callback(CanQuit callback);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_set_shutdown_request_callback(QuitNow callback);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_init();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_check_update_with_ui();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void win_sparkle_cleanup();

    private CanQuit? canQuit; // kept while WinSparkle may call them
    private QuitNow? quitNow;

    public bool Available { get; private set; }

    /// <summary>Start looking for updates. `quit` is called (from WinSparkle's own thread) once it has
    /// started the installer.</summary>
    public void Start(Place place, Action quit)
    {
        if (Available || place.Dev || place.Feed is null || place.UpdateKey is null)
        {
            return;
        }
        try
        {
            win_sparkle_set_appcast_url(place.Feed);
            if (win_sparkle_set_eddsa_public_key(place.UpdateKey) == 0)
            {
                Log.Error("No updates: WinSparkle refused the update key");
                return;
            }
            win_sparkle_set_app_details("Role Radar", place.Name, place.Version);
            win_sparkle_set_automatic_check_for_updates(1); // without asking first: the Mac app doesn't either
            win_sparkle_set_update_check_interval(CheckEvery);
            canQuit = () => 1;
            quitNow = () => quit();
            win_sparkle_set_can_shutdown_callback(canQuit);
            win_sparkle_set_shutdown_request_callback(quitNow);
            win_sparkle_init();
            Available = true;
        }
        catch (Exception error) when (error is DllNotFoundException or EntryPointNotFoundException or BadImageFormatException)
        {
            Log.Error("No updates", error);
        }
    }

    /// <summary>Look for an update now, showing WinSparkle's window either way.</summary>
    public void Check()
    {
        if (Available)
        {
            win_sparkle_check_update_with_ui();
        }
    }

    public void Stop()
    {
        if (Available)
        {
            win_sparkle_cleanup();
            Available = false;
        }
    }
}
