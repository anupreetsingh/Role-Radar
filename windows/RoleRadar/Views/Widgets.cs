// The pieces the windows share, each the Mac app's of the same name: a switch with its icon and what
// it's doing, a status line, a problem said plainly (Trouble), job title chips, the ⓘ beside a heading
// with its pop-over example, and a button that says how its action went.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace RoleRadar.Views;

/// <summary>Something's working: a small turning arc.</summary>
public sealed class Spinner : ContentControl
{
    private readonly RotateTransform turn = new();
    private readonly DoubleAnimation spin = new(0, 360, TimeSpan.FromSeconds(0.9)) { RepeatBehavior = RepeatBehavior.Forever };

    public Spinner(double size = 14)
    {
        var arc = new Path
        {
            Data = Geometry.Parse("M 7,1 A 6,6 0 1 1 1,7"), StrokeThickness = 2, StrokeStartLineCap = PenLineCap.Round,
            StrokeEndLineCap = PenLineCap.Round, Width = 14, Height = 14, Stretch = Stretch.None,
            RenderTransform = turn, RenderTransformOrigin = new Point(0.5, 0.5),
        };
        arc.SetResourceReference(Shape.StrokeProperty, Theme.Secondary);
        Content = new Viewbox { Child = arc, Width = size, Height = size };
        VerticalAlignment = VerticalAlignment.Center;
        IsVisibleChanged += (_, _) => turn.BeginAnimation(RotateTransform.AngleProperty, IsVisible ? spin : null);
    }
}

/// <summary>A switch with its icon, name and what it's doing.</summary>
public sealed class SwitchRow : Grid
{
    private readonly Border badge = new() { Width = 28, Height = 28, CornerRadius = new CornerRadius(14) };
    private readonly TextBlock glyph;
    private readonly TextBlock detail = Ui.Secondary("", 11);
    private readonly Button action = Ui.Button("", size: 11);
    private readonly Spinner spinner = new();
    private readonly CheckBox toggle = new();
    private Action? run;

    /// <summary>The person flipped it: on or off.</summary>
    public event Action<bool>? Flipped;

    public SwitchRow(string title, string icon)
    {
        glyph = Ui.Glyph(icon, 13);
        glyph.HorizontalAlignment = HorizontalAlignment.Center;
        badge.Child = glyph;
        toggle.SetResourceReference(StyleProperty, "Switch");
        toggle.Margin = new Thickness(10, 0, 0, 0);
        toggle.Click += (_, _) => Flipped?.Invoke(toggle.IsChecked == true);
        System.Windows.Automation.AutomationProperties.SetName(toggle, title);
        action.Margin = new Thickness(8, 0, 0, 0);
        action.Click += (_, _) => run?.Invoke();
        spinner.Margin = new Thickness(8, 0, 0, 0);
        var words = Ui.Column(1, Ui.Text(title, 13, FontWeights.SemiBold), detail);
        words.Margin = new Thickness(10, 0, 0, 0);
        words.VerticalAlignment = VerticalAlignment.Center;
        foreach (var width in new[] { GridLength.Auto, new GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto, GridLength.Auto })
        {
            ColumnDefinitions.Add(new ColumnDefinition { Width = width });
        }
        UIElement[] parts = [badge, words, spinner, action, toggle];
        for (var i = 0; i < parts.Length; i++)
        {
            SetColumn(parts[i], i);
            Children.Add(parts[i]);
        }
        ShowState("", false, false, false);
    }

    public void ShowState(string text, bool on, bool active, bool busy, bool locked = false, (string Title, Action Run)? offer = null)
    {
        detail.Text = text;
        badge.SetResourceReference(Border.BackgroundProperty, on ? (active ? Theme.Success : Theme.Accent) : Theme.Control);
        glyph.SetResourceReference(TextBlock.ForegroundProperty, on ? Theme.OnAccent : Theme.Secondary);
        toggle.IsChecked = on;
        toggle.Tag = on && active ? "active" : null;
        toggle.IsEnabled = !busy && !locked;
        spinner.Visible(busy);
        action.Visible(offer is not null && !busy);
        if (offer is { } given)
        {
            action.Content = given.Title;
            run = given.Run;
        }
    }
}

/// <summary>A symbol and a line of secondary text: the round, the jobs waiting, the failing sites.</summary>
public sealed class StatusLine : Grid
{
    private readonly TextBlock symbol = Ui.Glyph("", 11);
    private readonly TextBlock text = Ui.Secondary("", 11);

    public StatusLine()
    {
        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(20) });
        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        symbol.VerticalAlignment = VerticalAlignment.Top;
        symbol.Margin = new Thickness(0, 2, 0, 0);
        SetColumn(text, 1);
        Children.Add(symbol);
        Children.Add(text);
    }

    public void Set(string line, string glyph, string brush = Theme.Secondary)
    {
        text.Text = line;
        symbol.Text = glyph;
        symbol.SetResourceReference(TextBlock.ForegroundProperty, brush);
    }
}

/// <summary>Something that went wrong, said plainly, with a way to try again: never a silent blank.</summary>
public sealed class Trouble : Border
{
    private readonly TextBox message = new()
    {
        IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent, TextWrapping = TextWrapping.Wrap,
        FontFamily = new FontFamily("Cascadia Mono, Consolas"), Padding = new Thickness(0),
    };

    public Trouble(Func<Task> retry)
    {
        message.Size(11).Colored(Theme.Secondary);
        var again = Ui.Button("Try Again", size: 11);
        again.HorizontalAlignment = HorizontalAlignment.Left;
        again.Click += async (_, _) =>
        {
            again.Content = "Trying…";
            again.IsEnabled = false;
            await retry();
            again.Content = "Try Again";
            again.IsEnabled = true;
        };
        var heading = Ui.Row(6, Ui.Glyph(Glyphs.Warning, 12, Theme.Caution), Ui.Text("Role Radar hit a problem", 12, FontWeights.SemiBold, Theme.Caution));
        Child = Ui.Column(8, heading, message, again);
        Padding = new Thickness(12);
        CornerRadius = new CornerRadius(8);
        this.Colored(Theme.CautionBackground, BackgroundProperty);
        this.Visible(false);
    }

    public void ShowMessage(string? text)
    {
        message.Text = text ?? "";
        this.Visible(!string.IsNullOrEmpty(text));
    }
}

/// <summary>Job title chips, wrapping onto new lines; ticking one calls `toggled(title, on)`. Without it,
/// they're only shown (the ⓘ's examples).</summary>
public static class Chips
{
    public static WrapPanel Make(IEnumerable<string> titles, ISet<string>? ticked, Action<string, bool>? toggled = null)
    {
        var panel = new WrapPanel();
        foreach (var title in titles)
        {
            var chip = new ToggleButton { Content = title, IsChecked = ticked is null || ticked.Contains(title) };
            chip.SetResourceReference(FrameworkElement.StyleProperty, "Chip");
            if (toggled is null)
            {
                chip.IsHitTestVisible = false;
                chip.Focusable = false;
            }
            else
            {
                chip.Click += (_, _) => toggled(title, chip.IsChecked == true);
            }
            panel.Children.Add(chip);
        }
        return panel;
    }
}

/// <summary>An ⓘ beside a heading: a hint on hover, the full explanation in a pop-over on click.</summary>
public sealed class InfoButton : Button
{
    public InfoButton(string hint, Func<UIElement> content)
    {
        Content = Ui.Glyph(Glyphs.Info, 14, Theme.Secondary);
        ToolTip = hint;
        Padding = new Thickness(4, 2, 4, 2);
        Background = Brushes.Transparent;
        BorderThickness = new Thickness(0);
        Cursor = System.Windows.Input.Cursors.Hand;
        VerticalAlignment = VerticalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(this, "More about this");
        Click += (_, _) =>
        {
            var box = Ui.Card(content(), 18);
            box.Width = 400;
            box.SetResourceReference(Border.BackgroundProperty, Theme.Window);
            new Popup { Child = box, PlacementTarget = this, Placement = PlacementMode.Bottom, StaysOpen = false, AllowsTransparency = true, IsOpen = true };
        };
    }

    /// <summary>An example: the boxes ticked, drawn as Setup draws them, then the jobs that reach you and
    /// those that don't.</summary>
    public static UIElement Example(string rule, IReadOnlyList<string> targets, IReadOnlyList<string> words,
                                    IReadOnlyList<string> reach, IReadOnlyList<string> stopped)
    {
        var column = Ui.Column(14, Ui.Text(rule, 13), Ui.Text("You have ticked:", 13, FontWeights.SemiBold));
        foreach (var (name, titles) in new[] { ("Target roles", targets), ("Non-target roles", words) })
        {
            if (titles.Count > 0)
            {
                Ui.Add(column, Ui.Column(6, Ui.Secondary(name), Chips.Make(titles, null)));
            }
        }
        foreach (var (heading, titles, good) in new[] { ("Then, these reach you:", reach, true),
                                                        (reach.Count == 0 ? "Then, these don't:" : "And these don't:", stopped, false) })
        {
            if (titles.Count == 0)
            {
                continue;
            }
            var list = Ui.Column(6, Ui.Text(heading, 13, FontWeights.SemiBold));
            foreach (var title in titles)
            {
                Ui.Add(list, Ui.Row(8, Ui.Glyph(good ? Glyphs.CheckMark : Glyphs.Cancel, 12, good ? Theme.Success : Theme.Critical), Ui.Text(title, 13)), 6);
            }
            Ui.Add(column, list);
        }
        return column;
    }
}

/// <summary>A button, and once it has run, how that went: the Mac app's actionRow. Its action returns
/// (note, ok); `busy` hears when it starts and finishes, so the page's other buttons can wait.</summary>
public sealed class ActionRow : StackPanel
{
    public Button Button { get; }
    private readonly Spinner spinner = new();
    private readonly TextBlock note = Ui.Text("", 11);
    private readonly Func<Task<(string Note, bool Ok)>> action;
    private readonly Action<bool> busy;

    public ActionRow(string title, Func<Task<(string Note, bool Ok)>> action, Action<bool> busy)
    {
        Orientation = Orientation.Horizontal;
        Button = Ui.Button(title, size: 12);
        this.action = action;
        this.busy = busy;
        Button.Click += async (_, _) => await Run();
        spinner.Margin = note.Margin = new Thickness(10, 0, 0, 0);
        note.VerticalAlignment = VerticalAlignment.Center;
        note.MaxWidth = 460;
        spinner.Visible(false);
        note.Visible(false);
        Children.Add(Button);
        Children.Add(spinner);
        Children.Add(note);
    }

    private async Task Run()
    {
        spinner.Visible(true);
        note.Visible(false);
        busy(true);
        var (text, ok) = await action();
        spinner.Visible(false);
        busy(false);
        note.Text = (ok ? "✓ " : "⚠ ") + text;
        note.SetResourceReference(TextBlock.ForegroundProperty, ok ? Theme.Success : Theme.Critical);
        note.Visible(true);
    }
}
