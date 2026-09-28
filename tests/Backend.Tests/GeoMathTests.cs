using Backend.Models;
using Xunit;

namespace Backend.Tests;

public class GeoMathTests
{
    [Fact]
    public void OneDegreeOfLatitude_IsAboutSixtyNauticalMiles()
    {
        var d = GeoMath.DistanceNm(new GeoPoint(0, 0), new GeoPoint(1, 0));
        Assert.InRange(d, 59.9, 60.1);
    }

    [Fact]
    public void HamptonRoadsToBaltimore_IsAbout139Nm()
    {
        var d = GeoMath.DistanceNm(new GeoPoint(36.95, -76.33), new GeoPoint(39.26, -76.58));
        Assert.InRange(d, 137, 141);
    }

    [Theory]
    [InlineData(10)]
    [InlineData(50)]
    public void BoundingBox_ContainsPointsAtTheRadius(double radiusNm)
    {
        var center = new GeoPoint(36.95, -76.33);
        var (minLat, maxLat, minLon, maxLon) = GeoMath.BoundingBox(center, radiusNm);

        Assert.True(maxLat - center.Lat >= radiusNm / 60.0 - 1e-9);
        Assert.True(minLat < center.Lat && minLon < center.Lon && maxLon > center.Lon);
    }

    [Fact]
    public void GeoPoint_HasValueEquality()
    {
        // record struct: two separate values with the same data are equal
        Assert.Equal(new GeoPoint(1, 2), new GeoPoint(1, 2));
    }
}
