namespace Backend.Models;

// Shapes of the dependency-graph file written by pipeline/build_ci_graph.py.
// Classes (not positional records) so properties missing from the JSON keep their
// defaults, e.g. an edge without "directed" stays directed.

public sealed class InfraGraphDocument
{
    public InfraMeta Meta { get; init; } = new();
    public List<InfraNode> Nodes { get; init; } = [];
    public List<InfraEdge> Edges { get; init; } = [];
}

public sealed class InfraMeta
{
    public string Region { get; init; } = "";
    public InfraCenter? Center { get; init; }
    public double RadiusMi { get; init; }
    public string GeneratedUtc { get; init; } = "";
    public string Source { get; init; } = "";
    public string Attribution { get; init; } = "";
    public string License { get; init; } = "";
    public bool Sample { get; init; }
    public bool Inferred { get; init; } = true;
    public string Notes { get; init; } = "";
    public Dictionary<string, int> NodeCounts { get; init; } = new();
    public int EdgeCount { get; init; }
}

public sealed class InfraCenter
{
    public double Lat { get; init; }
    public double Lon { get; init; }
}

public sealed class InfraNode
{
    public string Id { get; init; } = "";
    public string? Osm { get; init; }
    public string Name { get; init; } = "";
    public string Sector { get; init; } = "";
    public string Kind { get; init; } = "";
    public double Lon { get; init; }
    public double Lat { get; init; }
    public double? VoltageKv { get; init; }
    /// <summary>Why this asset counts as a power source (plant, tie leaving the area); null otherwise.</summary>
    public string? Source { get; init; }
    /// <summary>Dependency types this asset can ride out (backup generator, stored water, radio).</summary>
    public List<string> Backup { get; init; } = [];
}

public sealed class InfraEdge
{
    public string Id { get; init; } = "";
    /// <summary>Supplier.</summary>
    public string From { get; init; } = "";
    /// <summary>Dependent.</summary>
    public string To { get; init; } = "";
    /// <summary>What flows along the edge: power, water or comms.</summary>
    public string Type { get; init; } = "";
    /// <summary>grid_link (mapped wires), distribution_link, or service (inferred nearest supplier).</summary>
    public string Kind { get; init; } = "";
    public string Basis { get; init; } = "";
    public bool Directed { get; init; } = true;
    public double? VoltageKv { get; init; }
    /// <summary>Wire route as [lon, lat] pairs; only for mapped power lines.</summary>
    public List<double[]>? Coords { get; init; }
}

// ---- Impact analysis results ----

public enum ImpactStatus { Failed, Degraded }

public sealed record AssetImpact(
    string Id,
    string Name,
    string Sector,
    string Kind,
    ImpactStatus Status,
    int Hop,
    string Cause,
    string? Via,
    string? ViaType, // what the failure travelled over from Via: power, water or comms
    IReadOnlyList<string> Lost,
    double DistanceKm);

public sealed record SectorImpact(string Sector, int Total, int Failed, int Degraded);

public sealed record HopImpact(int Hop, int Failed, int Degraded);

public sealed record ImpactSummary(
    int DirectlyHit,
    int Failed,
    int Degraded,
    int SeveredLines,
    int MaxHops,
    double ReachKm,
    double AreaKm2,
    IReadOnlyList<SectorImpact> Sectors,
    IReadOnlyList<HopImpact> Hops);

public sealed record ImpactResult(
    ImpactSummary Summary,
    IReadOnlyList<AssetImpact> Impacts,
    IReadOnlyList<string> SeveredEdges,
    string GraphVersion,
    bool SampleData);
