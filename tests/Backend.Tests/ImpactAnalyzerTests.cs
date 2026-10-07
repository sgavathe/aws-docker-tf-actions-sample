using System.Text;
using System.Text.Json;
using Backend.Models;
using Backend.Services;
using Xunit;

namespace Backend.Tests;

/// <summary>
/// Small hand-built network (lon/lat near Richmond):
///
///   P (plant) --e0-- S1 --e1-- S2 --e4--> PS (pump) --e5--> WT (tower) --e6--> H (hospital)
///                     \        |  \--e8--> X (exchange) --e9--> T (cell tower)
///                      \--e2-- S3 --e7--> H,  S3 --e10--> T
///                              (e3: S2-S3 wire, so the grid has a loop)
/// </summary>
public class ImpactAnalyzerTests
{
    private const string TinyGraph = """
    {
      "meta": { "region": "test", "sample": true },
      "nodes": [
        { "id": "P",  "name": "P",  "sector": "energy", "kind": "plant", "lon": -77.60, "lat": 37.50, "source": "power plant" },
        { "id": "S1", "name": "S1", "sector": "energy", "kind": "substation", "lon": -77.50, "lat": 37.50 },
        { "id": "S2", "name": "S2", "sector": "energy", "kind": "substation", "lon": -77.40, "lat": 37.55 },
        { "id": "S3", "name": "S3", "sector": "energy", "kind": "substation", "lon": -77.40, "lat": 37.45 },
        { "id": "PS", "name": "PS", "sector": "water", "kind": "pumping_station", "lon": -77.35, "lat": 37.56 },
        { "id": "WT", "name": "WT", "sector": "water", "kind": "water_tower", "lon": -77.30, "lat": 37.56 },
        { "id": "H",  "name": "H",  "sector": "health", "kind": "hospital", "lon": -77.30, "lat": 37.45, "backup": ["power", "water", "comms"] },
        { "id": "X",  "name": "X",  "sector": "communications", "kind": "telecom_exchange", "lon": -77.42, "lat": 37.60, "backup": ["power"] },
        { "id": "T",  "name": "T",  "sector": "communications", "kind": "comm_tower", "lon": -77.45, "lat": 37.40 }
      ],
      "edges": [
        { "id": "e0", "from": "P",  "to": "S1", "type": "power", "kind": "grid_link", "basis": "t", "coords": [[-77.60, 37.50], [-77.50, 37.50]] },
        { "id": "e1", "from": "S1", "to": "S2", "type": "power", "kind": "grid_link", "basis": "t", "coords": [[-77.50, 37.50], [-77.40, 37.55]] },
        { "id": "e2", "from": "S1", "to": "S3", "type": "power", "kind": "grid_link", "basis": "t", "coords": [[-77.50, 37.50], [-77.40, 37.45]] },
        { "id": "e3", "from": "S2", "to": "S3", "type": "power", "kind": "grid_link", "basis": "t", "directed": false, "coords": [[-77.40, 37.55], [-77.40, 37.45]] },
        { "id": "e4", "from": "S2", "to": "PS", "type": "power", "kind": "service", "basis": "t" },
        { "id": "e5", "from": "PS", "to": "WT", "type": "water", "kind": "service", "basis": "t" },
        { "id": "e6", "from": "WT", "to": "H",  "type": "water", "kind": "service", "basis": "t" },
        { "id": "e7", "from": "S3", "to": "H",  "type": "power", "kind": "service", "basis": "t" },
        { "id": "e8", "from": "S2", "to": "X",  "type": "power", "kind": "service", "basis": "t" },
        { "id": "e9", "from": "X",  "to": "T",  "type": "comms", "kind": "service", "basis": "t" },
        { "id": "e10", "from": "S3", "to": "T", "type": "power", "kind": "service", "basis": "t" },
        { "id": "bad", "from": "S1", "to": "nowhere", "type": "power", "kind": "service", "basis": "t" }
      ]
    }
    """;

    private static readonly InfrastructureGraph Tiny =
        InfrastructureGraph.Parse(Encoding.UTF8.GetBytes(TinyGraph), "test");

    internal static AreaPolygon Box(double lon0, double lat0, double lon1, double lat1)
    {
        var geometry = JsonSerializer.SerializeToElement(new
        {
            type = "Polygon",
            coordinates = new[]
            {
                new[]
                {
                    new[] { lon0, lat0 }, new[] { lon1, lat0 }, new[] { lon1, lat1 },
                    new[] { lon0, lat1 }, new[] { lon0, lat0 }
                }
            }
        });
        Assert.True(AreaPolygon.TryParse(geometry, out var area, out var error), error);
        return area!;
    }

    private static Dictionary<string, AssetImpact> ById(ImpactResult r) => r.Impacts.ToDictionary(i => i.Id);

    [Fact]
    public void Parse_DropsEdgesToUnknownNodes()
    {
        Assert.Equal(9, Tiny.Nodes.Count);
        Assert.Equal(11, Tiny.Edges.Count);
        Assert.DoesNotContain(Tiny.Edges, e => e.Id == "bad");
        Assert.False(Tiny.Edges.Single(e => e.Id == "e3").Directed);
        Assert.True(Tiny.Edges.Single(e => e.Id == "e1").Directed);
    }

    [Fact]
    public void HitSubstation_CascadesThroughWater_ButLoopKeepsOtherSubstationLive()
    {
        var r = ImpactAnalyzer.Analyze(Tiny, Box(-77.41, 37.54, -77.39, 37.56)); // around S2
        var x = ById(r);

        Assert.Equal(1, r.Summary.DirectlyHit);
        Assert.Equal(new[] { "e1", "e3" }, r.SeveredEdges);
        Assert.Equal((ImpactStatus.Failed, 0), (x["S2"].Status, x["S2"].Hop));
        Assert.Equal((ImpactStatus.Failed, 1, "S2"), (x["PS"].Status, x["PS"].Hop, x["PS"].Via));
        Assert.Equal((ImpactStatus.Degraded, 1), (x["X"].Status, x["X"].Hop));          // backup power
        Assert.Equal((ImpactStatus.Failed, 2, "PS"), (x["WT"].Status, x["WT"].Hop, x["WT"].Via));
        Assert.Equal("water", x["WT"].ViaType);
        Assert.Equal("power", x["PS"].ViaType);
        Assert.Null(x["S2"].ViaType);
        Assert.Equal((ImpactStatus.Degraded, 3), (x["H"].Status, x["H"].Hop));          // stored water
        Assert.Equal(new[] { "water" }, x["H"].Lost);

        Assert.False(x.ContainsKey("S3"));   // still fed from S1 over e2
        Assert.False(x.ContainsKey("T"));    // its exchange is degraded, not down
        Assert.Equal((3, 2, 3), (r.Summary.Failed, r.Summary.Degraded, r.Summary.MaxHops));
    }

    [Fact]
    public void CutWire_IslandsTheGrid_WithNoAssetInsideTheArea()
    {
        var r = ImpactAnalyzer.Analyze(Tiny, Box(-77.56, 37.49, -77.54, 37.51)); // across e0 only
        var x = ById(r);

        Assert.Equal(0, r.Summary.DirectlyHit);
        Assert.Equal(new[] { "e0" }, r.SeveredEdges);
        Assert.False(x.ContainsKey("P"));                                           // the plant still runs
        Assert.Equal((ImpactStatus.Failed, 1), (x["S1"].Status, x["S1"].Hop));
        Assert.Equal((2, "S1"), (x["S2"].Hop, x["S2"].Via));
        Assert.Equal("power", x["S2"].ViaType);
        Assert.Equal((2, "S1"), (x["S3"].Hop, x["S3"].Via));
        Assert.Equal((ImpactStatus.Failed, 3), (x["T"].Status, x["T"].Hop));
        Assert.Equal((ImpactStatus.Failed, 4), (x["WT"].Status, x["WT"].Hop));
        Assert.Equal((ImpactStatus.Degraded, 3), (x["H"].Status, x["H"].Hop));      // earliest loss wins
        Assert.Equal(new[] { "power", "water" }, x["H"].Lost.OrderBy(s => s));
        Assert.Equal((6, 2, 4), (r.Summary.Failed, r.Summary.Degraded, r.Summary.MaxHops));
        Assert.True(r.Summary.ReachKm > 15);
    }

    [Fact]
    public void AreaAwayFromEverything_ChangesNothing()
    {
        var r = ImpactAnalyzer.Analyze(Tiny, Box(-70.1, 40.0, -70.0, 40.1));
        Assert.Empty(r.Impacts);
        Assert.Empty(r.SeveredEdges);
        Assert.Equal(0, r.Summary.MaxHops);
        Assert.True(r.Summary.AreaKm2 > 80);
    }

    [Fact]
    public void Summary_CountsEverySector()
    {
        var r = ImpactAnalyzer.Analyze(Tiny, Box(-77.41, 37.44, -77.39, 37.46)); // around S3
        Assert.Equal(new[] { "energy", "water", "communications", "health" }, r.Summary.Sectors.Select(s => s.Sector));
        Assert.Equal(4, r.Summary.Sectors[0].Total);
        Assert.Equal(1, r.Summary.Sectors.Single(s => s.Sector == "communications").Failed);    // T
        Assert.Equal(1, r.Summary.Sectors.Single(s => s.Sector == "health").Degraded);          // H
    }

    [Fact]
    public void BundledSample_EveryCascadeStepPointsBackOneHop()
    {
        var sample = InfrastructureGraph.Parse(File.ReadAllBytes(SamplePath()), "sample");
        Assert.True(sample.Meta.Sample);

        // ~2 km box over downtown Richmond
        var r = ImpactAnalyzer.Analyze(sample, Box(-77.448, 37.5317, -77.424, 37.5497));
        Assert.True(r.Summary.DirectlyHit >= 1);
        Assert.True(r.Summary.Failed > r.Summary.DirectlyHit);
        Assert.True(r.Summary.MaxHops >= 3);

        var x = ById(r);
        foreach (var i in r.Impacts.Where(i => i.Via is not null))
        {
            Assert.Equal(ImpactStatus.Failed, x[i.Via!].Status);
            Assert.Equal(x[i.Via!].Hop + 1, i.Hop);
        }
        Assert.Equal(r.Impacts.OrderBy(i => i.Hop).Select(i => i.Id), r.Impacts.Select(i => i.Id));
    }

    internal static string SamplePath()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "backend", "Data", "ci-graph.sample.json");
            if (File.Exists(candidate)) return candidate;
            dir = dir.Parent;
        }
        return InfrastructureGraphProvider.SamplePath; // copied next to the test assembly
    }
}
