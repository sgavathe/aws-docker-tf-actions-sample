using System.Text.Json;

namespace Backend.Services;

/// <summary>One study region: a graph built from whole states (see Data/regions.json).</summary>
public sealed record GraphRegion(string Id, string Label, string Name, string States, string Extracts, string File, bool Default);

/// <summary>
/// The study regions from Data/regions.json (the same file drives the weekly pipeline workflow).
/// Exactly one is the default: the region served when a request names none.
/// </summary>
public sealed class GraphRegions
{
    public static readonly string ConfigPath = Path.Combine(AppContext.BaseDirectory, "Data", "regions.json");

    public IReadOnlyList<GraphRegion> All { get; }
    public GraphRegion Default { get; }

    public GraphRegions(IReadOnlyList<GraphRegion> regions)
    {
        if (regions.Count == 0) throw new InvalidDataException("regions.json lists no regions.");
        if (regions.Select(r => r.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() != regions.Count)
            throw new InvalidDataException("regions.json has duplicate region ids.");
        All = regions;
        Default = regions.FirstOrDefault(r => r.Default) ?? regions[0];
    }

    public static GraphRegions Load(string? path = null)
    {
        path ??= ConfigPath;
        if (!System.IO.File.Exists(path))
        {
            // Older deployments without the file: a single default region on the original key.
            return new GraphRegions([new GraphRegion("default", "Default", "Default", "", "", "ci-graph.json", true)]);
        }
        using var doc = JsonDocument.Parse(System.IO.File.ReadAllBytes(path));
        var list = doc.RootElement.GetProperty("regions").EnumerateArray().Select(r => new GraphRegion(
            Id: r.GetProperty("id").GetString()!,
            Label: r.GetProperty("label").GetString()!,
            Name: r.TryGetProperty("name", out var n) ? n.GetString()! : r.GetProperty("label").GetString()!,
            States: r.TryGetProperty("states", out var s) ? s.GetString() ?? "" : "",
            Extracts: r.TryGetProperty("extracts", out var e) ? e.GetString() ?? "" : "",
            File: r.GetProperty("file").GetString()!,
            Default: r.TryGetProperty("default", out var d) && d.GetBoolean())).ToList();
        return new GraphRegions(list);
    }

    /// <summary>
    /// False when the graph was built for other states than the region lists, e.g. a Florida build
    /// saved as the Mid-Atlantic file. Circle builds and older graphs without an area pass.
    /// </summary>
    public static bool Matches(GraphRegion region, Backend.Models.InfraMeta meta)
    {
        if (string.IsNullOrWhiteSpace(region.States) || meta.Area is not { Type: "states" } area) return true;
        static HashSet<string> Set(IEnumerable<string> names) =>
            new(names.Select(n => n.Trim()).Where(n => n.Length > 0), StringComparer.OrdinalIgnoreCase);
        return Set(region.States.Split(',')).SetEquals(Set(area.Names));
    }

    /// <summary>The named region, the default when <paramref name="id"/> is empty, null when unknown.</summary>
    public GraphRegion? Find(string? id) =>
        string.IsNullOrWhiteSpace(id) ? Default
            : All.FirstOrDefault(r => string.Equals(r.Id, id.Trim(), StringComparison.OrdinalIgnoreCase));
}
