using System.Text.Json.Serialization;

namespace Backend.Models;

// Minimal GeoJSON (RFC 7946) shapes. Returning a standard format means the output
// can be consumed directly by an ArcGIS GeoJSONLayer, QGIS, Leaflet, etc.

public sealed record PointGeometry(double[] Coordinates)
{
    [JsonPropertyOrder(-1)] public string Type => "Point";
}

public sealed record Feature(PointGeometry Geometry, object Properties)
{
    [JsonPropertyOrder(-1)] public string Type => "Feature";
}

public sealed record FeatureCollection(IReadOnlyList<Feature> Features)
{
    [JsonPropertyOrder(-1)] public string Type => "FeatureCollection";
}

public static class GeoJson
{
    // GeoJSON order is [longitude, latitude] -- a classic source of bugs.
    public static PointGeometry Point(GeoPoint p) => new([p.Lon, p.Lat]);

    public static Feature ToFeature(Incident i, double? distanceNm = null) => new(
        Point(i.Location),
        new
        {
            i.Id,
            type = i.Type.ToString(),
            i.Severity,
            i.PortId,
            i.VesselName,
            i.Summary,
            reportedUtc = i.ReportedUtc,
            distanceNm = distanceNm is null ? (double?)null : Math.Round(distanceNm.Value, 2)
        });

    public static FeatureCollection ToCollection(IEnumerable<Incident> items) =>
        new(items.Select(i => ToFeature(i)).ToList());

    public static FeatureCollection ToCollection(IEnumerable<IncidentHit> hits) =>
        new(hits.Select(h => ToFeature(h.Incident, h.DistanceNm)).ToList());

    public static FeatureCollection ToCollection(IEnumerable<Hotspot> hotspots) =>
        new(hotspots.Select(h => new Feature(
            Point(h.Center),
            new
            {
                h.ClusterId,
                h.IncidentCount,
                avgSeverity = Math.Round(h.AvgSeverity, 2),
                dominantType = h.DominantType.ToString(),
                radiusNm = Math.Round(h.RadiusNm, 1),
                riskScore = Math.Round(h.RiskScore, 1)
            })).ToList());
}
