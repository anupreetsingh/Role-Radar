using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using RoleRadar.Core.Tests;

namespace RoleRadar.Tests;

/// <summary>What's wrong with what a window shows, the way the VM showed it going wrong: a text box too narrow
/// to type in, a hint drawn for a box that isn't on screen, a button or box cut off at the right edge. And a
/// picture of it, in build/windows-screenshots, to look at without a Windows PC.</summary>
public static class Layout
{
    private const double Narrowest = 120; // a box narrower than this is a sliver: its hint spills out

    public static readonly string Pictures = Path.Combine(RealCli.Project, "build", "windows-screenshots");
    private static readonly Lazy<string> Folder = new(() =>
    {
        if (Directory.Exists(Pictures))
        {
            Directory.Delete(Pictures, recursive: true); // this run's pictures only
        }
        return Directory.CreateDirectory(Pictures).FullName;
    });

    /// <summary>The window's problems, each starting with `name`, and its picture saved as `name`.png.</summary>
    public static List<string> Check(Window window, string name)
    {
        window.UpdateLayout();
        if (!window.IsVisible)
        {
            return [$"{name}: the window closed"];
        }
        var problems = Problems((FrameworkElement)window.Content).Select(problem => $"{name}: {problem}").ToList();
        Save(window, name);
        return problems;
    }

    private static IEnumerable<string> Problems(FrameworkElement content)
    {
        foreach (var element in Shown(content))
        {
            if (element is TextBox or PasswordBox or ComboBox && element.ActualWidth < Narrowest)
            {
                yield return $"{Describe(element)} is {element.ActualWidth:0} px wide, too narrow to type in";
            }
            if (element is ButtonBase or TextBox or PasswordBox or ComboBox && Beyond(element, content) is var beyond and > 1)
            {
                yield return $"{Describe(element)} runs {beyond:0} px past the right edge";
            }
        }
        foreach (var box in All(content).OfType<Control>().Where(c => c is TextBox or PasswordBox && !c.IsVisible))
        {
            if (Hint(box) is { IsVisible: true })
            {
                yield return $"{Describe(box)} isn't on screen, but its hint is";
            }
        }
    }

    /// <summary>Everything on screen, without looking inside a box's own parts.</summary>
    private static IEnumerable<FrameworkElement> Shown(DependencyObject parent)
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            if (VisualTreeHelper.GetChild(parent, i) is not FrameworkElement { IsVisible: true } child)
            {
                continue;
            }
            yield return child;
            if (child is not (TextBox or PasswordBox or ComboBox))
            {
                foreach (var inner in Shown(child))
                {
                    yield return inner;
                }
            }
        }
    }

    private static IEnumerable<DependencyObject> All(DependencyObject parent)
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var inner in All(child))
            {
                yield return inner;
            }
        }
    }

    /// <summary>How far past the right edge of what holds it (the scrolled view it's in, or the window) it reaches.</summary>
    private static double Beyond(FrameworkElement element, FrameworkElement content)
    {
        FrameworkElement holder = content;
        for (var parent = VisualTreeHelper.GetParent(element); parent is not null && parent != content; parent = VisualTreeHelper.GetParent(parent))
        {
            if (parent is ScrollContentPresenter view)
            {
                holder = view;
                break;
            }
        }
        var bounds = element.TransformToAncestor(holder).TransformBounds(new Rect(element.RenderSize));
        return bounds.Right - holder.ActualWidth;
    }

    private static HintAdorner? Hint(Control box) =>
        AdornerLayer.GetAdornerLayer(box)?.GetAdorners(box)?.OfType<HintAdorner>().FirstOrDefault();

    /// <summary>A control as a person would name it: its words, or its hint.</summary>
    private static string Describe(FrameworkElement element)
    {
        var words = element is Control box && Hint(box) is { } hint ? hint.Text
            : element is ContentControl { Content: string text } ? text
            : string.Join(" ", All(element).OfType<TextBlock>().Select(t => t.Text).Where(t => t.Any(char.IsLetterOrDigit)));
        return $"{element.GetType().Name} \"{(words.Length > 60 ? words[..60] + "…" : words)}\"";
    }

    /// <summary>The window as it looks, its hints included, on its own background.</summary>
    private static void Save(Window window, string name)
    {
        var root = (FrameworkElement)VisualTreeHelper.GetChild(window, 0); // the window's inside, with the layer hints are drawn on
        var dpi = VisualTreeHelper.GetDpi(window);
        var size = new Rect(0, 0, root.ActualWidth, root.ActualHeight);
        var picture = new RenderTargetBitmap((int)Math.Ceiling(size.Width * dpi.DpiScaleX), (int)Math.Ceiling(size.Height * dpi.DpiScaleY),
                                             dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
        var background = new DrawingVisual();
        using (var drawing = background.RenderOpen())
        {
            drawing.DrawRectangle(window.TryFindResource(Theme.Window) as Brush ?? Brushes.White, null, size);
        }
        picture.Render(background);
        picture.Render(root);
        var png = new PngBitmapEncoder();
        png.Frames.Add(BitmapFrame.Create(picture));
        using var file = File.Create(Path.Combine(Folder.Value, name + ".png"));
        png.Save(file);
    }
}
