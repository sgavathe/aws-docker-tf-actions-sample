using Backend.Data;
using Backend.Models;
using Backend.Services;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Backend.Tests;

public class IncidentServiceTests
{
    private readonly InMemoryIncidentRepository _repo = new();

    private IncidentService CreateService() =>
        new(_repo, new RequestContext { CorrelationId = "test" }, NullLogger<IncidentService>.Instance);

    [Fact]
    public void Nearby_ReturnsOnlyHitsInsideRadius_SortedByDistance()
    {
        var hits = CreateService().Nearby(new GeoPoint(36.95, -76.33), 15, type: null);

        Assert.NotEmpty(hits);
        Assert.All(hits, h => Assert.True(h.DistanceNm <= 15));
        Assert.Equal(hits.OrderBy(h => h.DistanceNm).Select(h => h.Incident.Id),
                     hits.Select(h => h.Incident.Id));
    }

    [Fact]
    public void Nearby_MatchesBruteForce()
    {
        // The bounding-box pre-filter must never drop a real hit.
        var center = new GeoPoint(29.94, -90.06);
        var expected = _repo.Query().AsEnumerable()
            .Count(i => GeoMath.DistanceNm(center, i.Location) <= 20);

        Assert.Equal(expected, CreateService().Nearby(center, 20, null).Count);
    }

    [Fact]
    public void List_FiltersByType()
    {
        var fires = CreateService().List(IncidentType.Fire);
        Assert.All(fires, i => Assert.Equal(IncidentType.Fire, i.Type));
    }

    [Fact]
    public void Validator_RejectsBadInput()
    {
        var errors = new QueryValidator().ValidateNearby(lat: 95, lon: 0, radiusNm: 0);
        Assert.Contains("lat", errors.Keys);
        Assert.Contains("radiusNm", errors.Keys);
    }
}
