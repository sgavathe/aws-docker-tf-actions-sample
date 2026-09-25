using System.Globalization;
using System.Text.Json;
using Backend.Models;

namespace Backend.Services;

/// <summary>
/// TYPED HttpClient, created via IHttpClientFactory (see Program.cs). The factory pools
/// and recycles the underlying handlers, avoiding the two classic bugs:
/// socket exhaustion from `new HttpClient()` per request, and stale DNS from a single
/// static HttpClient that lives forever.
///
/// Calls the public National Weather Service API (no key needed, US points only).
/// </summary>
public sealed class WeatherClient(HttpClient http, ILogger<WeatherClient> logger)
{
    public async Task<WeatherSummary?> GetForecastAsync(GeoPoint p, CancellationToken ct)
    {
        try
        {
            // 1) Resolve the forecast URL for this point.
            var pointsUrl = string.Create(CultureInfo.InvariantCulture, $"points/{p.Lat:F4},{p.Lon:F4}");
            using var pointsDoc = await GetJsonAsync(pointsUrl, ct);
            var forecastUrl = pointsDoc.RootElement.GetProperty("properties").GetProperty("forecast").GetString();
            if (forecastUrl is null) return null;

            // 2) Fetch the forecast and take the first period.
            using var forecastDoc = await GetJsonAsync(forecastUrl, ct);
            var first = forecastDoc.RootElement.GetProperty("properties").GetProperty("periods")[0];

            return new WeatherSummary(
                Period: first.GetProperty("name").GetString() ?? "",
                Temperature: first.GetProperty("temperature").GetInt32(),
                TemperatureUnit: first.GetProperty("temperatureUnit").GetString() ?? "F",
                WindSpeed: first.GetProperty("windSpeed").GetString() ?? "",
                WindDirection: first.GetProperty("windDirection").GetString() ?? "",
                ShortForecast: first.GetProperty("shortForecast").GetString() ?? "");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or KeyNotFoundException)
        {
            // Catch the specific failures we expect from a flaky upstream; let anything
            // else bubble up to the global exception handler.
            logger.LogWarning(ex, "Weather lookup failed for {Lat},{Lon}", p.Lat, p.Lon);
            return null;
        }
    }

    private async Task<JsonDocument> GetJsonAsync(string url, CancellationToken ct)
    {
        using var response = await http.GetAsync(url, ct);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        return await JsonDocument.ParseAsync(stream, cancellationToken: ct);
    }
}
