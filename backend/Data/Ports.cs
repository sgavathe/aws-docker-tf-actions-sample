using Backend.Models;

namespace Backend.Data;

public static class Ports
{
    public static readonly IReadOnlyList<Port> All =
    [
        new("HRO", "Hampton Roads, VA",       new(36.95, -76.33)),
        new("BAL", "Baltimore, MD",           new(39.26, -76.58)),
        new("NYC", "New York / New Jersey",   new(40.67, -74.05)),
        new("MIA", "Miami, FL",               new(25.77, -80.17)),
        new("NOL", "New Orleans, LA",         new(29.94, -90.06)),
        new("HOU", "Houston, TX",             new(29.73, -95.02)),
        new("LAX", "Los Angeles / Long Beach", new(33.74, -118.26)),
        new("SEA", "Seattle, WA",             new(47.60, -122.34)),
    ];
}
