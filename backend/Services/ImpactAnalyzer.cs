using Backend.Models;

namespace Backend.Services;

/// <summary>
/// "What breaks if everything inside this area goes down?"
///
///   1. Direct hits      Every asset inside the area fails (hop 0).
///   2. Cut wires        Mapped power lines crossing the area are severed.
///   3. Grid islands     Energy assets that no longer have ANY path to a power source
///                       (plant, or tie to the grid outside the study area) are de-energized.
///                       Redundant routes keep assets alive; that's why this is a
///                       connectivity check rather than "everything downstream fails".
///   4. Service cascade  A dependent loses a service (power, water, comms) when every
///                       supplier of that service has failed. If it has backup for that
///                       service (generator, stored water, radio) it is "degraded" and
///                       keeps operating; otherwise it fails and the cascade continues.
///
/// Hop = how many dependency links the failure travelled from the drawn area.
/// Pure function of (graph, area): no I/O, easy to unit test.
/// </summary>
public static class ImpactAnalyzer
{
    public static readonly string[] SectorOrder =
        ["energy", "water", "communications", "it", "health", "emergency"];

    private enum State : byte { Ok, Degraded, Failed }

    public static ImpactResult Analyze(InfrastructureGraph g, AreaPolygon area)
    {
        var nodes = g.Nodes;
        var n = nodes.Count;
        var state = new State[n];
        var hop = new int[n];
        var cause = new string?[n];
        var via = new int[n];
        var viaType = new string?[n];
        var lost = new List<string>?[n];
        Array.Fill(via, -1);

        // 1) Direct hits -------------------------------------------------------------
        var directlyHit = 0;
        for (var i = 0; i < n; i++)
        {
            if (!area.Contains(nodes[i].Lon, nodes[i].Lat)) continue;
            state[i] = State.Failed;
            cause[i] = "Inside the drawn area";
            directlyHit++;
        }

        // 2) Severed wires -------------------------------------------------------------
        var severed = new bool[g.Edges.Count];
        var severedIds = new List<string>();
        for (var e = 0; e < g.Edges.Count; e++)
        {
            var coords = g.Edges[e].Coords;
            if (coords is null || coords.Count < 2) continue;
            for (var s = 1; s < coords.Count; s++)
            {
                if (coords[s - 1].Length < 2 || coords[s].Length < 2) continue;
                if (!area.Intersects(coords[s - 1][0], coords[s - 1][1], coords[s][0], coords[s][1])) continue;
                severed[e] = true;
                severedIds.Add(g.Edges[e].Id);
                break;
            }
        }

        // 3) Grid islands ---------------------------------------------------------------
        var before = Energized(g, null, null);
        var after = Energized(g, state, severed);

        var gridHop = new int[n];
        var gridVia = new int[n];
        Array.Fill(gridHop, -1);
        Array.Fill(gridVia, -1);
        bool InDarkGrid(int i) => g.IsGrid[i] && (state[i] == State.Failed || (before[i] && !after[i]));

        var queue = new Queue<int>();
        for (var i = 0; i < n; i++)
        {
            if (g.IsGrid[i] && state[i] == State.Failed)
            {
                gridHop[i] = 0;
                queue.Enqueue(i);
            }
        }
        for (var e = 0; e < severed.Length; e++)
        {
            if (!severed[e]) continue;
            foreach (var end in new[] { g.EdgeFrom[e], g.EdgeTo[e] })
            {
                if (gridHop[end] != -1 || !InDarkGrid(end)) continue;
                gridHop[end] = 1;
                cause[end] = "Its wires were cut inside the drawn area";
                queue.Enqueue(end);
            }
        }
        while (queue.Count > 0)
        {
            var u = queue.Dequeue();
            foreach (var (v, _) in g.GridNeighbors[u])
            {
                if (gridHop[v] != -1 || !InDarkGrid(v)) continue;
                gridHop[v] = gridHop[u] + 1;
                gridVia[v] = u;
                queue.Enqueue(v);
            }
        }
        for (var i = 0; i < n; i++)
        {
            if (!g.IsGrid[i] || state[i] == State.Failed || !before[i] || after[i]) continue;
            state[i] = State.Failed;
            hop[i] = gridHop[i] == -1 ? 1 : gridHop[i];
            via[i] = gridVia[i];
            viaType[i] = "power";
            cause[i] ??= gridVia[i] >= 0
                ? $"No remaining path to a power source (upstream: {nodes[gridVia[i]].Name})"
                : "No remaining path to a power source";
            lost[i] = ["power"];
        }

        // 4) Service cascade (lowest hop first, so each asset gets its earliest failure) -----
        var pending = new PriorityQueue<int, int>();
        for (var i = 0; i < n; i++)
            if (state[i] == State.Failed)
                pending.Enqueue(i, hop[i]);

        while (pending.TryDequeue(out var u, out _))
        {
            foreach (var e in g.ServiceOut[u])
            {
                var v = g.EdgeTo[e];
                if (state[v] == State.Failed) continue;
                if (Evaluate(g, v, state, hop, cause, via, viaType, lost))
                    pending.Enqueue(v, hop[v]);
            }
        }

        return BuildResult(g, area, state, hop, cause, via, viaType, lost, directlyHit, severedIds);
    }

    /// <summary>Re-check one dependent. Returns true when it has just failed.</summary>
    private static bool Evaluate(InfrastructureGraph g, int v, State[] state, int[] hop, string?[] cause,
                                 int[] via, string?[] viaType, List<string>?[] lost)
    {
        var node = g.Nodes[v];
        var lostTypes = new List<(string Type, int Hop, int Via)>();

        foreach (var group in g.ServiceIn[v].GroupBy(e => g.Edges[e].Type))
        {
            var suppliers = group.Select(e => g.EdgeFrom[e]).ToList();
            if (suppliers.Any(s => state[s] != State.Failed)) continue;
            var last = suppliers.MaxBy(s => hop[s]);
            lostTypes.Add((group.Key, hop[last] + 1, last));
        }
        if (lostTypes.Count == 0) return false;

        var critical = lostTypes.Where(t => !node.Backup.Contains(t.Type)).OrderBy(t => t.Hop).ToList();
        var worst = critical.Count > 0 ? critical[0] : lostTypes.OrderBy(t => t.Hop).First();
        var newState = critical.Count > 0 ? State.Failed : State.Degraded;
        var supplier = g.Nodes[worst.Via].Name;

        state[v] = newState;
        hop[v] = worst.Hop;
        via[v] = worst.Via;
        viaType[v] = worst.Type;
        var lostNames = lostTypes.Select(t => t.Type).Distinct().ToList();
        lost[v] = lostNames;
        cause[v] = newState == State.Failed
            ? $"Lost {worst.Type} from {supplier}"
            : $"Lost {string.Join(" and ", lostNames)}; running on backup (upstream: {supplier})";
        return newState == State.Failed;
    }

    /// <summary>Energy assets reachable from a working power source over intact wires.</summary>
    private static bool[] Energized(InfrastructureGraph g, State[]? state, bool[]? severed)
    {
        var n = g.Nodes.Count;
        var on = new bool[n];
        var queue = new Queue<int>();
        for (var i = 0; i < n; i++)
        {
            if (!g.IsGrid[i] || g.Nodes[i].Source is null) continue;
            if (state is not null && state[i] == State.Failed) continue;
            on[i] = true;
            queue.Enqueue(i);
        }
        while (queue.Count > 0)
        {
            var u = queue.Dequeue();
            foreach (var (v, e) in g.GridNeighbors[u])
            {
                if (on[v] || (severed is not null && severed[e])) continue;
                if (state is not null && state[v] == State.Failed) continue;
                on[v] = true;
                queue.Enqueue(v);
            }
        }
        return on;
    }

    private static ImpactResult BuildResult(InfrastructureGraph g, AreaPolygon area, State[] state, int[] hop,
                                            string?[] cause, int[] via, string?[] viaType, List<string>?[] lost,
                                            int directlyHit, List<string> severedIds)
    {
        var nodes = g.Nodes;
        var impacts = new List<AssetImpact>();
        for (var i = 0; i < nodes.Count; i++)
        {
            if (state[i] == State.Ok) continue;
            var nd = nodes[i];
            impacts.Add(new AssetImpact(
                nd.Id, nd.Name, nd.Sector, nd.Kind,
                state[i] == State.Failed ? ImpactStatus.Failed : ImpactStatus.Degraded,
                hop[i],
                cause[i] ?? "Failed",
                via[i] >= 0 ? nodes[via[i]].Id : null,
                via[i] >= 0 ? viaType[i] : null,
                lost[i] ?? [],
                Math.Round(area.DistanceKm(nd.Lon, nd.Lat), 1)));
        }

        int SectorRank(string s)
        {
            var r = Array.IndexOf(SectorOrder, s);
            return r < 0 ? SectorOrder.Length : r;
        }

        impacts = impacts
            .OrderBy(x => x.Hop)
            .ThenBy(x => SectorRank(x.Sector))
            .ThenBy(x => x.Status)
            .ThenBy(x => x.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();

        var sectors = nodes
            .GroupBy(x => x.Sector)
            .OrderBy(grp => SectorRank(grp.Key))
            .Select(grp => new SectorImpact(
                grp.Key,
                grp.Count(),
                impacts.Count(x => x.Sector == grp.Key && x.Status == ImpactStatus.Failed),
                impacts.Count(x => x.Sector == grp.Key && x.Status == ImpactStatus.Degraded)))
            .ToList();

        var hops = impacts
            .GroupBy(x => x.Hop)
            .OrderBy(grp => grp.Key)
            .Select(grp => new HopImpact(
                grp.Key,
                grp.Count(x => x.Status == ImpactStatus.Failed),
                grp.Count(x => x.Status == ImpactStatus.Degraded)))
            .ToList();

        var summary = new ImpactSummary(
            DirectlyHit: directlyHit,
            Failed: impacts.Count(x => x.Status == ImpactStatus.Failed),
            Degraded: impacts.Count(x => x.Status == ImpactStatus.Degraded),
            SeveredLines: severedIds.Count,
            MaxHops: impacts.Count == 0 ? 0 : impacts.Max(x => x.Hop),
            ReachKm: impacts.Count == 0 ? 0 : impacts.Max(x => x.DistanceKm),
            AreaKm2: Math.Round(area.AreaKm2, 1),
            Sectors: sectors,
            Hops: hops);

        return new ImpactResult(summary, impacts, severedIds, g.Version, g.Meta.Sample);
    }
}
