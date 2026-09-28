using Backend.Data;
using Backend.Models;

namespace Backend.Services;

public interface IIncidentService
{
    IReadOnlyList<Incident> List(IncidentType? type);
    IReadOnlyList<IncidentHit> Nearby(GeoPoint center, double radiusNm, IncidentType? type);
}

/// <summary>
/// SCOPED: one per request. It depends on RequestContext (scoped) and the repository
/// (singleton). A scoped service may depend on singletons, but a singleton must NOT
/// depend on a scoped service (a "captive dependency").
/// </summary>
public sealed class IncidentService(
    IIncidentRepository repo,
    RequestContext requestContext,
    ILogger<IncidentService> logger) : IIncidentService   // C# 12 primary constructor
{
    public IReadOnlyList<Incident> List(IncidentType? type)
    {
        IQueryable<Incident> query = repo.Query();
        if (type is not null)
            query = query.Where(i => i.Type == type);

        return query.OrderBy(i => i.Id).ToList(); // ToList() = execute now (materialize)
    }

    public IReadOnlyList<IncidentHit> Nearby(GeoPoint center, double radiusNm, IncidentType? type)
    {
        var (minLat, maxLat, minLon, maxLon) = GeoMath.BoundingBox(center, radiusNm);

        // Step 1 -- IQueryable: these filters are composable and, with EF Core, become a
        // SQL WHERE clause that can use an index. Nothing has executed yet (deferred).
        IQueryable<Incident> query = repo.Query()
            .Where(i => i.Location.Lat >= minLat && i.Location.Lat <= maxLat &&
                        i.Location.Lon >= minLon && i.Location.Lon <= maxLon);
        if (type is not null)
            query = query.Where(i => i.Type == type);

        // Step 2 -- AsEnumerable(): switch to in-memory LINQ (IEnumerable). The exact
        // Haversine check can't be translated to SQL, so it runs on the small
        // candidate set only.
        var hits = query
            .AsEnumerable()
            .Select(i => new IncidentHit(i, GeoMath.DistanceNm(center, i.Location)))
            .Where(h => h.DistanceNm <= radiusNm)
            .OrderBy(h => h.DistanceNm)
            .ToList();

        // Structured logging: named placeholders become searchable fields in
        // CloudWatch Logs Insights, not just a formatted string.
        logger.LogInformation(
            "Nearby search {CorrelationId}: {Count} hits within {RadiusNm} nm of {Lat},{Lon}",
            requestContext.CorrelationId, hits.Count, radiusNm, center.Lat, center.Lon);

        return hits;
    }
}
