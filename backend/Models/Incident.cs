namespace Backend.Models;

public enum IncidentType
{
    Pollution,
    Collision,
    Grounding,
    Fire,
    Machinery,
    MedEvac
}

/// <summary>
/// REFERENCE TYPE (sealed record class): lives on the heap, passed by reference,
/// but still immutable with value-based equality and "with" expressions.
/// Contrast with <see cref="GeoPoint"/>, which is a struct.
/// </summary>
public sealed record Incident(
    int Id,
    IncidentType Type,
    int Severity,              // 1 (minor) .. 5 (major)
    GeoPoint Location,
    DateTime ReportedUtc,
    string PortId,
    string VesselName,
    string Summary);

public sealed record Port(string Id, string Name, GeoPoint Location);

/// <summary>An incident plus its distance from a search point.</summary>
public sealed record IncidentHit(Incident Incident, double DistanceNm);

/// <summary>A cluster centre produced by the ML.NET K-Means model.</summary>
public sealed record Hotspot(
    int ClusterId,
    GeoPoint Center,
    int IncidentCount,
    double AvgSeverity,
    IncidentType DominantType,
    double RadiusNm,
    double RiskScore);

public sealed record WeatherSummary(
    string Period,
    int Temperature,
    string TemperatureUnit,
    string WindSpeed,
    string WindDirection,
    string ShortForecast);
