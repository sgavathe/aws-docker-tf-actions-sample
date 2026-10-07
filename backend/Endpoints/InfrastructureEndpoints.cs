using System.Text.Json;
using Backend.Services;
using Microsoft.Net.Http.Headers;

namespace Backend.Endpoints;

/// <summary>POST body for /api/infrastructure/impact: the drawn area as GeoJSON.</summary>
public sealed record ImpactRequest(JsonElement Area);

/// <summary>
/// Critical-infrastructure dependency graph + "what breaks if this area goes down".
/// </summary>
public static class InfrastructureEndpoints
{
    public static IEndpointRouteBuilder MapInfrastructureEndpoints(this IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("/api/infrastructure").WithTags("Critical infrastructure");

        // GET /api/infrastructure  -> what data is loaded (never exposes bucket names or paths)
        api.MapGet("", async (IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            var g = await provider.GetAsync(ct);
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
        api.MapGet("/graph", async (HttpContext http, IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            var g = await provider.GetAsync(ct);
            http.Response.Headers.CacheControl = "public, max-age=300";
            // With an entity tag, the file result answers If-None-Match with 304 by itself.
            return Results.Bytes(g.RawJson, "application/json", entityTag: new EntityTagHeaderValue(g.ETag));
        });

        // POST /api/infrastructure/impact  { "area": <GeoJSON Polygon | MultiPolygon | Feature> }
        //
        // Through CloudFront -> Lambda Function URL (OAC), POST requests must carry an
        // x-amz-content-sha256 header with the SHA-256 of the body. The frontend adds it.
        api.MapPost("/impact", async (ImpactRequest? body, IInfrastructureGraphProvider provider, CancellationToken ct) =>
        {
            if (body is null || body.Area.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null)
                return AreaError("Send the drawn area as GeoJSON in the \"area\" field.");
            if (!AreaPolygon.TryParse(body.Area, out var area, out var error) || area is null)
                return AreaError(error);

            var g = await provider.GetAsync(ct);
            return Results.Ok(ImpactAnalyzer.Analyze(g, area));
        });

        return app;
    }

    private static IResult AreaError(string message) =>
        Results.ValidationProblem(new Dictionary<string, string[]> { ["area"] = [message] });
}
