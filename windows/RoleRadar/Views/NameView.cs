// Before anything else, the app asks for its person's name, the Mac app's NameView: it goes with the app's
// check-ins. It also says to use Role Radar during business hours, since it reads job sites from their own
// internet connection. It shows whenever no name is saved: on a new copy, and once after updating from a
// version that didn't ask.
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using RoleRadar.Core;

namespace RoleRadar.Views;

public sealed class NameView : ScrollViewer
{
    private readonly AppModel model;
    private readonly TextBox name = new() { Width = 320, HorizontalAlignment = HorizontalAlignment.Left };
    private readonly Button save;
    private readonly Spinner spinner = new();
    private readonly TextBlock problem = Ui.Text("", 11, brush: Theme.Critical);
    private bool saving;

    public NameView(AppModel model)
    {
        this.model = model;
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        save = Ui.Button("Continue", async () => await Save(), accent: true);
        save.IsDefault = true;
        name.Size(13).Hint("Your name");
        name.TextChanged += (_, _) => Show();
        name.KeyDown += async (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                await Save();
            }
        };

        var warning = new Border
        {
            Padding = new Thickness(12), CornerRadius = new CornerRadius(8),
            Child = Ui.Row(10,
                Ui.Glyph(Glyphs.Warning, 14, Theme.Caution),
                Ui.Text("Role Radar checks companies' job sites from your own internet connection. Some companies block "
                        + "an address that sends unexpected traffic, so it's safest to use Role Radar during normal business "
                        + "hours and switch it off at night and on weekends.", 12)),
        }.Colored(Theme.CautionBackground, Border.BackgroundProperty);
        if (warning.Child is StackPanel row && row.Children[^1] is TextBlock text)
        {
            text.MaxWidth = 480; // wraps inside the box rather than running past it
        }
        spinner.Visible(false);

        Content = Ui.Column(22,
            Ui.Text("Welcome to Role Radar", 20, FontWeights.SemiBold),
            Ui.Column(6,
                Ui.Text("Your name", 15, FontWeights.SemiBold),
                Ui.Secondary("Helps keep track of Role Radar's users and decide which features to add or change. It's sent "
                             + "with the app's check-ins (the app version and your system's version) and nothing else."),
                name),
            warning,
            Ui.Row(10, save, spinner, problem));
        if (Content is FrameworkElement column)
        {
            column.Margin = new Thickness(32);
            column.MaxWidth = 560;
            column.HorizontalAlignment = HorizontalAlignment.Center;
        }
        IsVisibleChanged += (_, _) =>
        {
            if (IsVisible)
            {
                Dispatcher.BeginInvoke(() => name.Focus());
            }
        };
        Show();
    }

    private string Typed => string.Join(' ', name.Text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

    private void Show()
    {
        save.IsEnabled = Typed.Length > 0 && !saving;
        spinner.Visible(saving);
    }

    private async Task Save()
    {
        if (Typed.Length == 0 || saving)
        {
            return;
        }
        saving = true;
        problem.Text = "";
        Show();
        var error = await model.SaveNameAsync(Typed); // the model's setup changes, and the window moves on
        saving = false;
        problem.Text = error ?? "";
        Show();
    }
}
