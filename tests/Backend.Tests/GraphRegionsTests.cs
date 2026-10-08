using Backend.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Backend.Tests;

public class GraphRegionsTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("regions-").FullName;

    private static readonly GraphRegions Regions = new([
        new GraphRegion("midatlantic", "DMV / Mid-Atlantic", "Virginia, Maryland and DC", "Virginia", "", "ci-graph.json", true),
        new GraphRegion("florida", "Florida", "Florida", "Florida", "", "florida.json", false),
    ]);

    private static string Graph(string region, string states = "")
    {
        var area = states == ""
            ? ""
            : $", \"area\": {{ \"type\": \"states\", \"names\": [{string.Join(", ", states.Split(',').Select(n => $"\"{n}\""))}] }}";
        return "{ \"meta\": { \"region\": \"" + region + "\"" + area + " }, " +
               "\"nodes\": [ { \"id\": \"a\", \"name\": \"A\", \"sector\": \"energy\", \"kind\": \"substation\", \"lon\": -82, \"lat\": 28 } ], " +
               "\"edges\": [] }";
    }

    private InfrastructureGraphProvider Provider(Dictionary<string, string?> settings) =>
        new(new ConfigurationBuilder().AddInMemoryCollection(settings).Build(),
            NullLogger<InfrastructureGraphProvider>.Instance, Regions);

    [Fact]
    public void Find_defaults_ignores_case_and_rejects_unknown()
    {
        Assert.Equal("midatlantic", Regions.Find(null)!.Id);
        Assert.Equal("midatlantic", Regions.Find(" ")!.Id);
        Assert.Equal("florida", Regions.Find("Florida")!.Id);
        Assert.Null(Regions.Find("texas"));
    }

    [Fact]
    public void Shipped_regions_file_is_valid()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", "backend", "Data", "regions.json");
        var regions = GraphRegions.Load(Path.GetFullPath(path));
        Assert.Equal("midatlantic", regions.Default.Id);
        Assert.Equal("ci-graph.json", regions.Default.File);   // the original S3 key keeps working
        Assert.Contains(regions.All, r => r.Id == "florida");
        Assert.All(regions.All, r => Assert.False(string.IsNullOrWhiteSpace(r.States)));
    }

    [Fact]
    public async Task Loads_each_region_from_the_folder_of_CiGraph_Path()
    {
        File.WriteAllText(Path.Combine(_dir, "ci-graph.json"), Graph("Mid-Atlantic"));
        File.WriteAllText(Path.Combine(_dir, "florida.json"), Graph("Florida"));
        var p = Provider(new() { ["CiGraph:Path"] = Path.Combine(_dir, "ci-graph.json") });

        Assert.Equal("Mid-Atlantic", (await p.GetAsync(CancellationToken.None)).Meta.Region);
        var fl = await p.GetAsync(Regions.Find("florida")!, CancellationToken.None);
        Assert.Equal("Florida", fl!.Meta.Region);
        Assert.True(await p.IsAvailableAsync(Regions.Find("florida")!, CancellationToken.None));
    }

    [Fact]
    public async Task Unpublished_region_is_null_never_the_sample()
    {
        var p = Provider(new() { ["CiGraph:Dir"] = _dir });
        var florida = Regions.Find("florida")!;

        Assert.Null(await p.GetAsync(florida, CancellationToken.None));
        Assert.False(await p.IsAvailableAsync(florida, CancellationToken.None));
        // The default region still works, from the bundled sample.
        Assert.Equal("bundled sample", (await p.GetAsync(CancellationToken.None)).Origin);
    }

    [Fact]
    public async Task Graph_built_for_other_states_is_refused()
    {
        // The mistake this guards against: a Florida build saved as the Mid-Atlantic file.
        File.WriteAllText(Path.Combine(_dir, "ci-graph.json"), Graph("Florida", "Florida"));
        File.WriteAllText(Path.Combine(_dir, "florida.json"), Graph("Mid-Atlantic", "Virginia"));
        var p = Provider(new() { ["CiGraph:Path"] = Path.Combine(_dir, "ci-graph.json") });

        Assert.Equal("bundled sample", (await p.GetAsync(CancellationToken.None)).Origin);
        Assert.Null(await p.GetAsync(Regions.Find("florida")!, CancellationToken.None));
    }

    [Fact]
    public async Task Graph_built_for_the_right_states_loads()
    {
        File.WriteAllText(Path.Combine(_dir, "florida.json"), Graph("Florida", "Florida"));
        var p = Provider(new() { ["CiGraph:Dir"] = _dir });
        Assert.Equal("Florida", (await p.GetAsync(Regions.Find("florida")!, CancellationToken.None))!.Meta.Region);
    }

    public void Dispose() => Directory.Delete(_dir, recursive: true);
}
