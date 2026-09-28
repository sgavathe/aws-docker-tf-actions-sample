using Backend.Data;
using Backend.Models;
using Backend.Services;

namespace Backend.Endpoints;

/// <summary>
/// MINIMAL API endpoints, grouped with an extension method so Program.cs stays small.
/// Parameters are bound automatically: route values, query strings, and DI services.
/// </summary>
public static class IncidentEndpoints
{
    public static IEndpointRouteBuilder MapIncidentEndpoints(this IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("/api").WithTags("Maritime incidents");

        api.MapGet("/ports", () => Results.Ok(Ports.All));

        // GET /api/incidents?type=Pollution  -> GeoJSON FeatureCollection
        api.MapGet("/incidents", (string? type, IIncidentService service) =>
        {
            if (!TryParseType(type, out var parsed))
                return InvalidType();
            return Results.Ok(GeoJson.ToCollection(service.List(parsed)));
        });

        // GET /api/incidents/42   (":int" route constraint -> 404 for non-numbers)
        api.MapGet("/incidents/{id:int}", async (int id, IIncidentRepository repo, CancellationToken ct) =>
        {
            var incident = await repo.FindAsync(id, ct);
            return incident is null ? Results.NotFound() : Results.Ok(GeoJson.ToFeature(incident));
        });

        // GET /api/incidents/nearby?lat=36.95&lon=-76.33&radiusNm=15&type=Fire
        api.MapGet("/incidents/nearby", (
            double lat, double lon, double? radiusNm, string? type,
            QueryValidator validator, IIncidentService service) =>
        {
            var radius = radiusNm ?? 15;
            var errors = validator.ValidateNearby(lat, lon, radius);
            if (errors.Count > 0)
                return Results.ValidationProblem(errors); // 400 with ProblemDetails body
            if (!TryParseType(type, out var parsed))
                return InvalidType();

            var hits = service.Nearby(new GeoPoint(lat, lon), radius, parsed);
            return Results.Ok(new
            {
                center = new GeoPoint(lat, lon),
                radiusNm = radius,
                count = hits.Count,
                results = GeoJson.ToCollection(hits)
            });
        });

        // GET /api/weather?lat=36.95&lon=-76.33   (async I/O to an external API)
        api.MapGet("/weather", async (double lat, double lon, WeatherClient weather, CancellationToken ct) =>
        {
            var forecast = await weather.GetForecastAsync(new GeoPoint(lat, lon), ct);
            return forecast is null
                ? Results.Problem("Weather service is unavailable for this location.", statusCode: 503)
                : Results.Ok(forecast);
        });

        return app;
    }

    private static bool TryParseType(string? value, out IncidentType? type)
    {
        type = null;
        if (string.IsNullOrWhiteSpace(value)) return true;
        if (Enum.TryParse<IncidentType>(value, ignoreCase: true, out var t)) { type = t; return true; }
        return false;
    }

    private static IResult InvalidType() => Results.ValidationProblem(new Dictionary<string, string[]>
    {
        ["type"] = [$"Unknown incident type. Use one of: {string.Join(", ", Enum.GetNames<IncidentType>())}."]
    });
}
