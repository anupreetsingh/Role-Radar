// The app's one window: Setup's pages until they're done (or when asked for again), otherwise Live
// Tracking, switching in place rather than opening another window. Closing it leaves the tray icon,
// still checking. Its toolbar has the way between the two, and smaller and bigger text.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using RoleRadar.Core;

namespace RoleRadar.Views;

public sealed class MainWindow : Window
{
    private readonly AppModel model;
    private readonly SetupView setup;
    private readonly LiveView live;
    private readonly Button toSetup, toLive, smaller, bigger;

    public MainWindow(AppModel model)
    {
        this.model = model;
        setup = new SetupView(model);
        live = new LiveView(model);
        setup.Finished += () => ShowPage(setup: false);
        Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Assets/AppIcon.ico"));
        Width = 1000;
        Height = 780;
        MinWidth = 820;
        MinHeight = 600;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;

        toSetup = ToolbarButton(Glyphs.Settings, "Edit Setup", "Change your profession, countries, roles, qualifications or alerts",
                                () => ShowPage(setup: true));
        toLive = ToolbarButton(Glyphs.List, "Live Tracking", "Back to the new jobs; what you changed here is saved", () => ShowPage(setup: false));
        smaller = ToolbarButton(Glyphs.FontSmaller, null, "Smaller text (Ctrl+−)", () => TextSize.Change(-0.1));
        bigger = ToolbarButton(Glyphs.FontBigger, null, "Bigger text (Ctrl+=)", () => TextSize.Change(0.1));
        var toolbar = Ui.Spread(Ui.Row(4, toSetup, toLive), smaller, bigger);
        toolbar.Margin = new Thickness(8, 6, 8, 6);
        var pages = new Grid();
        pages.Children.Add(setup);
        pages.Children.Add(live);
        var layout = new DockPanel();
        DockPanel.SetDock(toolbar, Dock.Top);
        layout.Children.Add(toolbar);
        layout.Children.Add(pages);
        Content = layout;

        foreach (var (keys, step) in new[] { (new[] { Key.OemPlus, Key.Add }, 0.1), (new[] { Key.OemMinus, Key.Subtract }, -0.1) })
        {
            foreach (var key in keys)
            {
                InputBindings.Add(new KeyBinding(new Command(() => TextSize.Change(step)), key, ModifierKeys.Control));
            }
        }
        InputBindings.Add(new KeyBinding(new Command(TextSize.Reset), Key.D0, ModifierKeys.Control));
        InputBindings.Add(new KeyBinding(new Command(TextSize.Reset), Key.NumPad0, ModifierKeys.Control));
        TextSize.Changed += ShowToolbar;
        model.SetupChanged += ShowToolbar;
        model.WindowWanted += BringUp;
        ShowPage(setup: true);
    }

    private static Button ToolbarButton(string glyph, string? text, string tip, Action action)
    {
        var content = Ui.Row(6, Ui.Glyph(glyph, 13), text is null ? null : Ui.Text(text, 12, wrap: false));
        var button = new Button
        {
            Content = content, ToolTip = tip, Padding = new Thickness(8, 4, 8, 4), Background = System.Windows.Media.Brushes.Transparent,
            BorderThickness = new Thickness(0),
        };
        ToolTipService.SetShowOnDisabled(button, true);
        button.Click += (_, _) => action();
        return button;
    }

    private bool OnSetup => setup.Visibility == Visibility.Visible;

    private void ShowToolbar()
    {
        toSetup.Visible(!OnSetup);
        toLive.Visible(OnSetup && model.Ready);
        smaller.IsEnabled = TextSize.Scale > TextSize.Low;
        bigger.IsEnabled = TextSize.Scale < TextSize.High;
    }

    public void ShowPage(bool setup)
    {
        if (!setup && OnSetup)
        {
            this.setup.SaveIfChanged();
        }
        model.ShowingSetup = setup;
        this.setup.Visible(setup);
        live.Visible(!setup);
        Title = (setup ? "Role Radar Setup" : "Live Tracking") + (model.Place.Dev ? " (dev)" : "");
        ShowToolbar();
    }

    /// <summary>Open the window, in front, on the page the model asks for.</summary>
    public void BringUp()
    {
        ShowPage(model.ShowingSetup || !model.Ready);
        Show();
        if (WindowState == WindowState.Minimized)
        {
            WindowState = WindowState.Normal;
        }
        Activate();
    }

    /// <summary>Closing it keeps what was changed in Setup, and leaves the tray icon, still checking.</summary>
    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        if (OnSetup)
        {
            setup.SaveIfChanged();
        }
        e.Cancel = true;
        Hide();
    }

    /// <summary>A keyboard shortcut's action.</summary>
    private sealed class Command(Action run) : ICommand
    {
        public event EventHandler? CanExecuteChanged { add { } remove { } }
        public bool CanExecute(object? parameter) => true;
        public void Execute(object? parameter) => run();
    }
}
