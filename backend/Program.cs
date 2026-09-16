var builder = WebApplication.CreateBuilder(args);

builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
        policy.AllowAnyOrigin().AllowAnyMethod().AllowAnyHeader());
});

var app = builder.Build();
app.UseCors();

// Health check - used by ECS/ALB target group health checks
app.MapGet("/health", () => Results.Ok(new { status = "healthy", service = "geo-backend" }));

// Sample "geocode" style endpoint - stands in for a real ArcGIS/ESRI geocoding call
app.MapGet("/api/locations", () =>
{
    var locations = new[]
    {
        new { name = "Washington, DC", lat = 38.9072, lon = -77.0369 },
        new { name = "Richmond, VA",   lat = 37.5407, lon = -77.4360 },
        new { name = "Arlington, VA",  lat = 38.8816, lon = -77.0910 }
    };
    return Results.Ok(locations);
});

app.MapGet("/api/locations/{name}", (string name) =>
{
    var match = name.ToLower() switch
    {
        "dc" or "washington" => new { name = "Washington, DC", lat = 38.9072, lon = -77.0369 },
        "richmond"           => new { name = "Richmond, VA",   lat = 37.5407, lon = -77.4360 },
        "arlington"          => new { name = "Arlington, VA",  lat = 38.8816, lon = -77.0910 },
        _ => null
    };
    return match is null ? Results.NotFound() : Results.Ok(match);
});

app.Run("http://0.0.0.0:8080");
