using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using RoleRadar.Core;

namespace RoleRadar.Platform;

/// <summary>The tray icon (the notification area, by the clock): the Mac app's menu bar icon. Gray while
/// nothing is checking. Click it for the panel, right-click for its menu, double-click for the window.</summary>
public sealed class Tray : IDisposable
{
    private readonly System.Windows.Forms.NotifyIcon icon = new();
    private readonly System.Drawing.Icon normal = Load("AppIcon.ico");
    private readonly System.Drawing.Icon gray = Load("AppIconGray.ico");
    private readonly AppModel model;
    private readonly ContextMenu menu = new();
    private readonly Window anchor; // the window the menu belongs to, so clicking away closes it

    /// <summary>A left click: at this point on the screen (in pixels).</summary>
    public event Action<System.Drawing.Point>? Clicked;
    public event Action? DoubleClicked;

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    public Tray(AppModel model, Window anchor, IEnumerable<(string Title, Action Run)?> items)
    {
        this.model = model;
        this.anchor = anchor;
        foreach (var item in items)
        {
            if (item is { } entry)
            {
                var choice = new MenuItem { Header = entry.Title };
                choice.Click += (_, _) => entry.Run();
                menu.Items.Add(choice);
            }
            else
            {
                menu.Items.Add(new Separator());
            }
        }
        icon.MouseClick += (_, e) =>
        {
            if (e.Button == System.Windows.Forms.MouseButtons.Left)
            {
                Clicked?.Invoke(System.Windows.Forms.Control.MousePosition);
            }
            else if (e.Button == System.Windows.Forms.MouseButtons.Right)
            {
                ShowMenu();
            }
        };
        icon.MouseDoubleClick += (_, e) =>
        {
            if (e.Button == System.Windows.Forms.MouseButtons.Left)
            {
                DoubleClicked?.Invoke();
            }
        };
        model.StateChanged += Show;
        Show();
        icon.Visible = true;
    }

    private static System.Drawing.Icon Load(string name)
    {
        var resource = Application.GetResourceStream(new Uri($"pack://application:,,,/Assets/{name}"))!;
        using var stream = resource.Stream;
        return new System.Drawing.Icon(stream, System.Windows.Forms.SystemInformation.SmallIconSize);
    }

    private void ShowMenu()
    {
        // A menu from the tray closes when clicked away from only if its app is in front.
        SetForegroundWindow(new WindowInteropHelper(anchor).EnsureHandle());
        menu.Placement = System.Windows.Controls.Primitives.PlacementMode.MousePoint;
        menu.IsOpen = true;
    }

    private void Show()
    {
        icon.Icon = model.Checking is null ? gray : normal;
        var tip = model.Checking == "laptop" ? $"Role Radar: {Describe.Who.ToLowerInvariant()} is checking sites"
            : model.State is not null ? "Role Radar: nothing is checking sites" : "Role Radar";
        icon.Text = tip + (model.Place.Dev ? " (dev)" : "");
    }

    public void Dispose()
    {
        icon.Visible = false;
        icon.Dispose();
    }
}
