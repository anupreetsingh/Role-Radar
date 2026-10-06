// Setup's rules apart from its pages, as the Mac app's SetupView has them: which boxes a saved list
// ticks, what a page's answers save as, and how a company is described.
namespace RoleRadar.Core;

public static class SetupRules
{
    public static readonly string[] Pages = ["Profession", "Countries", "Companies", "Roles", "Qualifications", "Alerts"];
    public static readonly (string Value, string Name)[] Degrees =
        [("none", "No degree yet"), ("bachelors", "Bachelor's"), ("masters", "Master's"), ("phd", "PhD")];
    public const string AppPasswords = "https://myaccount.google.com/apppasswords";

    private static readonly Dictionary<string, string> SiteNames = new()
    {
        ["oracle_hcm"] = "Oracle", ["smartrecruiters"] = "SmartRecruiters", ["icims"] = "iCIMS",
        ["bamboohr"] = "BambooHR", ["hrmdirect"] = "HRM Direct", ["tiktok"] = "TikTok",
    };

    /// <summary>The boxes for what's saved: the offered ones ticked (any capitalization), and their own after.</summary>
    public static (HashSet<string> Ticked, List<string> Own) Boxes(IEnumerable<string> saved, IReadOnlyList<string> offered)
    {
        var ticked = new HashSet<string>();
        var own = new List<string>();
        foreach (var word in saved)
        {
            var known = offered.FirstOrDefault(o => string.Equals(o, word, StringComparison.OrdinalIgnoreCase));
            if (known is not null)
            {
                ticked.Add(known);
            }
            else
            {
                own.Add(word);
                ticked.Add(word);
            }
        }
        return (ticked, own);
    }

    /// <summary>One per line or comma, trimmed.</summary>
    public static List<string> Lines(string text) =>
        text.Split(['\n', '\r', ','], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList();

    /// <summary>What someone skipping jobs that ask for `from`+ years still hears about.</summary>
    public static string StillAlert(int from) => from switch
    {
        1 => "no experience",
        2 => "no experience or 1+ year",
        3 => "no experience, 1+ or 2+ years",
        _ => $"anything from no experience up to {from - 1}+ years",
    };

    /// <summary>"Workday · US, CA", and why it isn't tracked unless they turned it off themselves.</summary>
    public static string About(CompanyRow company)
    {
        var parts = new List<string>();
        if (company.Site is { } site && site != "generic")
        {
            parts.Add(SiteNames.GetValueOrDefault(site) ?? char.ToUpperInvariant(site[0]) + site[1..]);
        }
        if (company.Countries.Count > 0)
        {
            parts.Add(string.Join(", ", company.Countries));
        }
        if (company.Why is { } why && !company.Off)
        {
            parts.Add($"not tracked: {why}");
        }
        return string.Join(" · ", parts);
    }

    /// <summary>"United States", "United States and India", "Canada, Australia and India".</summary>
    public static string CountryNames(IEnumerable<string> names)
    {
        var list = names.ToList();
        return list.Count <= 1 ? list.FirstOrDefault() ?? "" : string.Join(", ", list[..^1]) + " and " + list[^1];
    }

    /// <summary>The countries ticked as `companies_for` keys them: in the offered order, "US+IN".</summary>
    public static string CountriesKey(SetupState setup, IReadOnlySet<string> picked) =>
        string.Join("+", setup.CountryOptions.Select(c => c.Code).Where(picked.Contains));

    /// <summary>What a page's answers say about the profile, for `setup profile`.</summary>
    public sealed record Profile(List<string> Roles, List<string> Exclude, List<string> Locations, List<string> Countries,
                                 int? MaxExperienceYears, string Education)
    {
        /// <summary>The JSON `setup profile` takes. Education goes once its page is open: until then the
        /// saved one (or none) stands.</summary>
        public string Json(bool withEducation)
        {
            var data = new SortedDictionary<string, object?>
            {
                ["roles"] = Roles, ["exclude"] = Exclude, ["locations"] = Locations, ["countries"] = Countries,
                ["max_experience_years"] = MaxExperienceYears,
            };
            if (withEducation)
            {
                data["education"] = Education;
            }
            return Core.Json.Write(data);
        }

        /// <summary>The answers as they stand, whatever the page: to tell when they've changed since saved.</summary>
        public string Fingerprint => Json(withEducation: true);
    }
}
