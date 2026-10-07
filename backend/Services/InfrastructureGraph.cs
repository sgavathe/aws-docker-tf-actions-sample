using System.Security.Cryptography;
using System.Text.Json;
using Backend.Models;

namespace Backend.Services;

/// <summary>
/// The loaded dependency graph plus the indexes the impact analysis needs.
/// Immutable after construction, so one instance is safely shared by all requests.
/// </summary>
public sealed class InfrastructureGraph
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    public InfraMeta Meta { get; }
    public IReadOnlyList<InfraNode> Nodes { get; }
    public IReadOnlyList<InfraEdge> Edges { get; }

    /// <summary>The file exactly as loaded; served to the map by GET /api/infrastructure/graph.</summary>
    public byte[] RawJson { get; }
    /// <summary>Short content hash; changes whenever the data file changes.</summary>
    public string Version { get; }
    public string ETag => $"\"{Version}\"";
    /// <summary>Where the data came from (s3://..., a file path, or "bundled sample").</summary>
    public string Origin { get; }

    internal IReadOnlyDictionary<string, int> IndexOf { get; }
    /// <summary>Power-grid adjacency between energy assets (undirected): (neighbour, edge index).</summary>
    internal List<(int Node, int Edge)>[] GridNeighbors { get; }
    /// <summary>Service edges (supplier -> dependent), indexed both ways.</summary>
    internal List<int>[] ServiceIn { get; }
    internal List<int>[] ServiceOut { get; }
    internal int[] EdgeFrom { get; }
    internal int[] EdgeTo { get; }
    internal bool[] IsGrid { get; }

    private InfrastructureGraph(InfraGraphDocument doc, byte[] raw, string origin)
    {
        Meta = doc.Meta;
        Origin = origin;
        RawJson = raw;
        Version = Convert.ToHexString(SHA256.HashData(raw))[..16].ToLowerInvariant();

        var nodes = new List<InfraNode>();
        var index = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var n in doc.Nodes)
        {
            if (string.IsNullOrEmpty(n.Id) || index.ContainsKey(n.Id)) continue;
            index[n.Id] = nodes.Count;
            nodes.Add(n);
        }
        Nodes = nodes;
        IndexOf = index;

        // Drop edges that point at nodes we don't have rather than failing the whole load.
        var edges = doc.Edges
            .Where(e => index.ContainsKey(e.From) && index.ContainsKey(e.To) && e.From != e.To)
            .ToList();
        Edges = edges;

        var n0 = nodes.Count;
        IsGrid = nodes.Select(x => x.Sector == "energy").ToArray();
        GridNeighbors = NewLists<(int Node, int Edge)>(n0);
        ServiceIn = NewLists<int>(n0);
        ServiceOut = NewLists<int>(n0);
        EdgeFrom = new int[edges.Count];
        EdgeTo = new int[edges.Count];

        for (var i = 0; i < edges.Count; i++)
        {
            int a = index[edges[i].From], b = index[edges[i].To];
            EdgeFrom[i] = a;
            EdgeTo[i] = b;
            if (edges[i].Type == "power" && IsGrid[a] && IsGrid[b])
            {
                // Power can flow either way along a wire, so the grid is treated as undirected.
                GridNeighbors[a].Add((b, i));
                GridNeighbors[b].Add((a, i));
            }
            else
            {
                ServiceOut[a].Add(i);
                ServiceIn[b].Add(i);
            }
        }
    }

    private static List<T>[] NewLists<T>(int n)
    {
        var lists = new List<T>[n];
        for (var i = 0; i < n; i++) lists[i] = [];
        return lists;
    }

    /// <exception cref="InvalidDataException">The file isn't a usable graph.</exception>
    public static InfrastructureGraph Parse(byte[] json, string origin)
    {
        InfraGraphDocument? doc;
        try
        {
            doc = JsonSerializer.Deserialize<InfraGraphDocument>(json, JsonOptions);
        }
        catch (JsonException ex)
        {
            throw new InvalidDataException($"Graph file from {origin} is not valid JSON: {ex.Message}", ex);
        }
        if (doc is null || doc.Nodes.Count == 0)
            throw new InvalidDataException($"Graph file from {origin} has no nodes.");
        return new InfrastructureGraph(doc, json, origin);
    }
}
