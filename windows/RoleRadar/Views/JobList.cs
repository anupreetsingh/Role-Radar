// Live Tracking's job lists: the new jobs and those marked as seen, each row the Mac app's MatchRow (its
// look is Styles.xaml's JobRow). A row shows the title, its company and place, and when it was found
// (or seen). Clicking it opens the posting; its tick box picks it for acting on several at once; a
// seen job has its own Mark as New. Right-click for the rest.
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using RoleRadar.Core;

namespace RoleRadar.Views;

/// <summary>One job in a list, as its row shows it.</summary>
public sealed class JobRow(Match match, bool seen, Action picked) : INotifyPropertyChanged
{
    private bool isPicked;

    public event PropertyChangedEventHandler? PropertyChanged;

    public Match Match { get; private set; } = match;
    public bool Seen { get; } = seen;
    public string Title => Match.Title;
    public string Where => string.Join(" · ", new[] { Match.Company, Match.Location }.Where(s => !string.IsNullOrEmpty(s)));
    public string When => Seen ? $"Seen {Describe.Stamp(Match.SkippedAt)}" : $"Found {Describe.Stamp(Match.Found)}";
    public bool Sending => Match.SendAt is not null;
    public bool CanPick => !Sending;
    public double Fade => Seen ? 0.6 : 1;

    public bool Picked
    {
        get => isPicked;
        set
        {
            if (value == isPicked || (value && Sending))
            {
                return;
            }
            isPicked = value;
            Changed(nameof(Picked));
            picked();
        }
    }

    /// <summary>The same job, as the latest read has it (on its way out, now, say).</summary>
    public void Update(Match latest)
    {
        Match = latest;
        if (Sending)
        {
            isPicked = false;
        }
        Changed("");
    }

    /// <summary>"12 min. ago" moves on.</summary>
    public void Tick() => Changed(nameof(When));

    private void Changed(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>A list of jobs that keeps its place: new rows go in where they belong, and the rest stay put,
/// ticks and all.</summary>
public sealed class JobList : ItemsControl
{
    private ObservableCollection<JobRow> rows = [];
    private readonly bool seen;
    private JobRow? pressed;
    private bool bulk; // ticking many at once: one PicksChanged at the end

    /// <summary>The ticks changed.</summary>
    public event Action? PicksChanged;

    /// <summary>A seen job's Mark as New.</summary>
    public event Action<Match>? MarkNew;

    /// <summary>More for a row's right-click menu.</summary>
    public Func<Match, IEnumerable<(string Title, Action Run)>>? Menu { get; set; }

    public JobList(bool seen)
    {
        this.seen = seen;
        SetResourceReference(StyleProperty, "JobList");
        SetResourceReference(ItemTemplateProperty, "JobRow");
        ItemsSource = rows;
        Focusable = false;
        ContextMenu = new ContextMenu(); // filled for the row right-clicked, as it opens
        AddHandler(System.Windows.Controls.Primitives.ButtonBase.ClickEvent, new RoutedEventHandler(OnButton));
        // "Found 2:32 PM (12 min. ago)": kept current, once a minute.
        var clock = new DispatcherTimer { Interval = TimeSpan.FromMinutes(1) };
        clock.Tick += (_, _) =>
        {
            foreach (var row in rows)
            {
                row.Tick();
            }
        };
        clock.Start();
    }

    public IReadOnlyList<JobRow> Rows => rows;

    /// <summary>The jobs a tick box can pick: not those on their way out.</summary>
    public List<Match> Pickable => rows.Where(r => r.CanPick).Select(r => r.Match).ToList();

    public List<Match> Picked => rows.Where(r => r.Picked).Select(r => r.Match).ToList();

    public void PickAll(bool on)
    {
        bulk = true;
        foreach (var row in rows)
        {
            row.Picked = on && row.CanPick;
        }
        bulk = false;
        PicksChanged?.Invoke();
    }

    private void RowPicked()
    {
        if (!bulk)
        {
            PicksChanged?.Invoke();
        }
    }

    /// <summary>Show these jobs, in this order, changing only the rows that changed.</summary>
    public void Show(IReadOnlyList<Match> jobs)
    {
        var kept = rows.ToDictionary(r => r.Match.Id);
        var target = jobs.Select(job =>
        {
            if (kept.TryGetValue(job.Id, out var row))
            {
                row.Update(job);
                return row;
            }
            return new JobRow(job, seen, RowPicked);
        }).ToList();
        if (rows.Count == 0)
        {
            rows = new ObservableCollection<JobRow>(target); // a first list, however long, in one go
            ItemsSource = rows;
            PicksChanged?.Invoke();
            return;
        }
        var wanted = target.Select(r => r.Match.Id).ToHashSet();
        for (var i = rows.Count - 1; i >= 0; i--)
        {
            if (!wanted.Contains(rows[i].Match.Id))
            {
                rows.RemoveAt(i);
            }
        }
        for (var i = 0; i < target.Count; i++)
        {
            if (i < rows.Count && ReferenceEquals(rows[i], target[i]))
            {
                continue;
            }
            var at = rows.IndexOf(target[i]);
            if (at >= 0)
            {
                rows.Move(at, i);
            }
            else
            {
                rows.Insert(i, target[i]);
            }
        }
        PicksChanged?.Invoke();
    }

    private static JobRow? RowOf(object source) => (source as FrameworkElement)?.DataContext as JobRow
        ?? (source as FrameworkContentElement)?.DataContext as JobRow;

    private void OnButton(object sender, RoutedEventArgs e)
    {
        if (e.OriginalSource is Button { Tag: "mark-new" } button && RowOf(button) is { } row)
        {
            MarkNew?.Invoke(row.Match);
            e.Handled = true;
        }
    }

    // A click on a row's words opens the posting (the tick box and the button take their own clicks).
    protected override void OnPreviewMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        pressed = IsOnWords(e.OriginalSource) ? RowOf(e.OriginalSource) : null;
        base.OnPreviewMouseLeftButtonDown(e);
    }

    protected override void OnPreviewMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        var row = IsOnWords(e.OriginalSource) ? RowOf(e.OriginalSource) : null;
        if (row is not null && ReferenceEquals(row, pressed) && !string.IsNullOrEmpty(row.Match.Url))
        {
            Ui.Open(row.Match.Url);
        }
        pressed = null;
        base.OnPreviewMouseLeftButtonUp(e);
    }

    /// <summary>Whether a click is on a row's words, not its tick box or button.</summary>
    private static bool IsOnWords(object source)
    {
        for (var element = source as DependencyObject; element is not null; element = System.Windows.Media.VisualTreeHelper.GetParent(element))
        {
            if (element is CheckBox or Button)
            {
                return false;
            }
            if (element is StackPanel { Name: "Words" })
            {
                return true;
            }
            if (element is JobList)
            {
                return false;
            }
        }
        return false;
    }

    protected override void OnContextMenuOpening(ContextMenuEventArgs e)
    {
        var row = RowOf(e.OriginalSource);
        if (row is null)
        {
            e.Handled = true;
            return;
        }
        var menu = ContextMenu!;
        menu.Items.Clear();
        if (!string.IsNullOrEmpty(row.Match.Url))
        {
            Item(menu, "Open Posting", () => Ui.Open(row.Match.Url));
            Item(menu, "Copy Link", () => Clipboard.SetText(row.Match.Url));
        }
        List<(string Title, Action Run)> more = row.Sending ? [] : Menu?.Invoke(row.Match).ToList() ?? [];
        if (more.Count > 0 && menu.Items.Count > 0)
        {
            menu.Items.Add(new Separator());
        }
        foreach (var (title, run) in more)
        {
            Item(menu, title, run);
        }
        if (menu.Items.Count == 0)
        {
            e.Handled = true;
            return;
        }
        base.OnContextMenuOpening(e);
    }

    private static void Item(ContextMenu menu, string title, Action run)
    {
        var item = new MenuItem { Header = title };
        item.Click += (_, _) => run();
        menu.Items.Add(item);
    }
}
