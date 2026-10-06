// The app's look, and helpers for building its windows in code. Colors are Windows 11's (the Fluent
// theme, light or dark as Windows is), named by their theme keys below; text sizes are the Mac app's,
// times the text size setting: it starts at Standard whenever the app opens, and A−/A+ (Ctrl+= / Ctrl+−
// / Ctrl+0) change it until the app quits. Every text sized with Ui.Size follows it.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;

namespace RoleRadar;

/// <summary>Windows 11's theme colors, by their keys in WPF's Fluent theme.</summary>
public static class Theme
{
    public const string Text = "TextFillColorPrimaryBrush";
    public const string Secondary = "TextFillColorSecondaryBrush";
    public const string Tertiary = "TextFillColorTertiaryBrush";
    public const string Accent = "AccentFillColorDefaultBrush";
    public const string AccentText = "AccentTextFillColorPrimaryBrush";
    public const string OnAccent = "TextOnAccentFillColorPrimaryBrush";
    public const string Card = "CardBackgroundFillColorDefaultBrush";
    public const string CardSecondary = "CardBackgroundFillColorSecondaryBrush";
    public const string CardStroke = "CardStrokeColorDefaultBrush";
    public const string Divider = "DividerStrokeColorDefaultBrush";
    public const string Subtle = "SubtleFillColorSecondaryBrush"; // under the pointer
    public const string SubtlePressed = "SubtleFillColorTertiaryBrush";
    public const string Control = "ControlFillColorDefaultBrush";
    public const string ControlStroke = "ControlStrongStrokeColorDefaultBrush";
    public const string Window = "SolidBackgroundFillColorBaseBrush";
    public const string Success = "SystemFillColorSuccessBrush";
    public const string Caution = "SystemFillColorCautionBrush";
    public const string Critical = "SystemFillColorCriticalBrush";
    public const string CautionBackground = "SystemFillColorCautionBackgroundBrush";
    public const string Symbols = "SymbolThemeFontFamily"; // Segoe Fluent Icons (Segoe MDL2 Assets on Windows 10)
}

/// <summary>The icon font's glyphs (Segoe Fluent Icons).</summary>
public static class Glyphs
{
    public const string Refresh = "";
    public const string Settings = "";
    public const string Mail = "";
    public const string Message = "";
    public const string Laptop = "";
    public const string Calculator = "";
    public const string Health = "";
    public const string Sync = "";
    public const string List = "";
    public const string Send = "";
    public const string CheckMark = "";
    public const string Completed = "";
    public const string Warning = "";
    public const string Cancel = "";
    public const string Info = "";
    public const string Search = "";
    public const string ChevronRight = "";
    public const string ChevronDown = "";
    public const string OpenOut = "";
    public const string FontSmaller = "";
    public const string FontBigger = "";
}

/// <summary>How big the app's text is: a multiple of each text's own size.</summary>
public static class TextSize
{
    public const double Standard = 1.08, Low = 0.85, High = 1.75;
    private static readonly int[] Sizes = [9, 10, 11, 12, 13, 14, 15, 18, 20, 22, 26];
    public static double Scale { get; private set; } = Standard;
    public static event Action? Changed;

    public static string Key(double size) => $"RR.Size.{size}";

    public static void Change(double step) => Set(Math.Round(Math.Clamp(Scale + step, Low, High), 2));

    public static void Reset() => Set(Standard);

    /// <summary>The sizes as resources, which every sized text follows.</summary>
    public static void Apply()
    {
        foreach (var size in Sizes)
        {
            Application.Current.Resources[Key(size)] = size * Scale;
        }
        Changed?.Invoke();
    }

    private static void Set(double scale)
    {
        if (scale != Scale)
        {
            Scale = scale;
            Apply();
        }
    }
}

public static class Ui
{
    /// <summary>Text of `size` (the Mac app's), following the text size setting.</summary>
    public static T Size<T>(this T element, double size, FontWeight? weight = null) where T : FrameworkElement
    {
        var property = element is TextBlock ? TextBlock.FontSizeProperty : Control.FontSizeProperty;
        element.SetResourceReference(property, TextSize.Key(size));
        if (weight is { } bold)
        {
            element.SetValue(element is TextBlock ? TextBlock.FontWeightProperty : Control.FontWeightProperty, bold);
        }
        return element;
    }

    /// <summary>A theme color for `property`, changing with light or dark mode.</summary>
    public static T Colored<T>(this T element, string brush, DependencyProperty? property = null) where T : FrameworkElement
    {
        element.SetResourceReference(property ?? (element is TextBlock ? TextBlock.ForegroundProperty : Control.ForegroundProperty), brush);
        return element;
    }

    public static TextBlock Text(string text = "", double size = 12, FontWeight? weight = null, string? brush = null, bool wrap = true)
    {
        var block = new TextBlock { Text = text, TextWrapping = wrap ? TextWrapping.Wrap : TextWrapping.NoWrap }.Size(size, weight);
        return brush is null ? block : block.Colored(brush);
    }

    public static TextBlock Secondary(string text = "", double size = 12) => Text(text, size, brush: Theme.Secondary);

    public static TextBlock Glyph(string glyph, double size = 14, string? brush = null)
    {
        var block = new TextBlock { Text = glyph, VerticalAlignment = VerticalAlignment.Center }.Size(size);
        block.SetResourceReference(TextBlock.FontFamilyProperty, Theme.Symbols);
        return brush is null ? block : block.Colored(brush);
    }

    public static Button Button(string text, Action? action = null, double size = 12, bool accent = false, string? tip = null)
    {
        var button = new Button { Content = text, ToolTip = tip }.Size(size);
        if (accent)
        {
            button.SetResourceReference(FrameworkElement.StyleProperty, "AccentButtonStyle");
        }
        if (action is not null)
        {
            button.Click += (_, _) => action();
        }
        return button;
    }

    /// <summary>A text link that runs `action`.</summary>
    public static TextBlock Link(string text, Action action, double size = 12)
    {
        var link = new Hyperlink(new Run(text));
        link.SetResourceReference(TextElement.ForegroundProperty, Theme.AccentText);
        link.Click += (_, _) => action();
        return new TextBlock(link).Size(size);
    }

    public static StackPanel Column(double spacing = 8, params UIElement?[] children)
    {
        var column = new StackPanel();
        foreach (var child in children)
        {
            Add(column, child, spacing);
        }
        return column;
    }

    public static StackPanel Row(double spacing = 8, params UIElement?[] children)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        foreach (var child in children)
        {
            Add(row, child, spacing);
        }
        return row;
    }

    /// <summary>Add to a column or row, `spacing` after the one before.</summary>
    public static void Add(StackPanel panel, UIElement? child, double spacing = 8)
    {
        if (child is null)
        {
            return;
        }
        if (panel.Children.Count > 0 && child is FrameworkElement element)
        {
            var margin = element.Margin;
            element.Margin = panel.Orientation == Orientation.Vertical
                ? new Thickness(margin.Left, margin.Top + spacing, margin.Right, margin.Bottom)
                : new Thickness(margin.Left + spacing, margin.Top, margin.Right, margin.Bottom);
        }
        panel.Children.Add(child);
    }

    /// <summary>A grid row with `left` taking the room and `right` at the end.</summary>
    public static Grid Spread(UIElement left, params UIElement[] right)
    {
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.Children.Add(left);
        foreach (var item in right)
        {
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(item, grid.ColumnDefinitions.Count - 1);
            if (item is FrameworkElement element)
            {
                element.Margin = new Thickness(8, element.Margin.Top, element.Margin.Right, element.Margin.Bottom);
                element.VerticalAlignment = VerticalAlignment.Center;
            }
            grid.Children.Add(item);
        }
        return grid;
    }

    /// <summary>A rounded box of related lines: the Mac app's card.</summary>
    public static Border Card(UIElement content, double padding = 12) =>
        new Border
        {
            Child = content, Padding = new Thickness(padding), CornerRadius = new CornerRadius(8), BorderThickness = new Thickness(1),
        }.Colored(Theme.Card, Border.BackgroundProperty).Colored(Theme.CardStroke, Border.BorderBrushProperty);

    public static Border Divider() => new Border { Height = 1 }.Colored(Theme.Divider, Border.BackgroundProperty);

    /// <summary>Shown, or taking no room.</summary>
    public static void Visible(this UIElement element, bool shown) =>
        element.Visibility = shown ? Visibility.Visible : Visibility.Collapsed;

    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Control, HintAdorner> Hints = new();

    /// <summary>The hint shown in an empty text box ("Search companies"); called again, it changes.</summary>
    public static T Hint<T>(this T box, string hint) where T : Control
    {
        if (Hints.TryGetValue(box, out var existing))
        {
            existing.Text = hint;
            return box;
        }
        var adorner = new HintAdorner(box, hint);
        Hints.Add(box, adorner);
        // On the box's adorner layer while it's on screen (a tab's page leaves the screen when another is picked).
        box.Loaded += (_, _) =>
        {
            if (VisualTreeHelper.GetParent(adorner) is null)
            {
                System.Windows.Documents.AdornerLayer.GetAdornerLayer(box)?.Add(adorner);
            }
        };
        box.Unloaded += (_, _) => (VisualTreeHelper.GetParent(adorner) as System.Windows.Documents.AdornerLayer)?.Remove(adorner);
        return box;
    }

    /// <summary>Open a web page in the browser.</summary>
    public static void Open(string url)
    {
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (System.ComponentModel.Win32Exception)
        {
            // No browser set: nothing to open it with.
        }
    }
}

/// <summary>The gray hint over an empty text box.</summary>
public sealed class HintAdorner : System.Windows.Documents.Adorner
{
    private readonly Control box;
    private string hint;

    public string Text
    {
        get => hint;
        set
        {
            hint = value;
            InvalidateVisual();
        }
    }

    public HintAdorner(Control box, string hint) : base(box)
    {
        this.box = box;
        this.hint = hint;
        IsHitTestVisible = false;
        if (box is TextBox text)
        {
            text.TextChanged += (_, _) => InvalidateVisual();
        }
        else if (box is PasswordBox password)
        {
            password.PasswordChanged += (_, _) => InvalidateVisual();
        }
        box.GotKeyboardFocus += (_, _) => InvalidateVisual();
        box.LostKeyboardFocus += (_, _) => InvalidateVisual();
    }

    protected override void OnRender(DrawingContext drawing)
    {
        var empty = box is TextBox text ? text.Text.Length == 0 : box is PasswordBox password && password.Password.Length == 0;
        if (!empty)
        {
            return;
        }
        var brush = box.TryFindResource(Theme.Tertiary) as Brush ?? Brushes.Gray;
        var formatted = new FormattedText(hint, System.Globalization.CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
            new Typeface(box.FontFamily, FontStyles.Normal, FontWeights.Normal, FontStretches.Normal), box.FontSize, brush,
            VisualTreeHelper.GetDpi(this).PixelsPerDip);
        var left = box.Padding.Left + box.BorderThickness.Left + 2;
        drawing.DrawText(formatted, new Point(left, (box.ActualHeight - formatted.Height) / 2));
    }
}
