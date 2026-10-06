// Starting Role Radar: RoleRadar.exe, installed (scripts/package_windows.sh) or built from the code.
//
//   --login   opened at login: start in the tray only, unless Setup isn't done
//   --quit    ask a running Role Radar to quit, and stop its checker (the installer, before an update)
//
// Opening it while it runs shows the running one's window instead. It re-reads the state every minute,
// which also restarts the checker if it stopped, and stops the checker when it quits.
using System.Windows;
using System.Windows.Threading;
using RoleRadar.Core;
using RoleRadar.Platform;
using RoleRadar.Views;

namespace RoleRadar;

public partial class App : Application
{
    private AppModel? model;
    private Tray? tray;
    private Updates? updates;
    private SingleInstance? instance;
    private MainWindow? window;
    private PanelWindow? panel;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var place = Place.Load(AppContext.BaseDirectory);
        instance = new SingleInstance(place.Name);
        if (e.Args.Contains("--quit"))
        {
            instance.TellRunning("quit");
            await new ProcessCli(place).RunAsync(["stop"]); // and wait for the checker to finish the companies in flight
            Shutdown();
            return;
        }
        if (instance.TellRunning("show") || !instance.Claim())
        {
            Shutdown(); // opened again: the running one shows its window
            return;
        }
        instance.Listen(Dispatcher);

        Log.Start(place.AppLog);
        var atLogin = e.Args.Contains(OpenAtLogin.Flag);
        Log.Info($"Role Radar {place.Version} starting ({(atLogin ? "at login" : "opened")})");
        DispatcherUnhandledException += (_, args) =>
        {
            Log.Error("Unexpected error", args.Exception);
            args.Handled = true;
        };
        TaskScheduler.UnobservedTaskException += (_, args) => Log.Error("Unexpected error", args.Exception);

        ThemeMode = ThemeMode.System; // Windows 11's look, light or dark as Windows is
        TextSize.Apply();
        model = new AppModel(new ProcessCli(place), place, () => OpenAtLogin.Set(place.Name, true)) { LaunchedAtLogin = atLogin };
        window = new MainWindow(model);
        updates = new Updates();
        updates.Start(place, () => Dispatcher.BeginInvoke(Shutdown)); // from WinSparkle's thread, for its installer
        Action? checkUpdates = updates.Available ? updates.Check : null;
        panel = new PanelWindow(model, OpenWindow, checkUpdates, Shutdown);
        tray = new Tray(model, panel,
        [
            ("Open Role Radar", () => OpenWindow(!model.Ready)),
            ("Edit Setup…", () => OpenWindow(true)),
            null,
            checkUpdates is null ? null : ("Check for Updates…", checkUpdates),
            ("Open Log", () => Ui.Open(place.CheckerLog)),
            null,
            ("Quit Role Radar", Shutdown),
        ]);
        tray.Clicked += point =>
        {
            // Clicking the icon with the panel open closes it (as any click away from it does), and the click
            // then arrives here too: that one mustn't open it again.
            if (panel.IsVisible)
            {
                panel.Hide();
            }
            else if ((DateTime.UtcNow - panel.HiddenAt).TotalMilliseconds > 300)
            {
                panel.ShowAt(point);
            }
        };
        tray.DoubleClicked += () =>
        {
            panel.Hide();
            OpenWindow(!model.Ready);
        };
        // Opened again: its window, on Setup until that's done, otherwise as it was.
        instance.Message += message =>
        {
            if (message == "quit")
            {
                Shutdown();
            }
            else
            {
                model.ShowWindow(model.Ready ? null : true);
            }
        };

        Every(TimeSpan.FromMinutes(1), model.RefreshAsync);
        if (!place.Dev)
        {
            // The anonymous check-in, once Setup's files exist and the network is up after a login, then every 6 hours.
            After(TimeSpan.FromSeconds(30), async () =>
            {
                await model.CheckInAsync();
                Every(TimeSpan.FromHours(6), model.CheckInAsync);
            });
        }
        await model.StartAsync();
    }

    private void OpenWindow(bool setup) => model?.ShowWindow(setup);

    private void Every(TimeSpan interval, Func<Task> run)
    {
        var timer = new DispatcherTimer { Interval = interval };
        timer.Tick += async (_, _) => await run();
        timer.Start();
    }

    private void After(TimeSpan delay, Func<Task> run)
    {
        var timer = new DispatcherTimer { Interval = delay };
        timer.Tick += async (_, _) =>
        {
            timer.Stop();
            await run();
        };
        timer.Start();
    }

    /// <summary>Quit (or signing out, or an update) stops the checker with the app.</summary>
    protected override void OnExit(ExitEventArgs e)
    {
        if (model is not null)
        {
            Log.Info("Quitting");
            model.StopChecker();
        }
        updates?.Stop();
        tray?.Dispose();
        instance?.Dispose();
        base.OnExit(e);
    }
}
