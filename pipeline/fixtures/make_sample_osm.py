#!/usr/bin/env python3
"""
Write a SYNTHETIC .osm file around Richmond, VA for tests and the bundled demo graph.

Nothing here is real infrastructure: names are invented and positions are a made-up
layout. It exercises the same tags the pipeline reads from real OSM extracts:
substation/plant polygons, wires with towers, a wire passing through a substation,
a mid-span tap, ties leaving the study area, unwired distribution substations,
water, telecom, health and emergency facilities.

    python pipeline/fixtures/make_sample_osm.py pipeline/fixtures/richmond_sample.osm
"""
import math
import random
import sys
from xml.sax.saxutils import quoteattr

LAT0, LON0 = 37.5407, -77.4360
KM_LAT = 110.95
KM_LON = 111.32 * math.cos(math.radians(LAT0))


def ll(dx_km, dy_km):
    return LAT0 + dy_km / KM_LAT, LON0 + dx_km / KM_LON


class Osm:
    def __init__(self):
        self.nodes, self.ways, self._id = [], [], 0

    def nid(self):
        self._id += 1
        return self._id

    def node(self, dx, dy, tags=None):
        i = self.nid()
        lat, lon = ll(dx, dy)
        self.nodes.append((i, lat, lon, tags or {}))
        return i

    def way(self, refs, tags):
        i = self.nid()
        self.ways.append((i, refs, tags))
        return i

    def polygon(self, dx, dy, size_km, tags):
        h = size_km / 2
        refs = [self.node(dx - h, dy - h), self.node(dx + h, dy - h),
                self.node(dx + h, dy + h), self.node(dx - h, dy + h)]
        return self.way(refs + [refs[0]], tags)

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="make_sample_osm">\n')
            for i, lat, lon, tags in self.nodes:
                if tags:
                    f.write(f'  <node id="{i}" version="1" lat="{lat:.7f}" lon="{lon:.7f}">\n')
                    for k, v in tags.items():
                        f.write(f"    <tag k={quoteattr(k)} v={quoteattr(str(v))}/>\n")
                    f.write("  </node>\n")
                else:
                    f.write(f'  <node id="{i}" version="1" lat="{lat:.7f}" lon="{lon:.7f}"/>\n')
            for i, refs, tags in self.ways:
                f.write(f'  <way id="{i}" version="1">\n')
                for r in refs:
                    f.write(f'    <nd ref="{r}"/>\n')
                for k, v in tags.items():
                    f.write(f"    <tag k={quoteattr(k)} v={quoteattr(str(v))}/>\n")
                f.write("  </way>\n")
            f.write("</osm>\n")


def main(path):
    random.seed(7)
    o = Osm()

    # ---------- Energy: plants + substations (dx, dy in km from downtown Richmond) ----------
    subs = {
        # key: (dx, dy, name, voltage)
        "N500": (0, 35, "Sample North Tie 500 kV Substation", "500000;230000"),
        "S500": (8, -25, "Sample South Tie 500 kV Substation", "500000;230000"),
        "A": (-15, 15, "Sample Westbrook Substation", "230000;115000"),
        "B": (12, 12, "Sample Eastgate Substation", "230000;115000"),
        "C": (-18, -10, "Sample Ridgeline Substation", "230000;115000"),
        "D": (15, -8, "Sample Riverbend Substation", "230000;115000"),
        "E": (0, 0, "Sample Downtown Substation", "230000;115000"),
        "F": (-25, 5, "Sample Millwood Substation", "115000;34500"),
        "G": (-8, 22, "Sample Hanover Road Substation", "115000;34500"),
        "H": (22, 20, "Sample Cold Harbor Substation", "115000;34500"),
        "I": (25, -2, "Sample Varina Substation", "115000;34500"),
        "J": (-5, -22, "Sample Chester Substation", "115000;34500"),
        "K": (5, -5, "Sample Manchester Substation", "115000;34500"),
        "L": (-12, -2, "Sample Westover Substation", "115000;34500"),
        "M": (8, 6, "Sample Church Hill Substation", "115000;34500"),
    }
    plants = {
        "P1": (5, -15, "Sample James Bend Generating Station", "gas", None),
        "P2": (-20, 28, "Sample Hanover Solar Farm", "solar", "80 MW"),
        "P3": (-9, 6, "Sample Grocery Rooftop Solar", "solar", "350 kW"),   # too small to be a source
        "P4": (13.5, 13.2, "Sample Eastgate Community Solar", "solar", "5 MW"),  # no wires; 1.9 km from Eastgate
    }
    centre = {}
    for k, (dx, dy, name, volt) in subs.items():
        o.polygon(dx, dy, 0.25, {"power": "substation", "name": name, "voltage": volt,
                                 "substation": "transmission"})
        centre[k] = (dx, dy)
    for k, (dx, dy, name, src, output) in plants.items():
        tags = {"power": "plant", "name": name, "plant:source": src}
        if output:
            tags["plant:output:electricity"] = output
        o.polygon(dx, dy, 0.8 if k not in ("P3", "P4") else 0.1, tags)
        centre[k] = (dx, dy)

    def wire(points, volt, name=None):
        """Wire through the given (dx, dy) waypoints with a tower every ~1.2 km."""
        refs = []
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 1.2))
            for s in range(n):
                t = s / n
                refs.append(o.node(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t,
                                   {"power": "tower"} if refs else None))
        refs.append(o.node(*points[-1]))
        tags = {"power": "line", "voltage": volt}
        if name:
            tags["name"] = name
        o.way(refs, tags)
        return refs

    W = lambda *keys: [centre[k] if isinstance(k, str) else k for k in keys]  # noqa: E731
    # Ties leaving the 30-mile study area -> treated as outside supply.
    wire(W("N500", (0, 62)), "500000", "Sample Northern 500 kV Tie")
    wire(W("S500", (18, -60)), "500000", "Sample Southern 500 kV Tie")
    # Feeders that also leave the area but are NOT imports: a 34.5 kV line and an untagged one.
    wire(W("F", (-62, 5)), "34500", "Sample Western 34.5 kV Feeder")
    wire(W("I", (62, -2)), "0")
    o.ways[-1][2].pop("voltage")          # untagged line
    # Generation
    wire(W("P1", "S500"), "500000")
    wire(W("P1", "D"), "230000")
    wire(W("P2", "G"), "115000")
    # 230 kV backbone (with a loop C-D and a ring through downtown)
    wire(W("S500", "D"), "230000")
    wire(W("N500", "A"), "230000")
    be = wire(W("N500", "B"), "230000")
    wire(W("A", "E"), "230000")
    be2 = wire(W("B", "E"), "230000")
    wire(W("C", "E"), "230000")
    wire(W("D", "E"), "230000")
    wire(W("D", (5, -14), "C"), "230000")
    # 115 kV
    wire(W("A", "G"), "115000")
    wire(W("B", "H"), "115000")
    wire(W("D", "I"), "115000")
    wire(W("C", "J"), "115000")
    wire(W("E", "K"), "115000")
    wire(W("E", "L", "F"), "115000")        # passes straight through Westover (L)
    # Mid-span tap: Church Hill (M) hangs off a tower in the middle of the B-E line.
    tap = be2[len(be2) // 2]
    tap_lat, tap_lon = next((lat, lon) for i, lat, lon, _ in o.nodes if i == tap)
    tx, ty = (tap_lon - LON0) * KM_LON, (tap_lat - LAT0) * KM_LAT
    refs = [tap] + [o.node(tx + (centre["M"][0] - tx) * t, ty + (centre["M"][1] - ty) * t,
                           {"power": "tower"}) for t in (0.33, 0.66)] + [o.node(*centre["M"])]
    o.way(refs, {"power": "line", "voltage": "115000", "name": "Sample Church Hill Tap"})
    del be
    # A wired pair with no source of its own: the line that would join it to the grid isn't mapped.
    for k, (dx, dy, name) in {"X1": (30, 10, "Sample Bottoms Bridge Substation"),
                              "X2": (33, 14, "Sample Providence Forge Substation")}.items():
        o.polygon(dx, dy, 0.25, {"power": "substation", "name": name, "voltage": "115000;34500"})
        centre[k] = (dx, dy)
    wire(W("X1", "X2"), "115000")

    # Distribution substations with no wires mapped (common in real OSM data).
    for n, (dx, dy) in enumerate([(-28, -14), (-22, 18), (-3, 12), (6, 25), (19, 8),
                                  (28, -12), (10, -20), (-10, -15), (-30, 22), (2, -30)]):
        o.node(dx, dy, {"power": "substation", "substation": "distribution", "voltage": "34500",
                        "name": f"Sample Distribution Substation {n + 1}"})

    # A state boundary covering the western half, for --states tests (60 km square at x -45..15).
    o.polygon(-15, 0, 60, {"boundary": "administrative", "admin_level": "4", "name": "Sample State",
                           "type": "boundary"})

    # ---------- Water ----------
    o.polygon(-6, 4, 0.5, {"man_made": "water_works", "name": "Sample City Water Treatment Plant"})
    o.polygon(14, -14, 0.5, {"man_made": "water_works", "name": "Sample County Water Treatment Plant"})
    o.polygon(10, -3, 0.5, {"man_made": "wastewater_plant", "name": "Sample Wastewater Treatment Plant"})
    pumping = [(-12, 8), (-4, 15), (6, 9), (18, 4), (-16, -6), (4, -12), (20, -16), (-8, -24)]
    for n, (dx, dy) in enumerate(pumping):
        o.node(dx, dy, {"man_made": "pumping_station", "pumping_station": "water",
                        "name": f"Sample Booster Pump Station {n + 1}"})
    for n, (dx, dy) in enumerate([(3, -2), (14, 14)]):
        o.node(dx, dy, {"man_made": "pumping_station", "substance": "sewage",
                        "name": f"Sample Sewage Lift Station {n + 1}"})
    towers = [(-20, 12), (-2, 19), (9, 13), (24, 6), (-22, -4), (2, -16), (22, -20), (-6, -28), (-28, 0)]
    for n, (dx, dy) in enumerate(towers):
        o.node(dx, dy, {"man_made": "water_tower", "name": f"Sample Water Tower {n + 1}"})

    # ---------- Communications / IT ----------
    for name, (dx, dy) in [("Sample Central Telephone Exchange", (1, 1)),
                           ("Sample West End Exchange", (-14, 12)),
                           ("Sample Southside Exchange", (16, -10))]:
        o.polygon(dx, dy, 0.08, {"telecom": "exchange", "building": "yes", "name": name})
    o.polygon(-20, 8, 0.3, {"building": "data_center", "name": "Sample West Creek Data Center"})
    o.polygon(12, 18, 0.3, {"telecom": "data_center", "name": "Sample Airport Data Center"})
    for n in range(14):
        a, r = random.uniform(0, 2 * math.pi), random.uniform(6, 34)
        o.node(r * math.cos(a), r * math.sin(a),
               {"man_made": "mast", "tower:type": "communication",
                "communication:mobile_phone": "yes", "name": f"Sample Cell Tower {n + 1}"})

    # ---------- Health + emergency services ----------
    for name, (dx, dy) in [("Sample Regional Medical Center", (2, 3)),
                           ("Sample West End Hospital", (-16, 9)),
                           ("Sample Southside Hospital", (6, -18)),
                           ("Sample Hanover Hospital", (-5, 24)),
                           ("Sample Eastern Henrico Hospital", (17, 10)),
                           ("Sample Chesterfield Hospital", (-14, -14))]:
        o.polygon(dx, dy, 0.25, {"amenity": "hospital", "emergency": "yes", "name": name})
    for n in range(10):
        a, r = random.uniform(0, 2 * math.pi), random.uniform(3, 30)
        o.node(r * math.cos(a), r * math.sin(a),
               {"amenity": "fire_station", "name": f"Sample Fire Station {n + 1}"})
    for n in range(5):
        a, r = random.uniform(0, 2 * math.pi), random.uniform(2, 25)
        o.node(r * math.cos(a), r * math.sin(a),
               {"amenity": "police", "name": f"Sample Police Precinct {n + 1}"})
    for n, (dx, dy) in enumerate([(-9, 2), (11, -11)]):
        o.node(dx, dy, {"emergency": "ambulance_station", "name": f"Sample EMS Station {n + 1}"})

    o.write(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "richmond_sample.osm")
