#!/usr/bin/env python3
"""Wave Script torture tower: one part that stresses every hard case for the algorithm.

    python make_torture_tower.py            # writes torture_tower.step and torture_tower.stl
    python make_torture_tower.py --scale 0.8

Zones, bottom to top (heights at --scale 1):

  Z   0-12  Baseline       straight circle, 50 mm             (reference texture)
  Z  12-24  Gentle flare   circle 50 -> 64 mm, ~30 deg out    (adaptive should barely react)
  Z  24-38  Shape morph    circle -> rounded square, twisting (corners + changing outline)
  Z  38-52  Flower         6-lobe flower with inward dips     (concave sections, tight curves)
  Z  52-62  Steep neck     flower -> 26 mm circle, ~50 deg in (steep inward overhang)
  Z  62-74  Thin neck      26 mm circle                       (small perimeter, stretched waves)
  Z  74-84  Steep flare    26 -> 50 mm, ~50 deg out           (steep outward overhang)
  Z  84-90  Lip            straight 50 mm                     (top rim lands here)

Sharp kinks between zones are deliberate: they test the look-ahead and speed/layer ramps.
"""
import argparse
import math
from pathlib import Path

from build123d import (BuildLine, BuildPart, BuildSketch, Circle, Plane, Polyline,
                       RectangleRounded, export_step, export_stl, loft, make_face)

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--scale", type=float, default=1.0, help="scale the whole tower")
p.add_argument("-o", "--output", type=Path, default=Path(__file__).parent / "torture_tower")
a = p.parse_args()
k = a.scale


def flower(r_mean, depth, lobes=6, n=180):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        r = r_mean + depth * math.cos(lobes * t)
        pts.append((r * math.cos(t), r * math.sin(t)))
    return pts


def square(z, w, r, rot):
    s = Plane.XY.offset(z * k).rotated((0, 0, rot))
    with BuildSketch(s) as sk:
        RectangleRounded(w * k, w * k, r * k)
    return sk.sketch


def circle(z, d):
    with BuildSketch(Plane.XY.offset(z * k)) as sk:
        Circle(d / 2 * k)
    return sk.sketch


def flow(z, r, depth):
    with BuildSketch(Plane.XY.offset(z * k)) as sk:
        with BuildLine():
            Polyline(*[(x * k, y * k) for x, y in flower(r, depth)], close=True)
        make_face()
    return sk.sketch


zones = [
    [circle(0, 50), circle(12, 50)],                                   # baseline
    [circle(12, 50), circle(24, 64)],                                  # gentle flare
    [circle(24, 64), square(31, 58, 14, 15), square(38, 56, 10, 35)],  # morph + twist
    [square(38, 56, 10, 35), flow(45, 28, 3.5), flow(52, 28, 3.5)],    # flower
    [flow(52, 28, 3.5), circle(62, 26)],                               # steep neck in
    [circle(62, 26), circle(74, 26)],                                  # thin neck
    [circle(74, 26), circle(84, 50)],                                  # steep flare out
    [circle(84, 50), circle(90, 50)],                                  # lip
]

with BuildPart() as tower:
    for secs in zones:
        loft(secs, ruled=len(secs) == 2)

out = a.output
out.parent.mkdir(parents=True, exist_ok=True)
export_step(tower.part, str(out.with_suffix(".step")))
export_stl(tower.part, str(out.with_suffix(".stl")), tolerance=0.01, angular_tolerance=0.1)
bb = tower.part.bounding_box()
print(f"wrote {out.with_suffix('.step')} and {out.with_suffix('.stl')}")
print(f"size: {bb.size.X:.1f} x {bb.size.Y:.1f} x {bb.size.Z:.1f} mm")
