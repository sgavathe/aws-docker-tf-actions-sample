namespace Backend.Services;

/// <summary>
/// SCOPED: one instance per HTTP request. The correlation-id middleware fills it in,
/// and any service in the same request can read it (e.g., to tag log lines).
/// </summary>
public sealed class RequestContext
{
    public string CorrelationId { get; set; } = "";
}
