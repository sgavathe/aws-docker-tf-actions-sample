using Backend.Services;

namespace Backend.Middleware;

/// <summary>
/// Custom MIDDLEWARE: runs for every request, in the order registered in Program.cs.
/// Reuses the caller's X-Correlation-ID (or creates one), stores it in the scoped
/// RequestContext, echoes it back in the response, and adds it to every log line in
/// this request via a logging scope. Paste that ID into CloudWatch to trace a request.
///
/// Note: scoped services (RequestContext) are injected into InvokeAsync, NOT the
/// constructor -- middleware is created once, so constructor injection of a scoped
/// service would capture one instance for the whole app lifetime.
/// </summary>
public sealed class CorrelationIdMiddleware(RequestDelegate next, ILogger<CorrelationIdMiddleware> logger)
{
    public const string HeaderName = "X-Correlation-ID";

    public async Task InvokeAsync(HttpContext context, RequestContext requestContext)
    {
        var id = context.Request.Headers.TryGetValue(HeaderName, out var incoming) && !string.IsNullOrWhiteSpace(incoming)
            ? incoming.ToString()
            : Guid.NewGuid().ToString("n")[..12];

        requestContext.CorrelationId = id;
        context.Response.Headers[HeaderName] = id;

        using (logger.BeginScope(new Dictionary<string, object> { ["CorrelationId"] = id }))
        {
            await next(context); // hand off to the next middleware in the pipeline
        }
    }
}
