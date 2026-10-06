// Setup's pages, the Mac app's SetupView: the person's profession (which brings its company list and job
// titles), their countries, the companies tracked, the roles they want and don't, their qualifications,
// and alerts (optional). Leaving a page saves it; closing the window does too.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Threading;
using RoleRadar.Core;

namespace RoleRadar.Views;

public sealed class SetupView : Grid
{
    private const int Last = 5;
    private static readonly Dictionary<string, string> ProfessionGlyphs = new()
    {
        ["tech"] = Glyphs.Laptop, ["accounting"] = Glyphs.Calculator, ["healthcare"] = Glyphs.Health,
    };
    private const string SavedWhere = "Saved in Windows Credential Manager";

    private readonly AppModel model;
    private int page;
    private string? busy; // the action working right now
    private string? choosing; // the profession being saved, shown as picked meanwhile
    private bool filled;
    private string saved = ""; // the profile pages' answers as last saved, to tell when they've changed
    private readonly HashSet<string> countries = [];
    private CompanySearch? found;
    private int shownResults = 50;
    private readonly HashSet<string> turning = []; // companies being turned on or off
    private readonly DispatcherTimer searchSoon = new() { Interval = TimeSpan.FromMilliseconds(300) };

    private readonly StackPanel markers = new() { Orientation = Orientation.Horizontal };
    private readonly Trouble trouble;
    private readonly TextBlock loading = Ui.Secondary("Loading…", 13);
    private readonly FrameworkElement[] pages;
    private readonly ScrollViewer scroll = new() { VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
    private readonly Button back, next, start;
    private readonly TextBlock footerNote = Ui.Secondary("", 12);
    private readonly Spinner footerSpinner = new();

    // page 1
    private readonly UniformGrid cards = new() { Rows = 1 };
    private readonly Dictionary<string, (Button Card, TextBlock Glyph, TextBlock Mark, Spinner Spinner)> cardParts = new();
    private readonly TextBlock professionNote = Ui.Secondary("", 12);
    // page 2
    private readonly StackPanel countryBoxes = Ui.Column(10);
    private readonly Dictionary<string, CheckBox> countryBox = new();
    private readonly TextBlock untaggedNote = Ui.Secondary("", 12);
    private readonly TextBlock trackedNote = Ui.Text("", 13, FontWeights.Medium);
    private readonly TextBox cities = new() { MaxWidth = 460, HorizontalAlignment = HorizontalAlignment.Left };
    // page 3
    private readonly StackPanel companiesHead = Ui.Column(6), byCountry = Ui.Column(6), results = Ui.Column(6);
    private readonly StackPanel findSection;
    private readonly TextBox query = new() { MaxWidth = 320, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly TextBlock foundNote = Ui.Secondary("", 11);
    private readonly TextBlock findProblem = Ui.Text("", 11, brush: Theme.Critical);
    private readonly Button more;
    private readonly TextBox newCompany = new() { MaxWidth = 320, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly TextBox newCareers = new() { MaxWidth = 420, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly ActionRow addRow;
    // page 4
    private readonly TitleBoxes targets, skips;
    private readonly StackPanel targetHead = Ui.Row(6), skipHead = Ui.Row(6);
    private string? examplesFor;
    // page 5
    private readonly CheckBox checkYears = new() { Content = "Skip jobs asking for", VerticalAlignment = VerticalAlignment.Center };
    private readonly ComboBox skipFrom = new() { MinWidth = 220 };
    private readonly TextBlock yearsNote = Ui.Secondary("", 12);
    private readonly List<RadioButton> degrees = [];
    // page 6
    private readonly TextBox address = new() { MaxWidth = 320, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly PasswordBox password = new() { MaxWidth = 320, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly ActionRow emailRow, alsoRow, testEmail, discordRow, testDiscord;
    private readonly StackPanel emailSaved;
    private readonly TextBox also = new() { AcceptsReturn = true, Height = 64, MaxWidth = 420, HorizontalAlignment = HorizontalAlignment.Left, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock recipients = Ui.Secondary("", 11);
    private readonly PasswordBox webhook = new() { MaxWidth = 420, HorizontalAlignment = HorizontalAlignment.Left };

    /// <summary>Start Checking (or Done): the window turns into Live Tracking.</summary>
    public event Action? Finished;

    public SetupView(AppModel model)
    {
        this.model = model;
        trouble = new Trouble(async () =>
        {
            await model.LoadSetupAsync();
            Load();
        });
        searchSoon.Tick += (_, _) =>
        {
            searchSoon.Stop();
            _ = Search();
        };
        more = Ui.Button("Show More", () =>
        {
            shownResults += 50;
            _ = Search();
        }, 11);
        addRow = Action("add", "Add", AddCompany);
        emailRow = Action("email", "Save", SaveEmail);
        alsoRow = Action("also", "Save List", SaveAlso);
        testEmail = Action("test-email", "Send Test Email", () => SendTest("email"));
        discordRow = Action("discord", "Save", SaveDiscord);
        testDiscord = Action("test-discord", "Send Test Message", () => SendTest("discord"));
        targets = new TitleBoxes("A title of your own", "Add a Title…", allButtons: true);
        skips = new TitleBoxes("A word of your own", "Add a Word…", allButtons: false);
        targets.Changed += ShowFooter;
        findSection = new StackPanel();
        emailSaved = new StackPanel();

        back = Ui.Button("Back", () => _ = Go(page - 1), 13);
        next = Ui.Button("Next", () => _ = Go(page + 1, always: true), 13, accent: true);
        start = Ui.Button("Start Checking", () => _ = Start(), 13, accent: true);
        pages = [ProfessionPage(), CountriesPage(), CompaniesPage(), RolesPage(), QualificationsPage(), AlertsPage()];

        var header = Ui.Column(12, Ui.Text("Set Up Role Radar", 20, FontWeights.SemiBold), markers);
        header.Margin = new Thickness(24, 24, 24, 14);
        var body = Ui.Column(0, trouble, loading);
        foreach (var part in pages)
        {
            body.Children.Add(part);
        }
        body.Margin = new Thickness(24);
        loading.HorizontalAlignment = HorizontalAlignment.Center;
        loading.Margin = new Thickness(0, 60, 0, 0);
        scroll.Content = body;

        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        UIElement[] rows = [header, Ui.Divider(), scroll, Ui.Divider(), Footer()];
        for (var i = 0; i < rows.Length; i++)
        {
            SetRow(rows[i], i);
            Children.Add(rows[i]);
        }

        model.SetupChanged += Show;
        model.StateChanged += ShowFooter;
        IsVisibleChanged += async (_, _) =>
        {
            if (IsVisible)
            {
                await model.LoadSetupAsync();
                Load();
            }
        };
        PreviewKeyDown += (_, e) =>
        {
            // Enter moves on, as the Mac's default button does (not from a field that takes its own Enter).
            if (e.Key == Key.Enter && page < Last && next.IsEnabled && Keyboard.FocusedElement is not TextBox and not PasswordBox)
            {
                e.Handled = true;
                _ = Go(page + 1, always: true);
            }
        };
        Show();
    }

    private SetupState? Setup => model.Setup;

    private Profession? Profession => Setup?.Professions.FirstOrDefault(p => p.Id == Setup.Profession);

    private ActionRow Action(string key, string title, Func<Task<(string, bool)>> action) =>
        new(title, action, on =>
        {
            busy = on ? key : null;
            Show();
        });

    // -- loading and showing -------------------------------------------------------------------------

    /// <summary>Fill the pages from what's saved, once, and open where there's something left to do.</summary>
    public void Load()
    {
        if (filled || Setup is not { } setup)
        {
            Show();
            return;
        }
        FillTitles();
        countries.Clear();
        countries.UnionWith(setup.Countries);
        cities.Text = string.Join(", ", setup.Cities);
        // On until the countries page is saved: then it's what they chose.
        checkYears.IsChecked = setup.MaxExperienceYears is not null || setup.Countries.Count == 0;
        skipFrom.SelectedIndex = Math.Clamp((setup.MaxExperienceYears ?? 2) + 1, 1, 20) - 1;
        address.Text = setup.Email ?? "";
        also.Text = string.Join("\n", setup.Also);
        SetEducation(setup.Education ?? "bachelors");
        ShowYears();
        saved = Answers().Fingerprint;
        page = setup.Profession is null ? 0 : setup.Countries.Count == 0 ? 1 : setup.Roles.Count == 0 ? 3
            : setup.Education is null ? 4 : 0;
        filled = true;
        Show();
    }

    private void FillTitles()
    {
        targets.Fill(Profession?.Groups ?? [], Setup?.Roles ?? []);
        skips.Fill(Profession?.SkipGroups ?? [], Setup?.Exclude ?? []);
    }

    private void Show()
    {
        trouble.ShowMessage(model.SetupError);
        loading.Visible(Setup is null && model.SetupError is null);
        for (var i = 0; i < pages.Length; i++)
        {
            pages[i].Visible(Setup is not null && i == page); // only the page shown takes room
        }
        if (Setup is not null)
        {
            ShowProfession();
            ShowCountries();
            ShowCompanies();
            ShowRoles();
            ShowAlerts();
        }
        ShowMarkers();
        ShowFooter();
    }

    private void ShowMarkers()
    {
        markers.Children.Clear();
        var setup = Setup;
        var savedCountries = setup?.Countries.Count > 0;
        bool[] done = [setup?.Profession is not null, savedCountries, savedCountries, savedCountries && setup?.Roles.Count > 0,
                       setup?.Education is not null, AlertsReady];
        for (var i = 0; i < SetupRules.Pages.Length; i++)
        {
            var index = i;
            var label = Ui.Row(5,
                done[i] ? Ui.Glyph(Glyphs.Completed, 12, Theme.Success) : Ui.Text($"{i + 1}.", 12, brush: i == page ? Theme.Text : Theme.Secondary),
                Ui.Text(SetupRules.Pages[i], 12, i == page ? FontWeights.SemiBold : FontWeights.Normal, i == page ? Theme.Text : Theme.Secondary));
            var marker = new Button
            {
                Content = label, Background = System.Windows.Media.Brushes.Transparent, BorderThickness = new Thickness(0),
                Padding = new Thickness(4, 2, 4, 2), Focusable = false, Cursor = Cursors.Hand,
                IsEnabled = busy is null && (i == 0 || setup?.Profession is not null),
            };
            marker.Click += (_, _) => _ = Go(index);
            Ui.Add(markers, marker, 2);
            if (i < SetupRules.Pages.Length - 1)
            {
                Ui.Add(markers, Ui.Glyph(Glyphs.ChevronRight, 9, Theme.Tertiary), 2);
            }
        }
    }

    // -- page 1: profession -------------------------------------------------------------------------------

    private StackPanel ProfessionPage() => Ui.Column(16,
        Ui.Text("What's your profession?", 15, FontWeights.SemiBold),
        Ui.Secondary("Role Radar watches the job boards of companies that hire in your profession, and emails you new jobs that "
                     + "match within minutes of them being posted.", 12),
        cards, professionNote);

    private void ShowProfession()
    {
        var options = Setup?.Professions ?? [];
        if (!cardParts.Keys.SequenceEqual(options.Select(o => o.Id)))
        {
            cards.Children.Clear();
            cardParts.Clear();
            cards.Columns = Math.Max(1, options.Count);
            foreach (var option in options)
            {
                var glyph = Ui.Glyph(ProfessionGlyphs.GetValueOrDefault(option.Id, Glyphs.Laptop), 26);
                var mark = Ui.Glyph(Glyphs.Completed, 18, Theme.AccentText);
                var spinner = new Spinner();
                var top = Ui.Spread(glyph, mark, spinner);
                var card = new Button
                {
                    Content = Ui.Column(8, top, Ui.Text(option.Name, 15, FontWeights.SemiBold), Ui.Secondary(option.About, 12)),
                    Margin = new Thickness(0, 0, 12, 0),
                };
                card.SetResourceReference(StyleProperty, "ProfessionCard");
                System.Windows.Automation.AutomationProperties.SetName(card, option.Name);
                card.Click += (_, _) => _ = Pick(option.Id);
                cards.Children.Add(card);
                cardParts[option.Id] = (card, glyph, mark, spinner);
            }
        }
        var chosen = choosing ?? Setup?.Profession;
        foreach (var (id, (card, glyph, mark, spinner)) in cardParts)
        {
            card.Tag = chosen == id ? "chosen" : null;
            card.IsEnabled = (busy is null && choosing is null) || chosen == id;
            glyph.SetResourceReference(TextBlock.ForegroundProperty, chosen == id ? Theme.AccentText : Theme.Secondary);
            mark.Visible(chosen == id && choosing is null);
            spinner.Visible(choosing == id);
        }
        if (Note("profession") is ({ } problem, false))
        {
            professionNote.Text = $"⚠ {problem}";
            professionNote.Colored(Theme.Critical);
        }
        else if (Profession is { } profession && choosing is null)
        {
            var count = profession.Groups.Sum(g => g.Titles.Count);
            professionNote.Text = $"✓ {profession.Name}: {count} job titles to choose from, and its companies. Next, pick your countries and titles.";
            professionNote.Colored(Theme.Secondary);
        }
        else
        {
            professionNote.Text = "";
        }
    }

    /// <summary>Show the choice straight away, save it, and stay on the page: Next moves on.</summary>
    private async Task Pick(string id)
    {
        if (id == Setup?.Profession || choosing is not null)
        {
            return;
        }
        choosing = id;
        Show();
        var problem = await model.SetupStepAsync(["profession"], new Dictionary<string, string> { ["profession"] = id });
        choosing = null;
        notes["profession"] = problem is null ? null : (problem, false);
        if (problem is null)
        {
            FillTitles();
            saved = Answers().Fingerprint; // a new profession brings its own boxes, saved already
        }
        Show();
    }

    // -- page 2: countries ------------------------------------------------------------------------------------

    private StackPanel CountriesPage()
    {
        cities.Size(13).Hint("Cities, separated by commas");
        return Ui.Column(24,
            Section("Where do you want to work?", "Role Radar tracks the companies that post jobs in the countries you pick.",
                    countryBoxes, untaggedNote, trackedNote),
            Section("Cities (optional)", "Alerts only for jobs in these cities, e.g. Bengaluru, Hyderabad. With none, jobs anywhere "
                    + "in your countries alert you. Jobs listed only as \"Remote\" always do.", cities));
    }

    private int? TrackedCount() =>
        countries.Count == 0 || Setup is null ? null : Setup.CompaniesFor.GetValueOrDefault(SetupRules.CountriesKey(Setup, countries));

    private string CountryNames() =>
        SetupRules.CountryNames(Setup?.CountryOptions.Where(c => countries.Contains(c.Code)).Select(c => c.Name) ?? []);

    private void ShowCountries()
    {
        var setup = Setup!;
        if (!countryBox.Keys.SequenceEqual(setup.CountryOptions.Select(c => c.Code)))
        {
            countryBoxes.Children.Clear();
            countryBox.Clear();
            foreach (var country in setup.CountryOptions)
            {
                var box = new CheckBox().Size(14);
                box.Click += (_, _) =>
                {
                    if (box.IsChecked == true) countries.Add(country.Code); else countries.Remove(country.Code);
                    ShowCountries();
                    ShowFooter();
                };
                Ui.Add(countryBoxes, box, 10);
                countryBox[country.Code] = box;
            }
        }
        // Each country's count is what ticking it alone tracks: those posting there, and those naming no country.
        foreach (var country in setup.CountryOptions)
        {
            var box = countryBox[country.Code];
            var count = setup.CompaniesByCountry.GetValueOrDefault(country.Code);
            box.Content = Ui.Row(12, Ui.Text(country.Name, 14),
                count > 0 ? Ui.Secondary($"{Describe.Number(count + setup.CompaniesUntagged)} companies", 12) : null);
            box.IsChecked = countries.Contains(country.Code);
        }
        untaggedNote.Visible(setup.CompaniesUntagged > 0);
        untaggedNote.Text = $"Each count includes {Describe.Number(setup.CompaniesUntagged)} companies whose job listings don't name a country.";
        var tracked = TrackedCount();
        trackedNote.Visible(tracked is not null);
        if (tracked is { } count2)
        {
            trackedNote.Text = $"{Describe.Number(count2)} companies to track in {CountryNames()}.";
            trackedNote.Colored(count2 == 0 ? Theme.Caution : Theme.Text);
        }
    }

    // -- page 3: the companies tracked -------------------------------------------------------------------------

    private StackPanel CompaniesPage()
    {
        query.Size(13).Hint("Search companies");
        query.TextChanged += (_, _) =>
        {
            shownResults = 50;
            searchSoon.Stop();
            searchSoon.Start(); // once they pause typing
        };
        foreach (var part in new UIElement[] { query, foundNote, results, more, findProblem })
        {
            Ui.Add(findSection, part, 10);
        }
        var find = Section("Find a company", "Untick a company to stop tracking it; tick it again to bring it back.", findSection);
        newCompany.Size(13).Hint("Company name");
        newCareers.Size(13).Hint("Careers page (optional), https://…");
        newCompany.TextChanged += (_, _) => ShowAdd();
        // A company they want: tracked at once if Role Radar can read its job site, and either way
        // suggested for everyone's list, unless it's listed already.
        var add = Section("Add a company you want", "Not on the list? Give its name, and its careers page if you know it. If Role "
                          + "Radar can read its job site, it's tracked right away; if not, we'll work on it.", newCompany, newCareers, addRow);
        findSection.Tag = find;
        return Ui.Column(18, companiesHead, byCountry, find, add);
    }

    private void ShowCompanies()
    {
        var setup = Setup!;
        var count = TrackedCount() ?? setup.Companies;
        var name = Profession?.Name ?? "";
        companiesHead.Children.Clear();
        byCountry.Children.Clear();
        if (count == 0)
        {
            Ui.Add(companiesHead, Section($"No {name} companies yet",
                $"Role Radar doesn't have a list of {name} employers yet. A later version adds them, and checking starts then."));
        }
        else
        {
            Ui.Add(companiesHead, Ui.Text($"{Describe.Number(count)} companies will be tracked", 22, FontWeights.SemiBold));
            Ui.Add(companiesHead, Ui.Secondary("Role Radar checks every one of their job boards from this PC while it's on: most every "
                + "20 minutes, Workday boards every 6 hours. You'll hear about new jobs that match your roles within minutes of them "
                + "being posted.", 13), 6);
            var lines = Ui.Column(6);
            foreach (var country in setup.CountryOptions.Where(c => countries.Contains(c.Code)))
            {
                var posting = setup.CompaniesByCountry.GetValueOrDefault(country.Code);
                Ui.Add(lines, Ui.Row(16, Ui.Text(country.Name, 13), Ui.Secondary($"{Describe.Number(posting)} companies post jobs here", 13)), 6);
            }
            if (setup.CompaniesUntagged > 0)
            {
                Ui.Add(lines, Ui.Secondary($"Also {Describe.Number(setup.CompaniesUntagged)} whose job listings don't name a country, "
                    + "so they're tracked for every country.", 12), 6);
            }
            Ui.Add(byCountry, Section("By country", null, lines));
        }
        ((UIElement)findSection.Tag).Visible(count > 0);
        ShowAdd();
    }

    private void ShowAdd() => addRow.Button.IsEnabled = !string.IsNullOrWhiteSpace(newCompany.Text) && busy is null;

    /// <summary>The companies matching what's typed (nothing typed: the ones turned off).</summary>
    private async Task Search()
    {
        var typed = query.Text;
        var result = await model.SetupCommandAsync(["find"], Json.Write(new Dictionary<string, object>
        {
            ["query"] = typed, ["limit"] = shownResults, ["which"] = typed.Length > 0 ? "all" : "off",
        }));
        if (typed != query.Text)
        {
            return; // they've typed more since
        }
        var problem = "";
        if (result.Ok)
        {
            try
            {
                found = result.Read<CompanySearch>();
            }
            catch (System.Text.Json.JsonException)
            {
                found = null;
                problem = "Unexpected reply from role-radar";
            }
        }
        else
        {
            problem = result.Message;
        }
        findProblem.Text = problem;
        findProblem.Visible(problem.Length > 0);
        ShowResults();
    }

    private void ShowResults()
    {
        results.Children.Clear();
        foundNote.Visible(false);
        more.Visible(false);
        if (found is not { } search)
        {
            return;
        }
        foundNote.Text = query.Text.Length == 0 ? (search.Results.Count > 0 ? $"Turned off ({search.Total})" : "")
            : search.Total == 0 ? "No company on the list matches. Add it below, or ask for it."
            : search.Total > search.Results.Count ? $"{search.Results.Count} of {search.Total}" : $"{search.Total} found";
        foundNote.Visible(foundNote.Text.Length > 0);
        foreach (var company in search.Results)
        {
            var box = new CheckBox
            {
                IsChecked = !company.Off, IsEnabled = company.Readable && !turning.Contains(company.Name), VerticalAlignment = VerticalAlignment.Top,
                MinWidth = 0, Margin = new Thickness(0, 2, 8, 0),
            };
            System.Windows.Automation.AutomationProperties.SetName(box, company.Name);
            box.Click += (_, _) => _ = SetTracked(company, box.IsChecked == true);
            var title = Ui.Row(6, Ui.Text(company.Name, 13), company.Own ? Ui.Secondary("Yours", 10) : null);
            var row = new DockPanel();
            DockPanel.SetDock(box, Dock.Left);
            row.Children.Add(box);
            row.Children.Add(Ui.Column(1, title, Ui.Secondary(SetupRules.About(company), 11)));
            Ui.Add(results, row, 6);
        }
        more.Visible(search.Total > search.Results.Count);
    }

    private async Task SetTracked(CompanyRow company, bool on)
    {
        turning.Add(company.Name);
        var problem = await model.SetupStepAsync(["track"], new Dictionary<string, object> { ["names"] = new[] { company.Name }, ["tracked"] = on });
        turning.Remove(company.Name);
        findProblem.Text = problem ?? "";
        findProblem.Visible(problem is not null);
        await Search();
    }

    private async Task<(string, bool)> AddCompany()
    {
        var gaveCareers = !string.IsNullOrWhiteSpace(newCareers.Text);
        var result = await model.SetupCommandAsync(["add"], Json.Write(new Dictionary<string, string>
        {
            ["name"] = newCompany.Text, ["url"] = newCareers.Text,
        }));
        if (!result.Ok)
        {
            return (result.Message, false);
        }
        AddResult added;
        try
        {
            added = result.Read<AddResult>();
        }
        catch (System.Text.Json.JsonException)
        {
            return ("Unexpected reply from role-radar", false);
        }
        (string, bool) note;
        switch (added.Status)
        {
            case "added":
                var jobs = added.Jobs switch { null => "", 1 => " (1 job listed now)", var n => $" ({n} jobs listed now)" };
                note = ($"Added: Role Radar now tracks {added.Name}{jobs}.", true);
                newCompany.Clear();
                newCareers.Clear();
                break;
            case "listed":
                note = added.Why is { } why ? ($"{added.Name} is on the list, but isn't tracked: {why}.", false)
                    : (added.TurnedOn == true ? $"{added.Name} was turned off; it's tracked again." : $"{added.Name} is tracked already.", true);
                break;
            default:
                var hint = !gaveCareers && added.Url is null ? " If you know its careers page, add it and try again." : "";
                note = ($"Role Radar can't track {added.Name} yet. We've noted it and are working on it.{hint}", false);
                break;
        }
        await model.LoadSetupAsync();
        await Search();
        return note;
    }

    // -- page 4: roles ---------------------------------------------------------------------------------------

    private StackPanel RolesPage() => Ui.Column(28,
        Ui.Column(10, targetHead,
            Ui.Secondary("A job alerts you when its title contains one of the ticked titles. Untick any you don't want.", 12), targets),
        Ui.Column(10, skipHead,
            Ui.Secondary("A ticked word here stops a job's alert, unless the word is part of a target title you ticked. Untick any you "
                         + "want, such as Senior or Lead if you have the experience.", 12), skips));

    private void ShowRoles()
    {
        var profession = Profession;
        if (examplesFor == profession?.Id)
        {
            return;
        }
        examplesFor = profession?.Id;
        targetHead.Children.Clear();
        skipHead.Children.Clear();
        Ui.Add(targetHead, Ui.Text("Target roles", 15, FontWeights.SemiBold), 6);
        Ui.Add(skipHead, Ui.Text("Non-target roles", 15, FontWeights.SemiBold), 6);
        // The ⓘs: what a ticked title does, and that a ticked word never stops a target title it's part of.
        if (profession?.Examples?.Target is { } target)
        {
            const string rule = "You get an alert when a ticked title is in a job's title.";
            Ui.Add(targetHead, new InfoButton(rule, () => InfoButton.Example(rule, [target.Title], [], target.Jobs, [])), 6);
        }
        if (profession?.Examples?.NonTarget is { } word)
        {
            const string rule = "A ticked word here stops the alert, unless it's part of a target title you ticked.";
            Ui.Add(skipHead, new InfoButton(rule, () => InfoButton.Example(rule, word.Targets, word.Words, word.Reach, word.Stopped)), 6);
        }
    }

    // -- page 5: qualifications ----------------------------------------------------------------------------------

    private StackPanel QualificationsPage()
    {
        checkYears.Size(13);
        skipFrom.Size(13);
        for (var years = 1; years <= 20; years++)
        {
            skipFrom.Items.Add($"{years}+ years of experience");
        }
        skipFrom.SelectedIndex = 2;
        checkYears.Click += (_, _) => ShowYears();
        skipFrom.SelectionChanged += (_, _) => ShowYears();
        var education = Ui.Column(8);
        foreach (var (value, name) in SetupRules.Degrees)
        {
            var choice = new RadioButton { Content = name, Tag = value, GroupName = "education" }.Size(13);
            degrees.Add(choice);
            Ui.Add(education, choice, 8);
        }
        SetEducation("bachelors");
        ShowYears();
        return Ui.Column(24,
            Section("Experience", "Role Radar reads each new match's description once, and skips jobs asking for more experience than you have.",
                    Ui.Row(10, checkYears, skipFrom), yearsNote),
            Section("Education", "Your highest degree, or the one you'll have when you start. Jobs that need a higher degree are skipped.",
                    education, Ui.Secondary("A degree can also count in place of experience: if you skip 3+ years and have a Master's, a job "
                                            + "asking for \"3 years, or 1 year with a Master's\" is still shown.", 12)));
    }

    private int SkipFrom => skipFrom.SelectedIndex + 1;

    private void ShowYears()
    {
        skipFrom.IsEnabled = checkYears.IsChecked == true;
        yearsNote.Text = checkYears.IsChecked == true
            ? $"You'll still hear about jobs asking for {SetupRules.StillAlert(SkipFrom)}, and jobs that don't say."
            : "Jobs alert you whatever experience they ask for.";
    }

    private void SetEducation(string value)
    {
        foreach (var choice in degrees)
        {
            choice.IsChecked = (string)choice.Tag == value;
        }
    }

    private string Education => degrees.FirstOrDefault(d => d.IsChecked == true)?.Tag as string ?? "bachelors";

    // -- page 6: alerts ---------------------------------------------------------------------------------------------

    private StackPanel AlertsPage()
    {
        address.Size(13).Hint("you@gmail.com");
        password.Size(13).Hint("App password (16 letters)");
        address.TextChanged += (_, _) => ShowAlerts();
        password.PasswordChanged += (_, _) => ShowAlerts();
        also.Size(12).Hint("friend@example.com");
        also.FontFamily = new System.Windows.Media.FontFamily("Cascadia Mono, Consolas");
        webhook.Size(13);
        webhook.PasswordChanged += (_, _) => ShowAlerts();
        Ui.Add(emailSaved, Ui.Text("Also send alerts to (a friend, your school email), one per line. Each person sees only your address.", 12), 0);
        Ui.Add(emailSaved, also, 8);
        Ui.Add(emailSaved, Ui.Row(12, alsoRow, testEmail), 8);
        Ui.Add(emailSaved, recipients, 8);
        return Ui.Column(28,
            Ui.Text("Alerts are optional. New jobs always collect in Live Tracking, newest on top, so you can just open the app to see "
                    + "them. Set up email, Discord or both to also get them sent every 10 minutes.", 13),
            Section("Email (Gmail)", "Alerts come from your own Gmail, sent to yourself and anyone you add. Gmail needs an app password "
                    + "for this: a 16-letter password just for Role Radar. Creating one needs 2-Step Verification on your Google account.",
                    Ui.Link("Create an app password ↗", () => Ui.Open(SetupRules.AppPasswords)), address, password, emailRow, emailSaved),
            Section("Discord", "Alerts go to a channel in your Discord server. In Discord, open the channel's settings, then "
                    + "Integrations → Webhooks → New Webhook → Copy Webhook URL, and paste it here.",
                    webhook, Ui.Row(12, discordRow, testDiscord)));
    }

    /// <summary>At least one way to send alerts is set up.</summary>
    private bool AlertsReady => Setup is { } setup && (setup.EmailReady || setup.DiscordReady);

    private void ShowAlerts()
    {
        var setup = Setup;
        var idle = busy is null;
        emailRow.Button.IsEnabled = idle && address.Text.Length > 0 && password.Password.Length > 0;
        var ready = setup?.Email is not null && setup.EmailReady;
        emailSaved.Visible(ready);
        if (ready)
        {
            var others = setup!.Also.Count;
            recipients.Text = $"Alerts go to {setup.Email}" + (others == 0 ? "." : $" and {others} other{(others == 1 ? "" : "s")}.");
        }
        var discordReady = setup?.DiscordReady == true;
        webhook.Hint(discordReady ? "Saved. Paste a new one to change it." : "https://discord.com/api/webhooks/...");
        discordRow.Button.IsEnabled = idle && webhook.Password.Trim().Length > 0;
        testDiscord.Visible(discordReady);
        foreach (var action in new[] { alsoRow, testEmail, testDiscord })
        {
            action.Button.IsEnabled = idle;
        }
    }

    private async Task<(string, bool)> SaveEmail()
    {
        var problem = await model.SetupStepAsync(["email"], new Dictionary<string, string> { ["address"] = address.Text, ["password"] = password.Password });
        if (problem is not null)
        {
            return (problem, false);
        }
        password.Clear();
        await model.SetAsync("email", true); // set up, so alerts go there
        return (SavedWhere, true);
    }

    private async Task<(string, bool)> SaveDiscord()
    {
        var problem = await model.SetupStepAsync(["discord"], new Dictionary<string, string> { ["webhook"] = webhook.Password });
        if (problem is not null)
        {
            return (problem, false);
        }
        webhook.Clear();
        await model.SetAsync("discord", true);
        return (SavedWhere, true);
    }

    private async Task<(string, bool)> SaveAlso()
    {
        var problem = await model.SetupStepAsync(["recipients"], new Dictionary<string, object> { ["also"] = SetupRules.Lines(also.Text) });
        return problem is null ? ("Saved", true) : (problem, false);
    }

    private async Task<(string, bool)> SendTest(string channel)
    {
        var result = await model.Cli.RunAsync(["notifications", "test", "--channel", channel]);
        return result.Ok ? (channel == "email" ? "Sent. Check your inbox." : "Sent. Check the channel.", true) : (result.Message, false);
    }

    // -- the footer, and moving between pages ---------------------------------------------------------------

    private readonly Dictionary<string, (string Text, bool Ok)?> notes = new(); // each action's last result

    private (string Text, bool Ok)? Note(string key) => notes.GetValueOrDefault(key);

    private Grid Footer()
    {
        footerNote.VerticalAlignment = VerticalAlignment.Center;
        footerNote.Margin = new Thickness(10, 0, 10, 0);
        footerSpinner.Visible(false);
        var footer = new Grid { Margin = new Thickness(24, 14, 24, 14) };
        foreach (var width in new[] { GridLength.Auto, new GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto, GridLength.Auto })
        {
            footer.ColumnDefinitions.Add(new ColumnDefinition { Width = width });
        }
        UIElement[] parts = [back, footerNote, footerSpinner, next, start];
        for (var i = 0; i < parts.Length; i++)
        {
            SetColumn(parts[i], i);
            footer.Children.Add(parts[i]);
        }
        footerSpinner.Margin = new Thickness(0, 0, 10, 0);
        return footer;
    }

    private void ShowFooter()
    {
        var setup = Setup;
        back.Visible(page > 0);
        back.IsEnabled = busy is null;
        if (Note("page") is ({ } problem, false))
        {
            footerNote.Text = $"⚠ {problem}";
            footerNote.Colored(Theme.Critical);
        }
        else
        {
            footerNote.Text = page != Last ? ""
                : setup?.Ready == true ? "All set. Role Radar checks while this PC is on and the app is open, and it opens at login."
                : "Pick your countries and roles to finish.";
            footerNote.Colored(Theme.Secondary);
        }
        footerSpinner.Visible(busy == "page");
        next.Visible(page < Last);
        next.IsEnabled = busy is null && setup?.Profession is not null && !(page == 1 && countries.Count == 0)
                         && !(page == 3 && targets.Ticked.Count == 0);
        start.Visible(page == Last);
        start.Content = model.Checking == "laptop" ? "Done" : "Start Checking";
        start.IsEnabled = setup?.Ready == true;
    }

    /// <summary>Move to another page. Leaving the countries, roles or qualifications page saves it first:
    /// always with Next, and with Back or a page's name whenever something changed, so no change is lost.</summary>
    private async Task Go(int target, bool always = false)
    {
        if (target is < 0 or > Last || busy is not null)
        {
            return;
        }
        notes["page"] = null;
        if (filled && (Answers().Fingerprint != saved || (always && page is 1 or 3 or 4)))
        {
            busy = "page";
            ShowFooter();
            var problem = await model.SetupStepAsync(["profile"], Answers().Json(withEducation: page == 4));
            busy = null;
            if (problem is not null)
            {
                notes["page"] = (problem, false);
                Show();
                return;
            }
            saved = Answers().Fingerprint;
        }
        page = target;
        Show();
        scroll.ScrollToTop(); // each page opens at its top
        if (target == 2)
        {
            await Search();
        }
    }

    private async Task Start()
    {
        await model.StartCheckingAsync();
        Finished?.Invoke();
    }

    /// <summary>Closing the window, or going to Live Tracking, keeps what was changed, as leaving the page does.</summary>
    public void SaveIfChanged()
    {
        if (filled && Answers().Fingerprint != saved)
        {
            var data = Answers().Json(withEducation: page == 4);
            saved = Answers().Fingerprint;
            _ = model.SetupStepAsync(["profile"], data);
        }
    }

    /// <summary>The countries, roles and qualifications pages' answers, as they stand.</summary>
    private SetupRules.Profile Answers() => new(
        targets.Chosen(), skips.Chosen(), SetupRules.Lines(cities.Text), countries.Order(StringComparer.Ordinal).ToList(),
        checkYears.IsChecked == true ? SkipFrom - 1 : null, Education);

    // -- pieces -----------------------------------------------------------------------------------------------------

    private static StackPanel Section(string title, string? about, params UIElement?[] content)
    {
        var section = Ui.Column(10, Ui.Text(title, 15, FontWeights.SemiBold), about is null ? null : Ui.Secondary(about, 12));
        foreach (var part in content)
        {
            Ui.Add(section, part, 10);
        }
        return section;
    }
}

/// <summary>A set of job title boxes in groups (target roles, or non-target words), with their own after, and
/// a button that opens a field for adding one.</summary>
public sealed class TitleBoxes : StackPanel
{
    private List<TitleGroup> groups = [];
    private readonly List<string> own = [];
    private readonly StackPanel chips = Ui.Column(10);
    private readonly TextBlock? count;
    private readonly TextBox field = new() { MaxWidth = 280, MinWidth = 220 };
    private readonly Button add, opener;
    private readonly StackPanel adder;

    public HashSet<string> Ticked { get; private set; } = [];
    public event Action? Changed;

    public TitleBoxes(string placeholder, string addButton, bool allButtons)
    {
        if (allButtons)
        {
            count = Ui.Secondary("", 11);
            count.VerticalAlignment = VerticalAlignment.Center;
            Ui.Add(this, Ui.Row(8, Ui.Button("Tick All", () => SetAll(true), 11), Ui.Button("Untick All", () => SetAll(false), 11), count), 10);
        }
        Ui.Add(this, chips, 10);
        field.Size(12).Hint(placeholder);
        field.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                e.Handled = true;
                Add();
            }
        };
        add = Ui.Button("Add", Add, 12);
        add.IsEnabled = false;
        field.TextChanged += (_, _) => add.IsEnabled = field.Text.Trim().Length > 0;
        adder = Ui.Row(8, field, add, Ui.Button("Done", CloseAdder, 12));
        adder.Visible(false);
        opener = Ui.Button(addButton, OpenAdder, 11);
        opener.HorizontalAlignment = HorizontalAlignment.Left;
        Ui.Add(this, adder, 10);
        Ui.Add(this, opener, 10);
    }

    private List<string> Offered => groups.SelectMany(g => g.Titles).ToList();

    public void Fill(List<TitleGroup> given, List<string> saved)
    {
        groups = given;
        var (ticked, theirs) = SetupRules.Boxes(saved, Offered);
        Ticked = ticked;
        own.Clear();
        own.AddRange(theirs);
        Draw();
    }

    /// <summary>The ticked boxes: the profession's in order, then their own.</summary>
    public List<string> Chosen() => Offered.Where(Ticked.Contains).Concat(own.Where(Ticked.Contains)).ToList();

    private void Draw()
    {
        chips.Children.Clear();
        var sections = groups.Select(g => (g.Name, g.Titles)).ToList();
        if (own.Count > 0)
        {
            sections.Add(("Your own", own.ToList()));
        }
        foreach (var (name, titles) in sections)
        {
            Ui.Add(chips, Ui.Column(6, Ui.Secondary(name, 11), Chips.Make(titles, Ticked, Toggle)), 10);
        }
        Count();
    }

    private void Count()
    {
        if (count is not null)
        {
            count.Text = $"{Ticked.Count} ticked";
        }
    }

    private void Toggle(string title, bool on)
    {
        if (on) Ticked.Add(title); else Ticked.Remove(title);
        Count();
        Changed?.Invoke();
    }

    private void SetAll(bool on)
    {
        Ticked = on ? Offered.Concat(own).ToHashSet() : [];
        Draw();
        Changed?.Invoke();
    }

    private void OpenAdder()
    {
        adder.Visible(true);
        opener.Visible(false);
        field.Focus();
    }

    private void CloseAdder()
    {
        field.Clear();
        adder.Visible(false);
        opener.Visible(true);
    }

    /// <summary>Tick what was typed: the box already offered under any capitalization, or a new one of their own.</summary>
    private void Add()
    {
        var word = field.Text.Trim();
        if (word.Length == 0)
        {
            return;
        }
        var known = Offered.Concat(own).FirstOrDefault(t => string.Equals(t, word, StringComparison.OrdinalIgnoreCase));
        if (known is null)
        {
            own.Add(word);
            known = word;
        }
        Ticked.Add(known);
        field.Clear();
        Draw();
        Changed?.Invoke();
    }
}
