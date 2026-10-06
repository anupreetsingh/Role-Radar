// Live Tracking: the Mac app's LiveWindow. A sidebar with what's happening now (the round, the alerts,
// the last 24 hours' activity), and the jobs as tabs: New jobs (the stack, newest on top), Seen, and the
// alerts sent. Ticked jobs, or all of them, can be marked as seen (never sent; undoable until the next
// digest records it, within minutes) or sent now, alerts on or off. A search narrows the new jobs shown,
// and what "all of them" means, to those whose title, company or place has its words. While it's shown,
// it's read every 5 seconds.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using RoleRadar.Core;

namespace RoleRadar.Views;

public sealed class LiveView : Grid
{
    private readonly AppModel model;
    private readonly DispatcherTimer reader = new() { Interval = TimeSpan.FromSeconds(5) };

    // the sidebar
    private readonly StatusLine round = new(), failing = new(), waiting = new();
    private readonly ProgressBar progress = new() { Maximum = 1, Height = 4 };
    private readonly TextBlock alertsHelp = Ui.Secondary("", 11);
    private readonly ActivityView activity = new();
    private readonly Border activityCard;
    private readonly Trouble trouble;

    // the lists
    private readonly TabControl tabs = new();
    private readonly TabItem newTab = new(), seenTab = new(), sentTab = new();
    private readonly TextBlock loading = Ui.Secondary("Loading matches…", 12);
    private readonly TextBox search = new();
    private readonly CheckBox newAll = SelectAll(), seenAll = SelectAll();
    private readonly TextBlock newCount = Ui.Secondary("", 11), seenCount = Ui.Secondary("", 11);
    private readonly Button markSeen, sendNow, markNew;
    private readonly TextBlock alertsOffNote = Ui.Secondary("", 12);
    private readonly JobList newJobs = new(seen: false), seenJobs = new(seen: true);
    private readonly TextBlock newEmpty = Ui.Secondary("", 12);
    private readonly TextBlock seenEmpty = Ui.Secondary("Jobs you mark as seen go here for a week, and are never sent. Mark one as new to put it back.", 12);
    private readonly StackPanel sent = new();
    private readonly ScrollViewer sentScroll;
    private readonly TextBlock sentEmpty = Ui.Secondary("No alerts sent lately.", 12);
    private List<SentAlert> sentShown = [];

    public LiveView(AppModel model)
    {
        this.model = model;
        reader.Tick += async (_, _) => await model.RefreshLiveAsync();
        IsVisibleChanged += async (_, _) =>
        {
            if (IsVisible)
            {
                reader.Start();
                await model.RefreshLiveAsync();
            }
            else
            {
                reader.Stop();
            }
        };

        trouble = new Trouble(async () =>
        {
            await model.RetryAsync();
            await model.RefreshLiveAsync();
        });
        activityCard = Ui.Card(activity);
        markSeen = Ui.Button("Mark All as Seen", MarkSeen, 11, tip: "Take them off the stack. Jobs marked as seen are never sent.");
        sendNow = Ui.Button("Send All as Alert", Send, 11);
        markNew = Ui.Button("Mark as New", MarkNew, 11, tip: "Put them back in New jobs");
        sentScroll = new ScrollViewer { Content = sent, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };

        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(330), MinWidth = 280, MaxWidth = 420 });
        ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star), MinWidth = 440 });
        var sidebar = Sidebar();
        var splitter = new GridSplitter { Width = 5, HorizontalAlignment = HorizontalAlignment.Stretch, Background = System.Windows.Media.Brushes.Transparent };
        var lists = Lists();
        SetColumn(splitter, 1);
        SetColumn(lists, 2);
        Children.Add(sidebar);
        Children.Add(splitter);
        Children.Add(lists);

        model.StateChanged += ShowState;
        model.LiveChanged += ShowLive;
        model.SetupChanged += ShowLive;
        ShowLive();
    }

    // -- the sidebar: what's happening now --------------------------------------------------------

    private ScrollViewer Sidebar()
    {
        var now = Ui.Column(8, Ui.Text("Now", 12, FontWeights.SemiBold));
        if (!model.Place.Checks)
        {
            Ui.Add(now, Ui.Secondary("This dev build doesn't check job sites.", 11));
        }
        Ui.Add(now, round);
        Ui.Add(now, progress);
        Ui.Add(now, failing);
        var alerts = Ui.Column(8, Ui.Text("Alerts", 12, FontWeights.SemiBold), waiting, alertsHelp);
        var column = Ui.Column(14, Ui.Card(now), Ui.Card(alerts), activityCard, trouble);
        column.Margin = new Thickness(16);
        return new ScrollViewer { Content = column, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
    }

    private void ShowState()
    {
        var (state, live) = (model.State, model.Live);
        var info = live?.Round ?? state?.Round;
        round.Set(Describe.RoundSummary(info), Glyphs.Sync);
        var fraction = Describe.RoundFraction(info);
        progress.Visible(fraction is not null);
        progress.Value = fraction ?? 0;
        var fail = Describe.FailingLine(state);
        failing.Visible(fail is not null);
        if (fail is { } line)
        {
            failing.Set(line.Text, line.Ok ? Glyphs.Completed : Glyphs.Warning, line.Ok ? Theme.Success : Theme.Caution);
        }
        waiting.Set(Describe.WaitingLine(state, live), live?.SendRequested == true ? Glyphs.Send : Glyphs.List);
        alertsHelp.Text = AlertsHelp();
        activityCard.Visible(state?.Activity is not null);
        if (state?.Activity is { } day)
        {
            activity.Show(day);
        }
        trouble.ShowMessage(model.LiveError ?? model.Error);
    }

    private string AlertsHelp()
    {
        if (model.Live is not { } live)
        {
            return "";
        }
        if (live.SendRequested)
        {
            return "Sending: this PC sends them within a minute.";
        }
        if (!model.CanAlert)
        {
            return "No alerts set up: new jobs collect here. To send them, set up email or Discord in Edit Setup.";
        }
        return live.AlertsOff ? "Alerts are off: new jobs collect here until you send them or mark them as seen."
            : "New jobs go out every 10 minutes. Send some sooner, or mark the ones you don't want as seen.";
    }

    // -- the lists ------------------------------------------------------------------------------------

    private static CheckBox SelectAll() => new()
    {
        MinWidth = 0, Padding = new Thickness(0), HorizontalAlignment = HorizontalAlignment.Center, ToolTip = "Select all",
    };

    /// <summary>A list's heading: the box above its rows' boxes (the same 40-wide column), its name and count,
    /// and buttons at the end.</summary>
    private static Grid Bar(CheckBox all, string name, TextBlock count, params Button[] buttons)
    {
        var box = new Grid { Width = 40 };
        box.Children.Add(all);
        var title = Ui.Row(8, Ui.Text(name, 12, FontWeights.SemiBold), count);
        title.VerticalAlignment = VerticalAlignment.Center;
        var bar = new Grid();
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        SetColumn(title, 1);
        bar.Children.Add(box);
        bar.Children.Add(title);
        foreach (var button in buttons)
        {
            bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            button.Margin = new Thickness(8, 0, 0, 0);
            SetColumn(button, bar.ColumnDefinitions.Count - 1);
            bar.Children.Add(button);
        }
        return bar;
    }

    /// <summary>A tab's page: what's fixed at the top, then the list (or what to say when it's empty).</summary>
    private static Grid Page(UIElement list, UIElement empty, params UIElement[] top)
    {
        var page = new Grid { Margin = new Thickness(12) };
        foreach (var part in top)
        {
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            if (part is FrameworkElement element)
            {
                element.Margin = new Thickness(0, 0, 0, 8);
            }
            SetRow(part, page.RowDefinitions.Count - 1);
            page.Children.Add(part);
        }
        page.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        foreach (var part in new[] { list, empty })
        {
            SetRow(part, page.RowDefinitions.Count - 1);
            page.Children.Add(part);
        }
        return page;
    }

    private Grid Lists()
    {
        search.Size(13).Hint("Search new jobs by title, company or place");
        search.TextChanged += (_, _) =>
        {
            // Ticks are for the jobs on screen: a new search starts with none, so a hidden job is never acted on.
            newJobs.PickAll(false);
            ShowNew();
        };
        search.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape)
            {
                search.Clear();
            }
        };
        newAll.Click += (_, _) => newJobs.PickAll(newJobs.Picked.Count < newJobs.Pickable.Count);
        seenAll.Click += (_, _) => seenJobs.PickAll(seenJobs.Picked.Count < seenJobs.Rows.Count);
        newJobs.PicksChanged += ShowNewBar;
        seenJobs.PicksChanged += ShowSeenBar;
        newJobs.Menu = match => model.CanAlert
            ? [("Mark as Seen", () => _ = model.MarkSeenAsync([match])), ("Send as Alert", () => _ = model.SendAsync([match]))]
            : [("Mark as Seen", () => _ = model.MarkSeenAsync([match]))];
        seenJobs.Menu = match => [("Mark as New", () => _ = model.SkipAsync(match, false))];
        seenJobs.MarkNew += match => _ = model.SkipAsync(match, false);

        newTab.Content = Page(newJobs, newEmpty, search, Bar(newAll, "New jobs", newCount, markSeen, sendNow), alertsOffNote);
        seenTab.Content = Page(seenJobs, seenEmpty, Bar(seenAll, "Seen", seenCount, markNew));
        sentTab.Content = Page(sentScroll, sentEmpty);
        foreach (var tab in new[] { newTab, seenTab, sentTab })
        {
            tab.Size(12);
            tabs.Items.Add(tab);
        }
        var holder = new Grid();
        loading.HorizontalAlignment = HorizontalAlignment.Center;
        loading.VerticalAlignment = VerticalAlignment.Center;
        holder.Children.Add(loading);
        holder.Children.Add(tabs);
        return holder;
    }

    private void ShowLive()
    {
        var live = model.Live;
        tabs.Visible(live is not null);
        loading.Visible(live is null);
        if (live is null)
        {
            loading.Text = model.LiveError is { } problem ? $"Couldn't load the matches\n\n{problem}" : "Loading matches…";
            ShowState();
            return;
        }
        alertsOffNote.Visible(live.AlertsOff);
        alertsOffNote.Text = model.CanAlert
            ? "Alerts are off: new jobs collect here, newest on top. Mark the ones you've seen, or send some as alerts."
            : "New jobs collect here, newest on top. Mark the ones you've seen.";
        ShowNew();
        seenJobs.Show(model.Skipped);
        seenJobs.Visible(model.Skipped.Count > 0);
        seenEmpty.Visible(model.Skipped.Count == 0);
        ShowSeenBar();
        ShowSent(live.Sent);
        newTab.Header = $"New jobs ({Describe.Number(model.Waiting.Count)})";
        seenTab.Header = $"Seen ({Describe.Number(model.Skipped.Count)})";
        sentTab.Header = $"Sent alerts ({Describe.Number(live.Sent.Count)})";
        ShowState();
    }

    // -- new jobs ---------------------------------------------------------------------------------------

    private bool Searching => !string.IsNullOrWhiteSpace(search.Text);

    private void ShowNew()
    {
        var shown = JobSearch.Matching(model.Waiting, search.Text);
        newJobs.Show(shown);
        newEmpty.Text = model.Waiting.Count == 0 ? "No new jobs. They appear here as soon as their company is checked."
            : $"No new jobs match “{search.Text.Trim()}”.";
        newJobs.Visible(shown.Count > 0);
        newEmpty.Visible(shown.Count == 0);
        ShowNewBar();
    }

    /// <summary>What the bar's buttons act on: the ticked jobs; with none, all the search shows (null: all).</summary>
    private List<Match>? Targets()
    {
        var chosen = newJobs.Picked;
        return chosen.Count > 0 ? chosen : Searching ? newJobs.Pickable : null;
    }

    private void ShowNewBar()
    {
        var open = newJobs.Pickable;
        var chosen = newJobs.Picked;
        var some = Searching ? Describe.Number(open.Count) : "All";
        newAll.IsChecked = chosen.Count == 0 ? false : chosen.Count == open.Count ? true : null;
        newAll.Visibility = open.Count > 0 ? Visibility.Visible : Visibility.Hidden;
        newAll.ToolTip = chosen.Count > 0 ? "Deselect all" : "Select all";
        var total = Describe.Number(model.Waiting.Count);
        newCount.Text = chosen.Count > 0 ? $"{chosen.Count} selected"
            : Searching ? $"{Describe.Number(newJobs.Rows.Count)} of {total}" : total;
        var busy = model.LiveBusy.Contains("seen") || model.LiveBusy.Contains("send");
        markSeen.Content = chosen.Count > 0 ? "Mark as Seen" : $"Mark {some} as Seen";
        markSeen.IsEnabled = open.Count > 0 && !busy;
        sendNow.Content = chosen.Count > 0 ? "Send as Alert" : $"Send {some} as Alert";
        sendNow.IsEnabled = open.Count > 0 && !busy && model.CanAlert;
        sendNow.ToolTip = model.CanAlert ? "Send them by email or Discord now; once sent, they leave the stack."
            : "Set up email or Discord in Edit Setup to send jobs.";
        ToolTipService.SetShowOnDisabled(sendNow, true);
    }

    private void MarkSeen()
    {
        var targets = Targets();
        newJobs.PickAll(false);
        _ = model.MarkSeenAsync(targets);
    }

    private void Send()
    {
        var targets = Targets();
        newJobs.PickAll(false);
        _ = model.SendAsync(targets);
    }

    // -- seen jobs ----------------------------------------------------------------------------------------

    private void ShowSeenBar()
    {
        var chosen = seenJobs.Picked;
        seenAll.IsChecked = chosen.Count == 0 ? false : chosen.Count == seenJobs.Rows.Count ? true : null;
        seenAll.Visibility = seenJobs.Rows.Count > 0 ? Visibility.Visible : Visibility.Hidden;
        seenCount.Text = chosen.Count > 0 ? $"{chosen.Count} selected" : Describe.Number(model.Skipped.Count);
        markNew.Visible(chosen.Count > 0);
        markNew.IsEnabled = !model.LiveBusy.Contains("new");
    }

    private void MarkNew()
    {
        var chosen = seenJobs.Picked;
        seenJobs.PickAll(false);
        _ = model.MarkNewAsync(chosen);
    }

    // -- sent alerts -------------------------------------------------------------------------------------

    private void ShowSent(List<SentAlert> alerts)
    {
        sentScroll.Visible(alerts.Count > 0);
        sentEmpty.Visible(alerts.Count == 0);
        var same = alerts.Count == sentShown.Count && alerts.Zip(sentShown).All(p => p.First.SentAt == p.Second.SentAt);
        var opened = sent.Children.OfType<Expander>().Where(e => e.IsExpanded).Select(e => e.Tag).ToHashSet();
        if (!same)
        {
            sent.Children.Clear();
            foreach (var alert in alerts)
            {
                var jobs = Ui.Column(2);
                jobs.Margin = new Thickness(28, 4, 0, 8);
                foreach (var job in alert.Jobs)
                {
                    Ui.Add(jobs, SentJob(job), 2);
                }
                sent.Children.Add(new Expander { Tag = alert.SentAt, Content = jobs, IsExpanded = opened.Contains(alert.SentAt), Margin = new Thickness(0, 0, 0, 4) });
            }
            sentShown = alerts;
        }
        // Each alert's heading, its "3 hr. ago" moving on.
        foreach (var (expander, alert) in sent.Children.OfType<Expander>().Zip(sentShown))
        {
            var when = Describe.Date(alert.SentAt);
            var by = alert.By switch { "lambda" => "Lambda", null => null, _ => "this PC" };
            var details = string.Join(" · ", new[] { Describe.Plural(alert.Jobs.Count, "job"), by is null ? null : $"sent by {by}",
                                                     when is null ? null : Describe.Ago(when) }.Where(p => p is not null));
            expander.Header = Ui.Row(10, Ui.Text(when is { } at ? Describe.Full(at) : alert.SentAt, 13, FontWeights.SemiBold), Ui.Secondary(details, 11));
            expander.ToolTip = "Show this alert's jobs";
        }
    }

    /// <summary>One of an alert's jobs: its title, company and place; clicking it opens the posting.</summary>
    private static UIElement SentJob(SentJob job)
    {
        var where = string.Join(" · ", new[] { job.Company, job.Location }.Where(s => !string.IsNullOrEmpty(s)));
        var words = Ui.Column(1, Ui.Text(job.Title ?? "", 13), Ui.Secondary(where, 11));
        if (!string.IsNullOrEmpty(job.Url))
        {
            words.Cursor = Cursors.Hand;
            words.Background = System.Windows.Media.Brushes.Transparent;
            words.ToolTip = "Open the posting in your browser";
            words.MouseLeftButtonUp += (_, _) => Ui.Open(job.Url);
        }
        return words;
    }
}
