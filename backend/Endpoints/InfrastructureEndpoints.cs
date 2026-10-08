using System.Text.Json;
using Backend.Services;
using Microsoft.Net.Http.Headers;

namespace Backend.Endpoints;

/// <summary>POST body for /api/infrastructure/impact: the drawn area as GeoJSON.</summary>
public sealed record ImpactRequest(JsonElement Area);

/// <summary>
/// Critical-infrastructure dependency graph + "what breaks if this area goes down".
/// Every endpoint takes an optional ?region= (Data/regions.json); without it, the default region.
/// </summary>
public static class InfrastructureEndpoints
{
    public static IEndpointRouteBuilder MapInfrastructureEndpoints(this IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("/api/infrastructure").WithTags("Critical infrastructure");

        // GET /api/infrastructure/regions  -> the study regions and which are published
        api.MapGet("/regions", async (IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            var regions = provider.Regions.All;
            var available = await Task.WhenAll(regions.Select(r => provider.IsAvailableAsync(r, ct)));
            return Results.Ok(regions.Select((r, i) => new
            {
                id = r.Id,
                label = r.Label,
                name = r.Name,
                @default = r.Default,
                available = available[i]
            }));
        });

        // GET /api/infrastructure  -> what data is loaded (never exposes bucket names or paths)
        api.MapGet("", async (string? region, IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            var (g, problem) = await Resolve(provider, region, ct);
            if (g is null) return problem!;
            return Results.Ok(new
            {
                g.Meta,
                version = g.Version,
                dataSource = g.Origin.StartsWith("s3://", StringComparison.Ordinal) ? "published"
                           : g.Origin == "bundled sample" ? "bundled sample" : "file",
                nodes = g.Nodes.Count,
                edges = g.Edges.Count
            });
        });

        // GET /api/infrastructure/graph  -> the whole graph for the map (ETag + gzip/brotli)
        api.MapGet("/graph", async (string? region, HttpContext http, IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            var (g, problem) = await Resolve(provider, region, ct);
            if (g is null) return problem!;
            http.Response.Headers.CacheControl = "public, max-age=300";
            // With an entity tag, the file result answers If-None-Match with 304 by itself.
            return Results.Bytes(g.RawJson, "application/json", entityTag: new EntityTagHeaderValue(g.ETag));
        });

        // POST /api/infrastructure/impact?region=  { "area": <GeoJSON Polygon | MultiPolygon | Feature> }
        //
        // Through CloudFront -> Lambda Function URL (OAC), POST requests must carry an
        // x-amz-content-sha256 header with the SHA-256 of the body. The frontend adds it.
        api.MapPost("/impact", async (string? region, ImpactRequest? body, IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            if (body is null || body.Area.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null)
                return AreaError("Send the drawn area as GeoJSON in the \"area\" field.");
            if (!AreaPolygon.TryParse(body.Area, out var area, out var error) || area is null)
                return AreaError(error);

            var (g, problem) = await Resolve(provider, region, ct);
            if (g is null) return problem!;
            return Results.Ok(ImpactAnalyzer.Analyze(g, area));
        });

        return app;
    }

    private static async Task<(InfrastructureGraph? Graph, IResult? Problem)> Resolve(
        IInfrastructureGraphProvider provider, string? regionId, CancellationToken ct)
    {
        var region = provider.Regions.Find(regionId);
        if (region is null)
        {
            var known = string.Join(", ", provider.Regions.All.Select(r => r.Id));
            return (null, Results.Problem(statusCode: StatusCodes.Status404NotFound, title: "Unknown region",
                detail: $"There is no region \"{Truncate(regionId)}\". Regions: {known}."));
        }
        var graph = await provider.GetAsync(region, ct);
        if (graph is not null) return (graph, null);
        return (null, Results.Problem(statusCode: StatusCodes.Status404NotFound, title: "Region not published",
            detail: $"The {region.Label} data hasn't been built yet."));
    }

    private static string Truncate(string? s) => s is null ? "" : s.Length > 40 ? s[..40] : s;

    private static IResult AreaError(string message) =>
        Results.ValidationProblem(new Dictionary<string, string[]> { ["area"] = [message] });
}
