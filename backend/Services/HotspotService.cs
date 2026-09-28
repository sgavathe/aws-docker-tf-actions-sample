using System.Collections.Concurrent;
using Backend.Data;
using Backend.Models;
using Microsoft.ML;
using Microsoft.ML.Data;

namespace Backend.Services;

public interface IHotspotService
{
    IReadOnlyList<Hotspot> GetHotspots(int clusters);
}

/// <summary>
/// AI/ML piece: unsupervised K-Means clustering (ML.NET) over incident locations to
/// find hotspots, then a simple risk score = incident count x average severity.
///
/// SINGLETON: training is relatively expensive, so results are cached per k and
/// shared across requests. MLContext training isn't thread-safe, hence the lock.
/// </summary>
public sealed class HotspotService(IIncidentRepository repo, ILogger<HotspotService> logger) : IHotspotService
{
    private readonly ConcurrentDictionary<int, IReadOnlyList<Hotspot>> _cache = new();
    private readonly object _trainLock = new();

    public IReadOnlyList<Hotspot> GetHotspots(int clusters) =>
        _cache.GetOrAdd(clusters, k =>
        {
            lock (_trainLock) { return Train(k); }
        });

    private IReadOnlyList<Hotspot> Train(int k)
    {
        var incidents = repo.Query().ToList();
        var ml = new MLContext(seed: 1); // seeded => reproducible clusters

        var data = ml.Data.LoadFromEnumerable(
            incidents.Select(i => new ClusterInput { Lat = (float)i.Location.Lat, Lon = (float)i.Location.Lon }));

        // Pipeline: combine columns into a feature vector, then train K-Means.
        // (Raw lat/lon is fine for a demo; for production you'd project to a planar
        // CRS or use haversine-aware clustering such as DBSCAN.)
        var pipeline = ml.Transforms
            .Concatenate("Features", nameof(ClusterInput.Lat), nameof(ClusterInput.Lon))
            .Append(ml.Clustering.Trainers.KMeans("Features", numberOfClusters: k));

        var model = pipeline.Fit(data);
        var predictions = ml.Data
            .CreateEnumerable<ClusterOutput>(model.Transform(data), reuseRowObject: false)
            .ToList();

        var hotspots = incidents
            .Zip(predictions, (incident, p) => (incident, cluster: (int)p.ClusterId))
            .GroupBy(x => x.cluster)
            .Select(g =>
            {
                var members = g.Select(x => x.incident).ToList();
                var center = new GeoPoint(members.Average(m => m.Location.Lat), members.Average(m => m.Location.Lon));
                var avgSeverity = members.Average(m => m.Severity);
                return new Hotspot(
                    ClusterId: g.Key,
                    Center: center,
                    IncidentCount: members.Count,
                    AvgSeverity: avgSeverity,
                    DominantType: members.GroupBy(m => m.Type).MaxBy(t => t.Count())!.Key,
                    RadiusNm: members.Max(m => GeoMath.DistanceNm(center, m.Location)),
                    RiskScore: members.Count * avgSeverity);
            })
            .OrderByDescending(h => h.RiskScore)
            .ToList();

        logger.LogInformation("Trained K-Means with k={K} on {N} incidents", k, incidents.Count);
        return hotspots;
    }

    // Public nested row types: ML.NET creates/reads them via reflection.
    public sealed class ClusterInput
    {
        public float Lat { get; set; }
        public float Lon { get; set; }
    }

    public sealed class ClusterOutput
    {
        [ColumnName("PredictedLabel")]
        public uint ClusterId { get; set; }
    }
}
