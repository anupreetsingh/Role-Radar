// Checks per hour for the last 24 hours, and the day's totals: the Mac app's ActivityView, with this PC
// as the one runner (the Windows app has no Lambda).
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using RoleRadar.Core;

namespace RoleRadar.Views;

/// <summary>The bars, one an hour; hovering one picks it out.</summary>
public sealed class ActivityBars : FrameworkElement
{
    // Categorical slot 1 of the chart palette, stepped for light or dark.
    private static readonly Brush Light = Frozen(0x2A, 0x78, 0xD6), Dark = Frozen(0x39, 0x87, 0xE5);
    private List<ActivityHour> hours = [];
    private bool empty = true;
    private int? at;

    /// <summary>The hour under the pointer, or null.</summary>
    public event Action<ActivityHour?>? Hovered;

    public ActivityBars()
    {
        Height = 90;
        System.Windows.Automation.AutomationProperties.SetName(this, "Checks per hour over the last 24 hours, by this PC");
    }

    private static Brush Frozen(byte r, byte g, byte b)
    {
        var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
        brush.Freeze();
        return brush;
    }

    public static Brush BarBrush(FrameworkElement on)
    {
        var window = on.TryFindResource(Theme.Window) as SolidColorBrush;
        var dark = window is not null && window.Color.R + window.Color.G + window.Color.B < 3 * 128;
        return dark ? Dark : Light;
    }

    public void Show(Activity activity)
    {
        hours = activity.Hours;
        empty = activity.Checked == 0;
        InvalidateVisual();
    }

    private double Axis => Label("8,888").Width + 6;

    private Rect Plot => new(Axis, 4, Math.Max(1, ActualWidth - Axis - 2), Math.Max(1, ActualHeight - 4 - 18));

    /// <summary>The y axis' top: a round number at or above the busiest hour.</summary>
    private int Top
    {
        get
        {
            var peak = hours.Count == 0 ? 0 : hours.Max(h => h.Mac);
            for (var exponent = 1; ; exponent *= 10)
            {
                foreach (var step in new[] { 1, 2, 5 })
                {
                    if (step * exponent >= peak)
                    {
                        return Math.Max(1, step * exponent);
                    }
                }
            }
        }
    }

    private FormattedText Label(string text, string brush = Theme.Secondary, double size = 10) =>
        new(text, CultureInfo.GetCultureInfo("en-US"), FlowDirection.LeftToRight, new Typeface("Segoe UI"),
            size * TextSize.Scale, TryFindResource(brush) as Brush ?? Brushes.Gray, VisualTreeHelper.GetDpi(this).PixelsPerDip);

    protected override void OnRender(DrawingContext drawing)
    {
        var plot = Plot;
        var top = Top;
        var grid = new Pen(TryFindResource(Theme.Divider) as Brush ?? Brushes.LightGray, 1);
        foreach (var value in top > 1 ? new[] { 0, top / 2, top } : [0, top])
        {
            var y = Math.Round(plot.Bottom - plot.Height * value / top) + 0.5;
            drawing.DrawLine(grid, new Point(plot.Left, y), new Point(plot.Right, y));
            var label = Label(Describe.Number(value));
            drawing.DrawText(label, new Point(plot.Left - 4 - label.Width, y - label.Height / 2));
        }
        var slot = plot.Width / Math.Max(hours.Count, 1);
        var bars = BarBrush(this);
        for (var i = 0; i < hours.Count; i++)
        {
            var height = plot.Height * hours[i].Mac / top;
            drawing.PushOpacity(at is null || at == i ? 1 : 0.35);
            drawing.DrawRoundedRectangle(bars, null, new Rect(plot.Left + slot * i + slot * 0.14, plot.Bottom - height, slot * 0.72, height), 1.5, 1.5);
            drawing.Pop();
            if (Describe.Date(hours[i].Start) is { } when && when.Hour % 6 == 0)
            {
                var label = Label(HourLabel(when.Hour));
                drawing.DrawText(label, new Point(plot.Left + slot * (i + 0.5) - label.Width / 2, plot.Bottom + 3));
            }
        }
        if (empty)
        {
            var label = Label("No checks in the last 24 hours", size: 11);
            drawing.DrawText(label, new Point(plot.Left + (plot.Width - label.Width) / 2, plot.Top + (plot.Height - label.Height) / 2));
        }
    }

    public static string HourLabel(int hour) => $"{(hour % 12 == 0 ? 12 : hour % 12)} {(hour < 12 ? "AM" : "PM")}";

    protected override void OnMouseMove(MouseEventArgs e)
    {
        var plot = Plot;
        var x = e.GetPosition(this).X;
        int? hovered = x >= plot.Left && x <= plot.Right && hours.Count > 0
            ? Math.Clamp((int)((x - plot.Left) / (plot.Width / hours.Count)), 0, hours.Count - 1) : null;
        if (hovered != at)
        {
            at = hovered;
            InvalidateVisual();
            Hovered?.Invoke(at is { } index ? hours[index] : null);
        }
    }

    protected override void OnMouseLeave(MouseEventArgs e)
    {
        at = null;
        InvalidateVisual();
        Hovered?.Invoke(null);
    }

    protected override HitTestResult HitTestCore(PointHitTestParameters hit) => new PointHitTestResult(this, hit.HitPoint);
}

/// <summary>The bars with their legend and caption, and the day's totals.</summary>
public sealed class ActivityView : StackPanel
{
    private readonly ActivityBars bars = new();
    private readonly TextBlock caption = Ui.Secondary("", 11);
    private readonly Dictionary<string, TextBlock> stats = new();

    public ActivityView()
    {
        var swatch = new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(2), Margin = new Thickness(0, 0, 4, 0) };
        Loaded += (_, _) => swatch.Background = ActivityBars.BarBrush(this);
        var legend = Ui.Row(0, swatch, Ui.Secondary(Describe.Who, 11));
        legend.VerticalAlignment = VerticalAlignment.Center;
        Ui.Add(this, Ui.Spread(Ui.Text("Activity", 12, FontWeights.SemiBold), legend), 0);
        Ui.Add(this, bars);
        Ui.Add(this, caption);
        var totals = new UniformGrid4();
        foreach (var (key, name) in new[] { ("checked", "checks"), ("new_jobs", "new jobs"), ("matches", "matches"), ("alerts", "alerts sent") })
        {
            var value = Ui.Text("0", 15, FontWeights.SemiBold);
            stats[key] = value;
            totals.Children.Add(Ui.Column(1, value, Ui.Secondary(name, 10)));
        }
        Ui.Add(this, totals);
        bars.Hovered += Caption;
        Caption(null);
    }

    public void Show(Activity activity)
    {
        bars.Show(activity);
        stats["checked"].Text = Describe.Compact(activity.Checked);
        stats["new_jobs"].Text = Describe.Compact(activity.NewJobs);
        stats["matches"].Text = Describe.Compact(activity.Matches);
        stats["alerts"].Text = Describe.Compact(activity.Alerts);
    }

    private void Caption(ActivityHour? hour) =>
        caption.Text = hour is not null && Describe.Date(hour.Start) is { } when
            ? $"{ActivityBars.HourLabel(when.Hour)}: {Describe.Who} {Describe.Number(hour.Mac)} checks"
            : "Checks per hour · hover a bar for details";

    /// <summary>Four columns of equal width.</summary>
    private sealed class UniformGrid4 : System.Windows.Controls.Primitives.UniformGrid
    {
        public UniformGrid4() => Columns = 4;
    }
}
