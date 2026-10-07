using System.Text.Json;
using Backend.Services;
using Xunit;

namespace Backend.Tests;

public class AreaPolygonTests
{
    private static (bool Ok, AreaPolygon? Area, string Error) Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var ok = AreaPolygon.TryParse(doc.RootElement.Clone(), out var area, out var error);
        return (ok, area, error);
    }

    private const string Square = """{ "type": "Polygon", "coordinates": [[[-77.5, 37.5], [-77.4, 37.5], [-77.4, 37.6], [-77.5, 37.6], [-77.5, 37.5]]] }""";

    [Fact]
    public void Polygon_ContainsInsideNotOutside()
    {
        var (ok, area, _) = Parse(Square);
        Assert.True(ok);
        Assert.True(area!.Contains(-77.45, 37.55));
        Assert.False(area.Contains(-77.35, 37.55));
        Assert.InRange(area.AreaKm2, 95, 99); // 0.1 deg x 0.1 deg at 37.5 N ~ 97 km2
    }

    [Fact]
    public void Hole_IsNotInside()
    {
        var (ok, area, _) = Parse("""
            { "type": "Polygon", "coordinates": [
              [[-77.5, 37.5], [-77.4, 37.5], [-77.4, 37.6], [-77.5, 37.6], [-77.5, 37.5]],
              [[-77.46, 37.54], [-77.44, 37.54], [-77.44, 37.56], [-77.46, 37.56], [-77.46, 37.54]] ] }
            """);
        Assert.True(ok);
        Assert.False(area!.Contains(-77.45, 37.55));
        Assert.True(area.Contains(-77.48, 37.52));
    }

    [Fact]
    public void Feature_And_MultiPolygon_AreAccepted()
    {
        var (ok1, a1, _) = Parse($$"""{ "type": "Feature", "properties": {}, "geometry": {{Square}} }""");
        Assert.True(ok1);
        Assert.True(a1!.Contains(-77.45, 37.55));

        var (ok2, a2, _) = Parse("""
            { "type": "MultiPolygon", "coordinates": [
              [[[-77.5, 37.5], [-77.4, 37.5], [-77.4, 37.6], [-77.5, 37.5]]],
              [[[-76.5, 37.5], [-76.4, 37.5], [-76.4, 37.6], [-76.5, 37.5]]] ] }
            """);
        Assert.True(ok2);
        Assert.True(a2!.Contains(-76.41, 37.52));
    }

    [Theory]
    [InlineData("""{ "type": "LineString", "coordinates": [[-77.5, 37.5], [-77.4, 37.5]] }""")]
    [InlineData("""{ "type": "Polygon", "coordinates": [[[-77.5, 37.5], [-77.4, 37.5], [-77.5, 37.5]]] }""")]
    [InlineData("""{ "type": "Polygon", "coordinates": [[[-277.5, 37.5], [-77.4, 37.5], [-77.4, 37.6], [-277.5, 37.5]]] }""")]
    [InlineData("""{ "type": "Polygon", "coordinates": [[["a", 37.5], [-77.4, 37.5], [-77.4, 37.6]]] }""")]
    [InlineData("""{ "type": "Polygon", "coordinates": [[[-77.5, 37.5], [-77.4, 37.5], [-77.3, 37.5], [-77.5, 37.5]]] }""")]
    [InlineData("""[1, 2, 3]""")]
    public void BadShapes_AreRejectedWithAMessage(string json)
    {
        var (ok, area, error) = Parse(json);
        Assert.False(ok);
        Assert.Null(area);
        Assert.False(string.IsNullOrWhiteSpace(error));
    }

    [Fact]
    public void Segment_CrossingTheArea_Intersects()
    {
        var (_, area, _) = Parse(Square);
        Assert.True(area!.Intersects(-77.6, 37.55, -77.3, 37.55));   // both ends outside, crosses
        Assert.False(area.Intersects(-77.6, 37.65, -77.3, 37.65));   // passes north of it
    }

    [Fact]
    public void Distance_IsZeroInsideAndKilometresOutside()
    {
        var (_, area, _) = Parse(Square);
        Assert.Equal(0, area!.DistanceKm(-77.45, 37.55));
        Assert.InRange(area.DistanceKm(-77.3, 37.55), 8.6, 9.0);      // 0.1 deg of longitude at 37.55 N
    }
}
