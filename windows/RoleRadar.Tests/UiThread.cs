using System.Windows;
using System.Windows.Threading;
using RoleRadar.Core.Tests;

// One app, on one thread, for every test: WPF allows a single Application, and its windows belong to the thread
// that made them.
[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace RoleRadar.Tests;

/// <summary>The thread the app's windows run on, with the app's resources and look, as RoleRadar.exe starts
/// them (App.xaml, the Fluent theme, the text sizes), but without its start-up: no tray icon, no checker.</summary>
public static class UiThread
{
    private static readonly Lazy<Dispatcher> Started = new(Start);

    /// <summary>What went wrong in the windows' own handlers (which would end the app): each fails its test.</summary>
    public static List<Exception> Errors { get; } = [];

    /// <summary>Run a test on the windows' thread.</summary>
    public static async Task Run(Func<Task> test)
    {
        Errors.Clear();
        await Started.Value.InvokeAsync(test).Task.Unwrap();
        Assert.True(Errors.Count == 0, string.Join(Environment.NewLine, Errors));
    }

    /// <summary>Wait until no command is running and the windows have caught up with what they replied.</summary>
    public static async Task Settle(RecordingCli cli)
    {
        for (var quiet = 0; quiet < 3;)
        {
            await Task.Delay(100);
            quiet = cli.Running ? 0 : quiet + 1;
        }
        await Dispatcher.Yield(DispatcherPriority.ApplicationIdle);
    }

    private static Dispatcher Start()
    {
        Dispatcher? dispatcher = null;
        Exception? failed = null;
        using var ready = new ManualResetEventSlim();
        var thread = new Thread(() =>
        {
            try
            {
                dispatcher = Dispatcher.CurrentDispatcher;
                SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(dispatcher));
                dispatcher.UnhandledException += (_, e) =>
                {
                    Errors.Add(e.Exception);
                    e.Handled = true;
                };
                Application.ResourceAssembly = typeof(App).Assembly; // pack://application: is the app's (its icon), not the test runner's
                var app = new App();
                app.InitializeComponent();
                app.ThemeMode = ThemeMode.Light;
                TextSize.Apply();
            }
            catch (Exception error)
            {
                failed = error;
            }
            ready.Set();
            if (failed is null)
            {
                Dispatcher.Run();
            }
        }) { IsBackground = true, Name = "Windows" };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        ready.Wait();
        return failed is null ? dispatcher! : throw new InvalidOperationException("Couldn't start the app's windows", failed);
    }
}
