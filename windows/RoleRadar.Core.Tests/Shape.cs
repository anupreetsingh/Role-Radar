using System.Collections;
using System.Reflection;
using System.Text.Json;

namespace RoleRadar.Core.Tests;

/// <summary>Whether a reply has every field the app reads it for: each one its type doesn't take as optional
/// (nullable), in nested objects, lists and dictionaries too. A field missing from the JSON would otherwise
/// be read as nothing, and show as a blank in the app.</summary>
public static class Shape
{
    private static readonly NullabilityInfoContext Nullability = new();

    public static void AssertComplete<T>(string json)
    {
        using var document = JsonDocument.Parse(json);
        var missing = Missing(typeof(T), document.RootElement, typeof(T).Name).ToList();
        Assert.True(missing.Count == 0, $"role-radar's reply lacks what the app reads: {string.Join(", ", missing)}");
    }

    private static IEnumerable<string> Missing(Type type, JsonElement json, string path)
    {
        if (Nullable.GetUnderlyingType(type) is { } inner)
        {
            type = inner;
        }
        if (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(Dictionary<,>))
        {
            return json.ValueKind != JsonValueKind.Object ? [$"{path} (not an object)"]
                : json.EnumerateObject().SelectMany(entry => Missing(type.GetGenericArguments()[1], entry.Value, $"{path}[{entry.Name}]"));
        }
        if (type != typeof(string) && typeof(IEnumerable).IsAssignableFrom(type) && type.IsGenericType)
        {
            return json.ValueKind != JsonValueKind.Array ? [$"{path} (not a list)"]
                : json.EnumerateArray().SelectMany((item, i) => Missing(type.GetGenericArguments()[0], item, $"{path}[{i}]"));
        }
        if (type.Namespace != typeof(Json).Namespace || !type.IsClass)
        {
            return []; // a string, number or flag: reading it checks its kind
        }
        return json.ValueKind != JsonValueKind.Object ? [$"{path} (not an object)"] : Fields(type, json, path);
    }

    private static IEnumerable<string> Fields(Type type, JsonElement json, string path)
    {
        foreach (var property in type.GetProperties(BindingFlags.Public | BindingFlags.Instance).Where(p => p.CanWrite))
        {
            var name = JsonNamingPolicy.SnakeCaseLower.ConvertName(property.Name);
            var optional = Nullable.GetUnderlyingType(property.PropertyType) is not null
                || (!property.PropertyType.IsValueType && Nullability.Create(property).WriteState == NullabilityState.Nullable);
            if (!json.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
            {
                if (!optional)
                {
                    yield return $"{path}.{name}";
                }
                continue;
            }
            foreach (var missing in Missing(property.PropertyType, value, $"{path}.{name}"))
            {
                yield return missing;
            }
        }
    }
}
