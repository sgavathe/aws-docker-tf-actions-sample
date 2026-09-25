using Backend.Models;

namespace Backend.Data;

/// <summary>
/// Synthetic, deterministic demo data (NOT real USCG data). Registered as a SINGLETON:
/// one shared, read-only instance for the life of the app, which is safe because the
/// data never changes after construction.
/// </summary>
public sealed class InMemoryIncidentRepository : IIncidentRepository
{
    private readonly List<Incident> _incidents;
    private readonly Dictionary<int, Incident> _byId;

    public InMemoryIncidentRepository()
    {
        _incidents = Seed();
        _byId = _incidents.ToDictionary(i => i.Id);
    }

    public IQueryable<Incident> Query() => _incidents.AsQueryable();

    public ValueTask<Incident?> FindAsync(int id, CancellationToken ct = default) =>
        ValueTask.FromResult(_byId.GetValueOrDefault(id));

    private static List<Incident> Seed()
    {
        var rng = new Random(42); // fixed seed => same data on every start
        var vessels = new[]
        {
            "MV Atlantic Dawn", "F/V Sea Harvest", "T/B Delta Carrier", "MV Gulf Trader",
            "S/V Windward", "MV Pacific Star", "Tug Resolute", "F/V Northern Light",
            "MV Chesapeake", "T/V Liberty Bell"
        };
        var summaries = new Dictionary<IncidentType, string[]>
        {
            [IncidentType.Pollution] = ["Oil sheen reported near anchorage", "Fuel spill during bunkering"],
            [IncidentType.Collision] = ["Allision with pier", "Collision in traffic separation scheme"],
            [IncidentType.Grounding] = ["Grounding outside marked channel", "Soft grounding at low tide"],
            [IncidentType.Fire]      = ["Engine room fire, crew mustered", "Galley fire extinguished"],
            [IncidentType.Machinery] = ["Loss of propulsion", "Steering casualty reported"],
            [IncidentType.MedEvac]   = ["Crew member medevac requested", "Injury aboard, transfer to shore"],
        };
        var types = Enum.GetValues<IncidentType>();

        var list = new List<Incident>();
        var id = 1;
        var start = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        foreach (var port in Ports.All)
        {
            var count = rng.Next(10, 22); // busier and quieter ports
            for (var n = 0; n < count; n++)
            {
                var type = types[rng.Next(types.Length)];
                var location = new GeoPoint(
                    port.Location.Lat + (rng.NextDouble() - 0.5) * 0.6,
                    port.Location.Lon + (rng.NextDouble() - 0.5) * 0.6);

                list.Add(new Incident(
                    Id: id++,
                    Type: type,
                    Severity: rng.Next(1, 6),
                    Location: location,
                    ReportedUtc: start.AddHours(rng.Next(0, 24 * 260)),
                    PortId: port.Id,
                    VesselName: vessels[rng.Next(vessels.Length)],
                    Summary: summaries[type][rng.Next(2)]));
            }
        }
        return list;
    }
}
