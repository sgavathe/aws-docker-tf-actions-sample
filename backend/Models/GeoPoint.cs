using System.Text.Json.Serialization;

namespace Backend.Models;

/// <summary>
/// VALUE TYPE (readonly record struct): copied by value, lives inline (no heap
/// allocation per point), immutable, and gets value equality for free.
/// Good fit for small, frequently created data like coordinates.
/// </summary>
public readonly record struct GeoPoint(double Lat, double Lon)
{
    [JsonIgnore]
    public bool IsValid => Lat is >= -90 and <= 90 && Lon is >= -180 and <= 180;
}

public static class GeoMath
{
    private const double EarthRadiusNm = 3440.065; // mean Earth radius in nautical miles

    /// <summary>Great-circle distance (Haversine) in nautical miles.</summary>
    /// <remarks>
    /// In SQL Server you'd push this to the database instead:
    ///   WHERE Location.STDistance(@point) &lt;= @radiusMeters   (geography type)
    /// </remarks>
    public static double DistanceNm(GeoPoint a, GeoPoint b)
    {
        static double Rad(double deg) => deg * Math.PI / 180.0;

        var dLat = Rad(b.Lat - a.Lat);
        var dLon = Rad(b.Lon - a.Lon);
        var h = Math.Pow(Math.Sin(dLat / 2), 2) +
                Math.Cos(Rad(a.Lat)) * Math.Cos(Rad(b.Lat)) * Math.Pow(Math.Sin(dLon / 2), 2);
        return 2 * EarthRadiusNm * Math.Asin(Math.Sqrt(h));
    }

    /// <summary>
    /// Cheap bounding box around a point. Used as a coarse pre-filter (an index-friendly
    /// "WHERE lat BETWEEN ... AND lon BETWEEN ..." in a real database) before the
    /// exact distance check.
    /// </summary>
    public static (double MinLat, double MaxLat, double MinLon, double MaxLon) BoundingBox(GeoPoint c, double radiusNm)
    {
        var dLat = radiusNm / 60.0; // 1 degree of latitude ~= 60 nm
        var dLon = radiusNm / (60.0 * Math.Max(Math.Cos(c.Lat * Math.PI / 180.0), 0.01));
        return (c.Lat - dLat, c.Lat + dLat, c.Lon - dLon, c.Lon + dLon);
    }
}
