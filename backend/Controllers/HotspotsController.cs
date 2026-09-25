using Backend.Models;
using Backend.Services;
using Microsoft.AspNetCore.Mvc;

namespace Backend.Controllers;

/// <summary>
/// CONTROLLER-based endpoint, shown alongside the minimal APIs for contrast.
/// Controllers suit larger APIs (filters, model binding attributes, conventions);
/// minimal APIs suit small, focused services. Both run in the same pipeline.
/// </summary>
[ApiController]
[Route("api/[controller]")]   // => /api/hotspots
public sealed class HotspotsController(IHotspotService hotspots) : ControllerBase
{
    /// <summary>ML.NET K-Means hotspots as GeoJSON. k = number of clusters (2-12).</summary>
    [HttpGet]
    [ProducesResponseType(typeof(FeatureCollection), StatusCodes.Status200OK)]
    public ActionResult<FeatureCollection> Get([FromQuery] int k = 8)
    {
        k = Math.Clamp(k, 2, 12);
        return Ok(GeoJson.ToCollection(hotspots.GetHotspots(k)));
    }
}
