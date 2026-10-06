// The panel that opens from the tray icon: the Mac app's menu bar Panel. Who's checking, the checker's
// switch and the alerts', the round and the jobs waiting, the way into Live Tracking or Setup, Open at
// Login, the log, Quit, and updates. It closes when clicked away from, as Windows' own flyouts do.
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using RoleRadar.Core;
using RoleRadar.Platform;

namespace RoleRadar.Views;

public sealed class PanelWindow : Window
{
    private readonly AppModel model;
    private readonly Action<bool> openWindow;
    private readonly TextBlock headline = Ui.Text("", 12, wrap: false);
    private readonly SwitchRow pc = new(Describe.Who, Glyphs.Laptop);
    private readonly SwitchRow discord = new("Discord", Glyphs.Message), email = new("Email", Glyphs.Mail);
    private readonly TextBlock alertsNote = Ui.Secondary("", 11);
    private readonly Border unready, health;
    private readonly StatusLine round = new(), waiting = new(), failing = new();
    private readonly Trouble trouble;
    private readonly CheckBox login = new() { Content = "Open at Login" };

    /// <summary>When it last closed: a click on the tray icon that closed it mustn't open it again.</summary>
    public DateTime HiddenAt { get; private set; }

    public PanelWindow(AppModel model, Action<bool> openWindow, Action? checkUpdates, Action quit)
    {
        this.model = model;
        this.openWindow = openWindow;
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = Brushes.Transparent;
        ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false;
        Topmost = true;
        SizeToContent = SizeToContent.Height;
        Width = 340;
        Title = "Role Radar";

        var refresh = new Button { Content = Ui.Glyph(Glyphs.Refresh, 13), ToolTip = "Refresh", Background = Brushes.Transparent, BorderThickness = new Thickness(0) };
        refresh.Click += async (_, _) => await model.RefreshAsync();
        pc.Flipped += on => _ = model.SetAsync("laptop", on);
        discord.Flipped += on => _ = model.SetAsync("discord", on);
        email.Flipped += on => _ = model.SetAsync("email", on);
        trouble = new Trouble(model.RetryAsync);

        unready = Ui.Card(Ui.Column(8,
            Ui.Secondary("Finish setting up: pick your profession, countries and the roles you want.", 11),
            Wide(Ui.Button("Set Up Role Radar", () => Open(true), 13, accent: true))));
        health = Ui.Card(Ui.Column(8, round, waiting, failing,
            Wide(Ui.Button("Live Tracking", () => Open(false), 13, accent: true,
                tip: "Matches waiting to be sent, seen and sent, the round in progress, and activity"))));

        login.Size(12);
        login.Visible(model.Place.Installed && !model.Place.Dev);
        login.Click += (_, _) => SetLogin(login.IsChecked == true);
        var footer = Ui.Spread(login,
            Ui.Button("Edit Setup…", () => Open(true), 12, tip: "Your profession, countries, roles, qualifications and alerts"),
            Ui.Button("Log", () => OpenFile(model.Place.CheckerLog), 12),
            Ui.Button("Quit", quit, 12, tip: "Also stops this PC's checker"));
        var version = checkUpdates is null
            ? (UIElement)Ui.Secondary($"Version {model.Place.Version}", 11)
            : Ui.Spread(Ui.Secondary($"Version {model.Place.Version}", 11), Ui.Link("Check for Updates…", checkUpdates, 11));

        var column = Ui.Column(12,
            Ui.Spread(Ui.Text("Role Radar", 13, FontWeights.SemiBold), refresh),
            headline,
            Ui.Card(pc, 10),
            Ui.Secondary("This PC checks while this app is open. Everything stays on this PC.", 11),
            Ui.Text("Alerts", 12, FontWeights.SemiBold),
            Ui.Card(Ui.Column(10, discord, email), 10),
            alertsNote, unready, health, trouble, Ui.Divider(), footer, version);
        var frame = new Border
        {
            Child = column, Padding = new Thickness(14), CornerRadius = new CornerRadius(8), BorderThickness = new Thickness(1),
        };
        frame.SetResourceReference(Border.BackgroundProperty, Theme.Window);
        frame.SetResourceReference(Border.BorderBrushProperty, Theme.CardStroke);
        Content = frame;

        Deactivated += (_, _) => Hide();
        IsVisibleChanged += (_, _) =>
        {
            if (!IsVisible)
            {
                HiddenAt = DateTime.UtcNow;
            }
        };
        model.StateChanged += ShowContent;
        model.SetupChanged += ShowContent;
        ShowContent();
    }

    private static Button Wide(Button button)
    {
        button.HorizontalAlignment = HorizontalAlignment.Stretch;
        return button;
    }

    /// <summary>Open beside the tray icon, clicked at `pixel` (the taskbar's side of the screen decides which way).</summary>
    public void ShowAt(System.Drawing.Point pixel)
    {
        _ = model.RefreshAsync();
        login.IsChecked = OpenAtLogin.IsOn(model.Place.Name);
        var handle = new WindowInteropHelper(this).EnsureHandle();
        var screen = System.Windows.Forms.Screen.FromPoint(pixel);
        var toDips = HwndSource.FromHwnd(handle)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
        var area = screen.WorkingArea;
        var topLeft = toDips.Transform(new Point(area.Left, area.Top));
        var bottomRight = toDips.Transform(new Point(area.Right, area.Bottom));
        var at = toDips.Transform(new Point(pixel.X, pixel.Y));
        Width = Math.Max(340, 310 * TextSize.Scale); // wider as the text grows, so lines don't wrap into a column
        ((FrameworkElement)Content).Measure(new Size(Width, double.PositiveInfinity));
        var height = ((FrameworkElement)Content).DesiredSize.Height;
        var x = Math.Clamp(at.X - Width / 2, topLeft.X + 8, bottomRight.X - Width - 8);
        // With the taskbar at the bottom (the usual), above the icon; at the top, below it.
        var y = at.Y > (topLeft.Y + bottomRight.Y) / 2 ? bottomRight.Y - height - 8 : topLeft.Y + 8;
        Left = x;
        Top = Math.Max(topLeft.Y + 8, y);
        Show();
        Activate();
    }

    private void Open(bool setup)
    {
        Hide();
        openWindow(setup);
    }

    private void SetLogin(bool on)
    {
        try
        {
            OpenAtLogin.Set(model.Place.Name, on);
        }
        catch (Exception error) when (error is UnauthorizedAccessException or System.Security.SecurityException or IOException)
        {
            model.Error = $"Open at Login: {error.Message}";
            ShowContent();
        }
        login.IsChecked = OpenAtLogin.IsOn(model.Place.Name);
    }

    private static void OpenFile(string path)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        if (!File.Exists(path))
        {
            File.WriteAllText(path, ""); // the log, before the checker's first line: an empty file, not an error
        }
        Ui.Open(path);
    }

    // -- what it shows ------------------------------------------------------------------------------------

    private void ShowContent()
    {
        var state = model.State;
        if (state is null)
        {
            headline.Text = model.Error is null ? "Loading…" : "Can't read the status";
            headline.Colored(model.Error is null ? Theme.Secondary : Theme.Caution);
        }
        else
        {
            headline.Text = model.Checking == "laptop" ? $"● {Describe.Who} is checking sites" : "● Nothing is checking sites";
            headline.Colored(model.Checking == "laptop" ? Theme.Success : Theme.Caution);
        }

        var on = model.Switch("laptop");
        var detail = state is null ? "" : !on ? "Off" : model.Checking == "laptop" ? $"Checking · last pass {LastPass()}"
            : "On, but role-radar start isn't running";
        // Switched on but not running: offer to start it.
        var needsStart = state is not null && on && state.LaptopAppPid is null && model.CanStart;
        pc.ShowState(detail, on, model.Checking == "laptop", model.Busy.Contains("laptop"), locked: state is null,
            offer: needsStart ? ("Start", () => _ = model.SetAsync("laptop", true)) : null);

        foreach (var (name, line) in new[] { ("discord", discord), ("email", email) })
        {
            var ready = model.ChannelReady(name);
            var switched = ready && model.Switch(name);
            var text = !ready ? "Not set up · add it in Edit Setup…" : switched ? "New jobs are sent here" : "Off";
            line.ShowState(text, switched, switched, model.Busy.Contains(name), locked: state is null || (!ready && !switched));
        }
        var alertsOff = state is not null && !model.Switch("discord") && !model.Switch("email");
        alertsNote.Text = alertsOff ? "Alerts are optional. Off, new jobs collect in Live Tracking, newest on top."
            : "New jobs go to the alerts switched on, every 10 minutes.";

        unready.Visible(model.Setup is not null && !model.Ready);
        health.Visible(model.Ready && state is not null);
        if (state is not null)
        {
            round.Set(Describe.RoundSummary(state.Round), Glyphs.Sync);
            waiting.Set(Describe.WaitingLine(state, null), Glyphs.List);
            var fail = Describe.FailingLine(state);
            failing.Visible(fail is not null);
            if (fail is { } line)
            {
                failing.Set(line.Text, line.Ok ? Glyphs.Completed : Glyphs.Warning, line.Ok ? Theme.Success : Theme.Caution);
            }
        }
        trouble.ShowMessage(model.Error ?? model.SetupError);
    }

    private string LastPass()
    {
        var last = model.State?.LastRuns.Where(run => run.Key.StartsWith("laptop") && run.Value.FinishedAt is not null)
            .Select(run => Describe.Date(run.Value.FinishedAt)).Max();
        return Describe.Ago(last);
    }
}
