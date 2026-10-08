#!/usr/bin/env python3
"""How connected is a ci-graph.json? Explains what looks "disconnected" on the map.

    python pipeline/graph_report.py build/ci-graph.json
    python pipeline/graph_report.py build/ci-graph.json --csv build/isolated.csv   # list them

Reports
  1. Isolated assets: no link at all, by kind (these are the lone dots on the map)
  2. Grid islands: groups of energy assets wired to each other but not to the rest
  3. Unpowered grid: energy assets with no path to any power source (the cascade can't darken them)
  4. Missing services: consumers with no supplier found for a service they need
  5. Flow direction: how many grid links have no inferred direction
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict, deque

NEEDS = {  # mirrors DEPENDENCY_RULES in build_ci_graph.py
    "water_treatment": ["power"], "wastewater_plant": ["power"], "sewage_pumping": ["power"],
    "pumping_station": ["power", "water"], "water_tower": ["water"], "telecom_exchange": ["power"],
    "comm_tower": ["power", "comms"], "data_center": ["power", "comms"],
    "hospital": ["power", "water", "comms"], "fire_station": ["power", "water", "comms"],
    "police": ["power", "comms"], "ambulance_station": ["power", "comms"],
}
GRID_KINDS = {"grid_link", "distribution_link"}


def pct(n, d):
    return f"{n:>6,} ({100 * n / d:4.1f}%)" if d else f"{n:>6,}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("graph")
    ap.add_argument("--csv", help="write every isolated / unpowered / under-served asset to this CSV")
    a = ap.parse_args(argv)

    g = json.load(open(a.graph))
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = g["edges"]
    m = g.get("meta", {})
    print(f"{m.get('region', a.graph)}: {len(nodes):,} assets, {len(edges):,} links"
          f"{' (synthetic sample)' if m.get('sample') and 'sample' not in m.get('region', '') else ''}\n")

    degree = Counter()
    grid_adj = defaultdict(set)
    supplied = defaultdict(set)          # node -> service types it receives
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
        if e["kind"] in GRID_KINDS:
            grid_adj[e["from"]].add(e["to"])
            grid_adj[e["to"]].add(e["from"])
        else:
            supplied[e["to"]].add(e["type"])

    rows = []

    # 1. Isolated
    isolated = [n for n in nodes.values() if degree[n["id"]] == 0]
    print(f"1. Isolated assets (no link at all): {pct(len(isolated), len(nodes))}")
    for (sector, kind), c in Counter((n["sector"], n["kind"]) for n in isolated).most_common():
        total = sum(1 for n in nodes.values() if n["kind"] == kind)
        print(f"     {kind:<18} {c:>5,} of {total:,}")
    for n in isolated:
        rows.append((n, "isolated", "no wires mapped and nothing in range"))
    print("   Why: the pipeline keeps every energy asset even when no wire reaches it.\n"
          "   Plants never get a fallback link; substations only get one if a wired\n"
          "   substation is within 15 km.\n")

    # 2. Grid islands
    energy = [i for i, n in nodes.items() if n["sector"] == "energy"]
    seen, comps = set(), []
    for s in energy:
        if s in seen or not grid_adj[s]:
            continue
        comp, q = [], deque([s])
        seen.add(s)
        while q:
            u = q.popleft()
            comp.append(u)
            for v in grid_adj[u]:
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    wired = sum(len(c) for c in comps)
    print(f"2. Grid islands: {len(comps):,} separate groups among {wired:,} wired energy assets")
    if comps:
        sizes = [len(c) for c in comps]
        rest = f", then {', '.join(map(str, sizes[1:8]))}{' ...' if len(sizes) > 8 else ''}" if len(sizes) > 1 else ""
        print(f"     largest {sizes[0]:,} ({100 * sizes[0] / wired:.0f}% of wired){rest}")
        print(f"     groups of 2-3 assets: {sum(1 for s in sizes if s <= 3):,}")
    print("   Why: OSM often has gaps where a line stops short of a substation or a\n"
          "   segment is unmapped, so a short line ends up joined to nothing.\n")

    # 3. Unpowered grid (no path to a source)
    sources = [i for i in energy if nodes[i].get("source")]
    on, q = set(sources), deque(sources)
    while q:
        u = q.popleft()
        for v in grid_adj[u]:
            if v not in on:
                on.add(v)
                q.append(v)
    dark = [i for i in energy if i not in on]
    print(f"3. Energy assets with no path to any power source: {pct(len(dark), len(energy))}")
    print(f"     power sources: {len(sources):,}")
    for kind, c in Counter(nodes[i]["kind"] for i in dark).most_common():
        print(f"     {kind:<18} {c:>5,}")
    for i in dark:
        if degree[i]:
            rows.append((nodes[i], "unpowered", "wired, but its group has no plant or high-voltage tie"))
    print("   Effect: these can never go dark in a cascade (they were never 'on'), so\n"
          "   drawing around their supply changes nothing for them or what they feed.\n")

    # 4. Missing services
    print("4. Consumers missing a service they need (no supplier in range):")
    any_missing = False
    for kind, needs in NEEDS.items():
        group = [n for n in nodes.values() if n["kind"] == kind]
        if not group:
            continue
        for t in needs:
            miss = [n for n in group if t not in supplied[n["id"]]]
            if miss:
                any_missing = True
                print(f"     {kind:<18} no {t:<6} {len(miss):>5,} of {len(group):,}")
                rows.extend((n, f"no_{t}", "no supplier of that kind within range") for n in miss)
    if not any_missing:
        print("     none")
    print()

    # 5. Direction
    gl = [e for e in edges if e["kind"] == "grid_link"]
    undirected = sum(1 for e in gl if e.get("directed") is False)
    print(f"5. Grid links with unknown flow direction: {pct(undirected, len(gl))}")
    print("   (drawn dashed; mostly lines with no voltage tag between equal-voltage substations)")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["issue", "why", "id", "osm", "name", "sector", "kind", "lat", "lon", "osm_url"])
            for n, issue, why in rows:
                osm = n.get("osm", "")
                url = {"n": "node", "w": "way", "r": "relation"}.get(osm[:1])
                w.writerow([issue, why, n["id"], osm, n["name"], n["sector"], n["kind"], n["lat"], n["lon"],
                            f"https://www.openstreetmap.org/{url}/{osm[1:]}" if url else ""])
        print(f"\nWrote {len(rows):,} rows to {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
