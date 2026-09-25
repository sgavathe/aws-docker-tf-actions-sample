using Backend.Data;
using Backend.Services;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Backend.Tests;

public class HotspotServiceTests
{
    [Fact]
    public void KMeans_AssignsEveryIncidentToAtMostKClusters()
    {
        var repo = new InMemoryIncidentRepository();
        var service = new HotspotService(repo, NullLogger<HotspotService>.Instance);

        var hotspots = service.GetHotspots(8);

        Assert.InRange(hotspots.Count, 1, 8);
        Assert.Equal(repo.Query().Count(), hotspots.Sum(h => h.IncidentCount));
        Assert.Equal(hotspots.OrderByDescending(h => h.RiskScore), hotspots); // highest risk first
    }

    [Fact]
    public void Results_AreCachedPerK()
    {
        var service = new HotspotService(new InMemoryIncidentRepository(), NullLogger<HotspotService>.Instance);
        Assert.Same(service.GetHotspots(5), service.GetHotspots(5));
    }
}
