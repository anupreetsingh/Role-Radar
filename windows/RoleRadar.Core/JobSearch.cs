// Live Tracking's search: the new jobs with every typed word starting a word of their title, company or
// place, ignoring case and accents. "eng" finds Engineer and "montreal" Montréal, but "ai" doesn't
// find Maintain. The Mac app's JobList.matching.
using System.Globalization;
using System.Text;

namespace RoleRadar.Core;

public static class JobSearch
{
    public static List<Match> Matching(IEnumerable<Match> jobs, string query)
    {
        var words = query.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries).Select(Fold).ToArray();
        if (words.Length == 0)
        {
            return jobs.ToList();
        }
        return jobs.Where(job =>
        {
            var text = Fold($"{job.Title} {job.Company} {job.Location}");
            return words.All(word => StartsAWord(word, text));
        }).ToList();
    }

    /// <summary>Lowercase, without accents.</summary>
    public static string Fold(string text)
    {
        var plain = new StringBuilder(text.Length);
        foreach (var c in text.Normalize(NormalizationForm.FormD))
        {
            if (CharUnicodeInfo.GetUnicodeCategory(c) != UnicodeCategory.NonSpacingMark)
            {
                plain.Append(c);
            }
        }
        return plain.ToString().Normalize(NormalizationForm.FormC).ToLowerInvariant();
    }

    private static bool StartsAWord(string word, string text)
    {
        for (var at = text.IndexOf(word, StringComparison.Ordinal); at >= 0; at = text.IndexOf(word, at + 1, StringComparison.Ordinal))
        {
            if (at == 0 || !char.IsLetterOrDigit(text[at - 1]))
            {
                return true;
            }
        }
        return false;
    }
}
