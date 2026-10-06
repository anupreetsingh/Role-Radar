using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using RoleRadar.Core;
using RoleRadar.Core.Tests;
using RoleRadar.Views;

namespace RoleRadar.Tests;

/// <summary>The app's windows as Windows draws them, on the real role-radar, in the smallest window: every Setup
/// page with the standard text and the biggest, and Live Tracking's tabs and the tray panel, light and dark.
/// Each is checked (Layout) and saved as a picture in build/windows-screenshots, which the CI keeps, so a
/// change to the windows can be checked and seen without a Windows PC.</summary>
public class WindowTests
{
    private static readonly string[] Tabs = ["new-jobs", "seen", "sent-alerts"];

    [Fact(Timeout = 300_000)]
    public Task EverySetupPageFits() => UiThread.Run(async () =>
    {
        var (model, cli, _) = RealCli.Made();
        var window = Open(model);
        await StartSetUp(model, cli);
        var problems = new List<string>();
        foreach (var (scale, size) in new[] { (TextSize.Standard, "standard"), (TextSize.High, "biggest") })
        {
            TextSize.Change(scale - TextSize.Scale);
            for (var i = 0; i < SetupRules.Pages.Length; i++)
            {
                var name = SetupRules.Pages[i];
                var marker = Find<Button>(window, button => Words(button).Contains(name));
                Assert.True(marker.IsEnabled, $"Setup's {name} step can't be opened");
                marker.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
                await UiThread.Settle(cli);
                problems.AddRange(Layout.Check(window, $"setup-{i + 1}-{name.ToLowerInvariant()}-{size}"));
            }
        }
        TextSize.Reset();
        window.Hide();
        Assert.True(problems.Count == 0, string.Join(Environment.NewLine, problems));
    });

    [Fact(Timeout = 300_000)]
    public Task LiveTrackingAndTheTrayPanelFit() => UiThread.Run(async () =>
    {
        var (model, cli, place) = RealCli.Made();
        var window = Open(model);
        await StartSetUp(model, cli);
        RealCli.SeedLiveTracking(place);
        window.ShowPage(setup: false);
        await model.RefreshAsync();
        await model.RefreshLiveAsync();
        await UiThread.Settle(cli);
        Assert.Contains(RealCli.LongTitle, Words(window)); // the jobs are listed

        var problems = new List<string>();
        var tabs = Find<TabControl>(window, _ => true);
        foreach (var theme in new[] { ThemeMode.Light, ThemeMode.Dark })
        {
            Application.Current.ThemeMode = theme;
            var look = theme.Value.ToLowerInvariant();
            for (var i = 0; i < Tabs.Length; i++)
            {
                tabs.SelectedIndex = i;
                await UiThread.Settle(cli);
                problems.AddRange(Layout.Check(window, $"live-{i + 1}-{Tabs[i]}-{look}"));
            }
            var panel = new PanelWindow(model, _ => { }, null, () => { });
            panel.ShowAt(new System.Drawing.Point(900, 700));
            await UiThread.Settle(cli);
            problems.AddRange(Layout.Check(panel, $"panel-{look}"));
            panel.Hide();
        }
        Application.Current.ThemeMode = ThemeMode.Light;
        window.Hide();
        Assert.True(problems.Count == 0, string.Join(Environment.NewLine, problems));
    });

    /// <summary>The window, at its smallest, as RoleRadar.exe makes it before the model starts.</summary>
    private static MainWindow Open(AppModel model) => new(model)
    {
        Width = 820, Height = 600, WindowStartupLocation = WindowStartupLocation.Manual, Left = 0, Top = 0,
    };

    /// <summary>Start the app, and do Setup as a person would: Tech, and their countries, roles and qualifications.</summary>
    private static async Task StartSetUp(AppModel model, RecordingCli cli)
    {
        await model.StartAsync(); // opens the window, on Setup
        Assert.Null(await model.SetupStepAsync(["profession"], new Dictionary<string, string> { ["profession"] = "tech" }));
        var answers = new SetupRules.Profile(model.Setup!.Roles, model.Setup.Exclude, [], ["US", "IN"], 2, "bachelors");
        Assert.Null(await model.SetupStepAsync(["profile"], answers.Json(withEducation: true)));
        Assert.True(model.Ready);
        await UiThread.Settle(cli);
    }

    private static T Find<T>(DependencyObject parent, Func<T, bool> wanted) where T : FrameworkElement =>
        Descendants(parent).OfType<T>().FirstOrDefault(e => e.IsVisible && wanted(e))
        ?? throw new InvalidOperationException($"No {typeof(T).Name} like that on screen");

    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var inner in Descendants(child))
            {
                yield return inner;
            }
        }
    }

    private static List<string> Words(DependencyObject parent) =>
        Descendants(parent).OfType<TextBlock>().Where(t => t.IsVisible).Select(t => t.Text).ToList();
}
