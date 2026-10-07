using System.Net;
using Amazon.Runtime;
using Amazon.S3;
using Amazon.S3.Model;

namespace Backend.Services;

public interface IInfrastructureGraphProvider
{
    Task<InfrastructureGraph> GetAsync(CancellationToken ct);
}

/// <summary>
/// SINGLETON that loads the dependency graph and keeps it in memory.
///
/// Where the data comes from, first match wins:
///   1. S3        CiGraph__S3Bucket (+ CiGraph__S3Key, default data/ci-graph.json).
///                Used by the serverless stack; the weekly pipeline workflow writes it.
///   2. A file    CiGraph__Path (local dev, or a volume mounted into the ECS task).
///   3. Built in  Data/ci-graph.sample.json - synthetic sample, so the app always works.
///
/// The graph is re-checked every CiGraph__RefreshMinutes (default 15). For S3 that is a
/// conditional GET on the ETag, so an unchanged file costs one cheap request and no parsing.
/// A failed refresh keeps serving the last good graph.
/// </summary>
public sealed class InfrastructureGraphProvider(IConfiguration config, ILogger<InfrastructureGraphProvider> logger)
    : IInfrastructureGraphProvider, IDisposable
{
    public static readonly string SamplePath = Path.Combine(AppContext.BaseDirectory, "Data", "ci-graph.sample.json");

    private readonly SemaphoreSlim _gate = new(1, 1);

    private InfrastructureGraph? _graph;
    private DateTimeOffset _checkedAt;
    private string? _s3ETag;
    private AmazonS3Client? _s3;

    private TimeSpan Refresh => TimeSpan.FromMinutes(Math.Max(1, config.GetValue("CiGraph:RefreshMinutes", 15)));

    public async Task<InfrastructureGraph> GetAsync(CancellationToken ct)
    {
        var current = _graph;
        if (current is not null && DateTimeOffset.UtcNow - _checkedAt < Refresh)
            return current;

        await _gate.WaitAsync(ct);
        try
        {
            if (_graph is not null && DateTimeOffset.UtcNow - _checkedAt < Refresh)
                return _graph;
            _graph = await LoadAsync(_graph, ct);
            _checkedAt = DateTimeOffset.UtcNow;
            return _graph;
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<InfrastructureGraph> LoadAsync(InfrastructureGraph? current, CancellationToken ct)
    {
        var bucket = config["CiGraph:S3Bucket"];
        if (!string.IsNullOrWhiteSpace(bucket))
        {
            var key = config["CiGraph:S3Key"] ?? "data/ci-graph.json";
            var origin = $"s3://{bucket}/{key}";
            try
            {
                _s3 ??= new AmazonS3Client();
                var request = new GetObjectRequest { BucketName = bucket, Key = key };
                if (current?.Origin == origin && _s3ETag is not null)
                    request.EtagToNotMatch = _s3ETag;

                using var response = await _s3.GetObjectAsync(request, ct);
                using var buffer = new MemoryStream();
                await response.ResponseStream.CopyToAsync(buffer, ct);
                var graph = InfrastructureGraph.Parse(buffer.ToArray(), origin);
                _s3ETag = response.ETag;
                Log(graph);
                return graph;
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.NotModified)
            {
                return current!;
            }
            catch (Exception ex) when (ex is AmazonServiceException or AmazonClientException
                                           or InvalidDataException or HttpRequestException or IOException)
            {
                if (current is not null)
                {
                    logger.LogWarning(ex, "Could not refresh the graph from {Origin}; keeping version {Version}",
                        origin, current.Version);
                    return current;
                }
                logger.LogWarning(ex, "Could not load the graph from {Origin}; using the bundled sample", origin);
            }
        }

        var path = config["CiGraph:Path"];
        if (!string.IsNullOrWhiteSpace(path) && File.Exists(path))
            return Log(InfrastructureGraph.Parse(await File.ReadAllBytesAsync(path, ct), path));

        return Log(InfrastructureGraph.Parse(await File.ReadAllBytesAsync(SamplePath, ct), "bundled sample"));
    }

    private InfrastructureGraph Log(InfrastructureGraph g)
    {
        logger.LogInformation("Loaded infrastructure graph {Version} from {Origin}: {Nodes} nodes, {Edges} edges",
            g.Version, g.Origin, g.Nodes.Count, g.Edges.Count);
        return g;
    }

    public void Dispose()
    {
        _s3?.Dispose();
        _gate.Dispose();
    }
}
