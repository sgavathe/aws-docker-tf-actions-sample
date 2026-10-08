using System.Collections.Concurrent;
using System.Net;
using Amazon.Runtime;
using Amazon.S3;
using Amazon.S3.Model;

namespace Backend.Services;

public interface IInfrastructureGraphProvider
{
    GraphRegions Regions { get; }

    /// <summary>The region's graph, or null when it hasn't been published yet.</summary>
    Task<InfrastructureGraph?> GetAsync(GraphRegion region, CancellationToken ct);

    /// <summary>Whether the region's graph exists, without loading it (cached like the graph).</summary>
    Task<bool> IsAvailableAsync(GraphRegion region, CancellationToken ct);
}

public static class GraphProviderExtensions
{
    /// <summary>The default region's graph; never null (falls back to the bundled sample).</summary>
    public static async Task<InfrastructureGraph> GetAsync(this IInfrastructureGraphProvider p, CancellationToken ct) =>
        (await p.GetAsync(p.Regions.Default, ct))!;
}

/// <summary>
/// SINGLETON that loads one dependency graph per study region (Data/regions.json) and keeps
/// each in memory once it has been asked for.
///
/// Where a region's data comes from, first match wins:
///   1. S3        CiGraph__S3Bucket. The default region reads CiGraph__S3Key
///                (default data/ci-graph.json); others read &lt;same folder&gt;/&lt;region file&gt;,
///                e.g. data/florida.json. The weekly pipeline workflow writes them.
///   2. A folder  CiGraph__Dir (default: the folder of CiGraph__Path) holding &lt;region file&gt;.
///   3. A file    CiGraph__Path, default region only (local dev, or a volume in the ECS task).
///   4. Built in  Data/ci-graph.sample.json, default region only, so the app always works.
/// A non-default region with no published file is "not available" (null), never the sample.
///
/// Each graph is re-checked every CiGraph__RefreshMinutes (default 15). For S3 that is a
/// conditional GET on the ETag, so an unchanged file costs one cheap request and no parsing.
/// A failed refresh keeps serving the last good graph.
/// </summary>
public sealed class InfrastructureGraphProvider : IInfrastructureGraphProvider, IDisposable
{
    public static readonly string SamplePath = Path.Combine(AppContext.BaseDirectory, "Data", "ci-graph.sample.json");

    private sealed class Slot
    {
        public readonly SemaphoreSlim Gate = new(1, 1);
        public InfrastructureGraph? Graph;
        public DateTimeOffset CheckedAt;
        public string? ETag;
        public bool? Available;
        public DateTimeOffset AvailableCheckedAt;
    }

    private readonly IConfiguration _config;
    private readonly ILogger<InfrastructureGraphProvider> _logger;
    private readonly ConcurrentDictionary<string, Slot> _slots = new(StringComparer.OrdinalIgnoreCase);
    private AmazonS3Client? _s3;

    public InfrastructureGraphProvider(IConfiguration config, ILogger<InfrastructureGraphProvider> logger,
        GraphRegions? regions = null)
    {
        _config = config;
        _logger = logger;
        Regions = regions ?? GraphRegions.Load();
    }

    public GraphRegions Regions { get; }

    private TimeSpan Refresh => TimeSpan.FromMinutes(Math.Max(1, _config.GetValue("CiGraph:RefreshMinutes", 15)));
    private string? Bucket => _config["CiGraph:S3Bucket"] is { Length: > 0 } b && !string.IsNullOrWhiteSpace(b) ? b : null;

    private string S3Key(GraphRegion region)
    {
        var defaultKey = _config["CiGraph:S3Key"] ?? "data/ci-graph.json";
        if (region.Default) return defaultKey;
        var slash = defaultKey.LastIndexOf('/');
        return (slash >= 0 ? defaultKey[..(slash + 1)] : "") + region.File;
    }

    private string? LocalPath(GraphRegion region)
    {
        var dir = _config["CiGraph:Dir"];
        var path = _config["CiGraph:Path"];
        if (string.IsNullOrWhiteSpace(dir) && !string.IsNullOrWhiteSpace(path)) dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrWhiteSpace(dir))
        {
            var candidate = Path.Combine(dir, region.File);
            if (File.Exists(candidate)) return candidate;
        }
        if (region.Default && !string.IsNullOrWhiteSpace(path) && File.Exists(path)) return path;
        return null;
    }

    public async Task<InfrastructureGraph?> GetAsync(GraphRegion region, CancellationToken ct)
    {
        var slot = _slots.GetOrAdd(region.Id, _ => new Slot());
        if (slot.CheckedAt != default && DateTimeOffset.UtcNow - slot.CheckedAt < Refresh)
            return slot.Graph;                                 // a missing region is re-checked after Refresh too

        await slot.Gate.WaitAsync(ct);
        try
        {
            if (slot.CheckedAt != default && DateTimeOffset.UtcNow - slot.CheckedAt < Refresh)
                return slot.Graph;
            slot.Graph = await LoadAsync(region, slot, ct);
            slot.CheckedAt = DateTimeOffset.UtcNow;
            slot.Available = slot.Graph is not null;
            slot.AvailableCheckedAt = slot.CheckedAt;
            return slot.Graph;
        }
        finally
        {
            slot.Gate.Release();
        }
    }

    public async Task<bool> IsAvailableAsync(GraphRegion region, CancellationToken ct)
    {
        if (region.Default) return true;                       // falls back to the sample
        var slot = _slots.GetOrAdd(region.Id, _ => new Slot());
        if (slot.Graph is not null) return true;
        if (slot.Available is { } known && DateTimeOffset.UtcNow - slot.AvailableCheckedAt < Refresh) return known;

        var available = LocalPath(region) is not null;
        if (!available && Bucket is { } bucket)
        {
            try
            {
                _s3 ??= new AmazonS3Client();
                await _s3.GetObjectMetadataAsync(bucket, S3Key(region), ct);
                available = true;
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Forbidden)
            {
                available = false;
            }
            catch (Exception ex) when (ex is AmazonServiceException or AmazonClientException or HttpRequestException)
            {
                _logger.LogWarning(ex, "Could not check region {Region}", region.Id);
                return false;                                  // don't cache a transient failure
            }
        }
        slot.Available = available;
        slot.AvailableCheckedAt = DateTimeOffset.UtcNow;
        return available;
    }

    private async Task<InfrastructureGraph?> LoadAsync(GraphRegion region, Slot slot, CancellationToken ct)
    {
        var current = slot.Graph;
        if (Bucket is { } bucket)
        {
            var key = S3Key(region);
            var origin = $"s3://{bucket}/{key}";
            try
            {
                _s3 ??= new AmazonS3Client();
                var request = new GetObjectRequest { BucketName = bucket, Key = key };
                if (current?.Origin == origin && slot.ETag is not null)
                    request.EtagToNotMatch = slot.ETag;

                using var response = await _s3.GetObjectAsync(request, ct);
                using var buffer = new MemoryStream();
                await response.ResponseStream.CopyToAsync(buffer, ct);
                var graph = InfrastructureGraph.Parse(buffer.ToArray(), origin);
                slot.ETag = response.ETag;
                if (Matches(region, graph)) return Log(region, graph);
                slot.ETag = null;
                if (current is not null) return current;
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.NotModified)
            {
                return current;
            }
            catch (Exception ex) when (ex is AmazonServiceException or AmazonClientException
                                           or InvalidDataException or HttpRequestException or IOException)
            {
                if (current is not null)
                {
                    _logger.LogWarning(ex, "Could not refresh region {Region} from {Origin}; keeping version {Version}",
                        region.Id, origin, current.Version);
                    return current;
                }
                var missing = ex is AmazonS3Exception { StatusCode: HttpStatusCode.NotFound or HttpStatusCode.Forbidden };
                if (missing && !region.Default)
                    _logger.LogInformation("Region {Region} is not published yet ({Origin})", region.Id, origin);
                else
                    _logger.LogWarning(ex, "Could not load region {Region} from {Origin}", region.Id, origin);
            }
        }

        if (LocalPath(region) is { } path)
        {
            var graph = InfrastructureGraph.Parse(await File.ReadAllBytesAsync(path, ct), path);
            if (Matches(region, graph)) return Log(region, graph);
        }

        return region.Default
            ? Log(region, InfrastructureGraph.Parse(await File.ReadAllBytesAsync(SamplePath, ct), "bundled sample"))
            : null;
    }

    private bool Matches(GraphRegion region, InfrastructureGraph g)
    {
        if (GraphRegions.Matches(region, g.Meta)) return true;
        _logger.LogError("Ignoring {Origin} for region {Region}: it was built for {Built}, the region is {Expected}",
            g.Origin, region.Id, string.Join(", ", g.Meta.Area!.Names), region.States);
        return false;
    }

    private InfrastructureGraph Log(GraphRegion region, InfrastructureGraph g)
    {
        _logger.LogInformation("Loaded region {Region} graph {Version} from {Origin}: {Nodes} nodes, {Edges} edges",
            region.Id, g.Version, g.Origin, g.Nodes.Count, g.Edges.Count);
        return g;
    }

    public void Dispose()
    {
        _s3?.Dispose();
        foreach (var slot in _slots.Values) slot.Gate.Dispose();
    }
}
