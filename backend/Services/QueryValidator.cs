namespace Backend.Services;

/// <summary>
/// TRANSIENT: a new instance every time it's injected. Fine for small, stateless
/// helpers. Returns errors in the shape Results.ValidationProblem() expects, so the
/// API answers bad input with a standard 400 ProblemDetails response.
/// </summary>
public sealed class QueryValidator
{
    public const double MaxRadiusNm = 200;

    public Dictionary<string, string[]> ValidateNearby(double lat, double lon, double radiusNm)
    {
        var errors = new Dictionary<string, string[]>();
        if (lat is < -90 or > 90) errors["lat"] = ["Latitude must be between -90 and 90."];
        if (lon is < -180 or > 180) errors["lon"] = ["Longitude must be between -180 and 180."];
        if (radiusNm is <= 0 or > MaxRadiusNm)
            errors["radiusNm"] = [$"Radius must be greater than 0 and at most {MaxRadiusNm} nm."];
        return errors;
    }
}
