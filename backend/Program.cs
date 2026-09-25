using System.Text.Json.Serialization;
using Backend.Data;
using Backend.Endpoints;
using Backend.Middleware;
using Backend.Services;

var builder = WebApplication.CreateBuilder(args);

// ---------------------------------------------------------------------------
// 1) SERVICES (dependency injection container) -- lifetimes on purpose:
//    Singleton = one for the app, Scoped = one per HTTP request,
//    Transient = new every time it's requested.
// ---------------------------------------------------------------------------
builder.Services.AddSingleton<IIncidentRepository, InMemoryIncidentRepository>(); // shared read-only data
builder.Services.AddSingleton<IHotspotService, HotspotService>();                 // cached ML model results
builder.Services.AddScoped<RequestContext>();                                     // per-request correlation id
builder.Services.AddScoped<IIncidentService, IncidentService>();                  // per-request business logic
builder.Services.AddTransient<QueryValidator>();                                  // stateless helper

// Typed HttpClient via IHttpClientFactory (pooled handlers, central config).
builder.Services.AddHttpClient<WeatherClient>(client =>
{
    client.BaseAddress = new Uri("https://api.weather.gov/");
    client.Timeout = TimeSpan.FromSeconds(8);
    client.DefaultRequestHeaders.UserAgent.ParseAdd("geo-devops-demo (map.spatialenable.com)"); // NWS requires a UA
});

// Enums as strings ("Pollution", not 0) for both minimal APIs and controllers.
builder.Services.ConfigureHttpJsonOptions(o => o.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));
builder.Services.AddControllers()
    .AddJsonOptions(o => o.JsonSerializerOptions.Converters.Add(new JsonStringEnumConverter()));

builder.Services.AddProblemDetails();   // RFC 7807 error bodies for unhandled exceptions
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

// The frontend calls the API from the browser. In ECS both sit behind the same ALB
// hostname, so this mainly matters for local dev (Vite on :5173, API on :8080).
builder.Services.AddCors(options =>
    options.AddDefaultPolicy(policy => policy
        .WithOrigins(builder.Configuration["Cors:AllowedOrigins"]?.Split(',') ??
                     new[] { "http://localhost:5173", "http://localhost:4173", "http://localhost:8081", "https://map.spatialenable.com" })
        .AllowAnyHeader()
        .AllowAnyMethod()
        .WithExposedHeaders(CorrelationIdMiddleware.HeaderName)));

var app = builder.Build();

// ---------------------------------------------------------------------------
// 2) MIDDLEWARE PIPELINE -- order matters: each piece wraps everything after it.
// ---------------------------------------------------------------------------
app.UseExceptionHandler();                        // outermost: catches anything below
app.UseMiddleware<CorrelationIdMiddleware>();     // tag every request + log line
app.UseCors();

// Swagger under /api/* so the existing ALB rule ("/api/*" -> backend) routes it.
app.UseSwagger(o => o.RouteTemplate = "api/swagger/{documentName}/swagger.json");
app.UseSwaggerUI(o =>
{
    o.RoutePrefix = "api/swagger";
    o.SwaggerEndpoint("/api/swagger/v1/swagger.json", "Geo Maritime API v1");
});

// ---------------------------------------------------------------------------
// 3) ENDPOINTS
// ---------------------------------------------------------------------------
// Health check -- used by the ALB target group (path "/health").
app.MapGet("/health", () => Results.Ok(new { status = "healthy", service = "geo-backend" }))
   .ExcludeFromDescription();

app.MapIncidentEndpoints();   // minimal APIs  (Endpoints/IncidentEndpoints.cs)
app.MapControllers();         // controllers   (Controllers/HotspotsController.cs)

// Port is configurable for local dev (e.g., PORT=5080); ECS uses the 8080 default.
var port = Environment.GetEnvironmentVariable("PORT") ?? "8080";
app.Run($"http://0.0.0.0:{port}");

// Makes the implicit Program class visible to integration tests (WebApplicationFactory).
public partial class Program { }
