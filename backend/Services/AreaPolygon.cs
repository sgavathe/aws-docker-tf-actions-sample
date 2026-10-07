using System.Text.Json;

namespace Backend.Services;

/// <summary>
/// The area a user draws on the map (GeoJSON Polygon, MultiPolygon or a Feature
/// holding one), projected once to a local planar grid in kilometres.
///
/// An equirectangular projection around the area's own latitude is accurate to well
/// under 1% across a metro region, which is plenty for "is this asset inside the
/// drawn area" and "how far away did the outage reach". No GIS library needed.
/// </summary>
public sealed class AreaPolygon
{
    public const int MaxVertices = 5000;
    private const double KmPerDegLat = 110.574;

    private readonly double _kmPerDegLon;
    // polygons -> rings (first = outer boundary, rest = holes) -> vertex arrays in km
    private readonly List<List<(double[] X, double[] Y)>> _polygons = [];
    private readonly double _minX, _minY, _maxX, _maxY;

    public double AreaKm2 { get; }

    private AreaPolygon(List<List<List<(double Lon, double Lat)>>> polygons)
    {
        var lats = polygons.SelectMany(p => p[0]).Select(v => v.Lat).ToList();
        var lat0 = (lats.Min() + lats.Max()) / 2;
        _kmPerDegLon = 111.320 * Math.Cos(lat0 * Math.PI / 180.0);

        _minX = _minY = double.MaxValue;
        _maxX = _maxY = double.MinValue;
        double area = 0;
        foreach (var poly in polygons)
        {
            var rings = new List<(double[] X, double[] Y)>();
            for (var r = 0; r < poly.Count; r++)
            {
                var xs = poly[r].Select(v => v.Lon * _kmPerDegLon).ToArray();
                var ys = poly[r].Select(v => v.Lat * KmPerDegLat).ToArray();
                rings.Add((xs, ys));
                var a = Math.Abs(Shoelace(xs, ys));
                area += r == 0 ? a : -a;
                if (r != 0) continue;
                _minX = Math.Min(_minX, xs.Min());
                _maxX = Math.Max(_maxX, xs.Max());
                _minY = Math.Min(_minY, ys.Min());
                _maxY = Math.Max(_maxY, ys.Max());
            }
            _polygons.Add(rings);
        }
        AreaKm2 = Math.Max(0, area);
    }

    // ------------------------------------------------------------------ queries

    public bool Contains(double lon, double lat) => ContainsKm(lon * _kmPerDegLon, lat * KmPerDegLat);

    /// <summary>True if the segment touches the area (an end inside, or crossing an edge).</summary>
    public bool Intersects(double lon1, double lat1, double lon2, double lat2)
    {
        double ax = lon1 * _kmPerDegLon, ay = lat1 * KmPerDegLat;
        double bx = lon2 * _kmPerDegLon, by = lat2 * KmPerDegLat;
        if (Math.Max(ax, bx) < _minX || Math.Min(ax, bx) > _maxX ||
            Math.Max(ay, by) < _minY || Math.Min(ay, by) > _maxY)
            return false;
        if (ContainsKm(ax, ay) || ContainsKm(bx, by))
            return true;

        foreach (var poly in _polygons)
            foreach (var (xs, ys) in poly)
                for (int i = 0, j = xs.Length - 1; i < xs.Length; j = i++)
                    if (SegmentsCross(ax, ay, bx, by, xs[j], ys[j], xs[i], ys[i]))
                        return true;
        return false;
    }

    /// <summary>Distance from a point to the area in km (0 when inside).</summary>
    public double DistanceKm(double lon, double lat)
    {
        double px = lon * _kmPerDegLon, py = lat * KmPerDegLat;
        if (ContainsKm(px, py)) return 0;

        var best = double.MaxValue;
        foreach (var poly in _polygons)
            foreach (var (xs, ys) in poly)
                for (int i = 0, j = xs.Length - 1; i < xs.Length; j = i++)
                    best = Math.Min(best, PointSegmentDistance(px, py, xs[j], ys[j], xs[i], ys[i]));
        return best;
    }

    private bool ContainsKm(double x, double y)
    {
        if (x < _minX || x > _maxX || y < _minY || y > _maxY) return false;
        foreach (var poly in _polygons)
        {
            if (!InRing(poly[0], x, y)) continue;
            var inHole = false;
            for (var h = 1; h < poly.Count && !inHole; h++)
                inHole = InRing(poly[h], x, y);
            if (!inHole) return true;
        }
        return false;
    }

    // ------------------------------------------------------------------ geometry helpers

    private static bool InRing((double[] X, double[] Y) ring, double x, double y)
    {
        var (xs, ys) = ring;
        var inside = false;
        for (int i = 0, j = xs.Length - 1; i < xs.Length; j = i++)
        {
            if ((ys[i] > y) != (ys[j] > y) &&
                x < (xs[j] - xs[i]) * (y - ys[i]) / (ys[j] - ys[i]) + xs[i])
                inside = !inside;
        }
        return inside;
    }

    private static double Cross(double ax, double ay, double bx, double by, double cx, double cy) =>
        (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);

    private static bool OnSegment(double ax, double ay, double bx, double by, double px, double py) =>
        Math.Min(ax, bx) <= px && px <= Math.Max(ax, bx) && Math.Min(ay, by) <= py && py <= Math.Max(ay, by);

    private static bool SegmentsCross(double ax, double ay, double bx, double by,
                                      double cx, double cy, double dx, double dy)
    {
        var d1 = Cross(cx, cy, dx, dy, ax, ay);
        var d2 = Cross(cx, cy, dx, dy, bx, by);
        var d3 = Cross(ax, ay, bx, by, cx, cy);
        var d4 = Cross(ax, ay, bx, by, dx, dy);
        if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0)))
            return true;
        return (d1 == 0 && OnSegment(cx, cy, dx, dy, ax, ay)) ||
               (d2 == 0 && OnSegment(cx, cy, dx, dy, bx, by)) ||
               (d3 == 0 && OnSegment(ax, ay, bx, by, cx, cy)) ||
               (d4 == 0 && OnSegment(ax, ay, bx, by, dx, dy));
    }

    private static double PointSegmentDistance(double px, double py, double ax, double ay, double bx, double by)
    {
        double vx = bx - ax, vy = by - ay;
        var len2 = vx * vx + vy * vy;
        var t = len2 == 0 ? 0 : Math.Clamp(((px - ax) * vx + (py - ay) * vy) / len2, 0, 1);
        double qx = ax + t * vx - px, qy = ay + t * vy - py;
        return Math.Sqrt(qx * qx + qy * qy);
    }

    private static double Shoelace(double[] xs, double[] ys)
    {
        double s = 0;
        for (int i = 0, j = xs.Length - 1; i < xs.Length; j = i++)
            s += (xs[j] + xs[i]) * (ys[j] - ys[i]);
        return s / 2;
    }

    // ------------------------------------------------------------------ GeoJSON parsing

    /// <summary>Accepts a Polygon, MultiPolygon, or a Feature holding one.</summary>
    public static bool TryParse(JsonElement geometry, out AreaPolygon? area, out string error)
    {
        area = null;
        error = "";
        try
        {
            if (geometry.ValueKind != JsonValueKind.Object || !geometry.TryGetProperty("type", out var typeEl))
            {
                error = "Send a GeoJSON Polygon or MultiPolygon.";
                return false;
            }

            var type = typeEl.GetString();
            if (type == "Feature")
            {
                if (!geometry.TryGetProperty("geometry", out var inner))
                {
                    error = "The Feature has no geometry.";
                    return false;
                }
                return TryParse(inner, out area, out error);
            }

            if (!geometry.TryGetProperty("coordinates", out var coords) || coords.ValueKind != JsonValueKind.Array)
            {
                error = "The geometry has no coordinates.";
                return false;
            }

            var polygons = new List<List<List<(double Lon, double Lat)>>>();
            if (type == "Polygon")
                polygons.Add(ReadPolygon(coords));
            else if (type == "MultiPolygon")
                polygons.AddRange(coords.EnumerateArray().Select(ReadPolygon));
            else
            {
                error = $"Geometry type '{type}' isn't supported. Draw a polygon.";
                return false;
            }

            if (polygons.Count == 0)
            {
                error = "The polygon is empty.";
                return false;
            }
            var vertices = polygons.Sum(p => p.Sum(r => r.Count));
            if (vertices > MaxVertices)
            {
                error = $"The area has {vertices} vertices; the limit is {MaxVertices}. Simplify the shape.";
                return false;
            }

            area = new AreaPolygon(polygons);
            if (area.AreaKm2 <= 0)
            {
                area = null;
                error = "The polygon has no area. Draw at least three distinct corners.";
                return false;
            }
            return true;
        }
        catch (FormatException ex)
        {
            error = ex.Message;
            return false;
        }
        catch (InvalidOperationException)
        {
            error = "Coordinates must be [longitude, latitude] number pairs.";
            return false;
        }
    }

    private static List<List<(double Lon, double Lat)>> ReadPolygon(JsonElement rings)
    {
        var result = new List<List<(double Lon, double Lat)>>();
        foreach (var ringEl in rings.EnumerateArray())
        {
            var ring = new List<(double Lon, double Lat)>();
            foreach (var pos in ringEl.EnumerateArray())
            {
                if (pos.GetArrayLength() < 2)
                    throw new FormatException("Each position needs a longitude and a latitude.");
                var lon = pos[0].GetDouble();
                var lat = pos[1].GetDouble();
                if (!double.IsFinite(lon) || !double.IsFinite(lat) || lon is < -180 or > 180 || lat is < -90 or > 90)
                    throw new FormatException("Coordinates must be [longitude, latitude] within -180..180 and -90..90.");
                ring.Add((lon, lat));
            }
            // GeoJSON rings repeat the first vertex at the end; drop it, the math closes rings itself.
            if (ring.Count > 1 && ring[0] == ring[^1])
                ring.RemoveAt(ring.Count - 1);
            if (ring.Count < 3)
                throw new FormatException("Each polygon ring needs at least three corners.");
            result.Add(ring);
        }
        if (result.Count == 0)
            throw new FormatException("The polygon has no rings.");
        return result;
    }
}
