using Backend.Models;

namespace Backend.Data;

/// <summary>
/// INTERFACE = a contract. Services depend on this, not on a concrete class, so we can
/// swap the in-memory implementation for SQL Server / PostGIS / an ArcGIS feature
/// service without touching callers (and mock it in unit tests).
/// </summary>
public interface IIncidentRepository
{
    /// <summary>
    /// IQueryable (not IEnumerable) so callers can compose filters that a real
    /// provider (EF Core) translates into SQL and runs in the database.
    /// </summary>
    IQueryable<Incident> Query();

    /// <summary>
    /// Async signature so a database-backed implementation can do real I/O.
    /// ValueTask avoids a Task allocation when the result is available synchronously
    /// (as it is for the in-memory version).
    /// </summary>
    ValueTask<Incident?> FindAsync(int id, CancellationToken ct = default);
}
