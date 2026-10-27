#!/usr/bin/env python3
"""Bulb vase, built as a true solid with build123d. Exports STEP (for Shapr3D/Fusion) and STL
(for Wave Script).

    pip install build123d
    python examples/make_bulb_vase.py
    python examples/make_bulb_vase.py --height 120 --max-diameter 110 --opening 45

The profile is a smooth spline: narrow at the bottom, widest in the middle (or wherever
--bulge-at puts it), narrow again at the top. It's revolved around the Z axis into a solid,
so Wave Script uses its outside surface and Shapr3D imports it as an editable body.
"""
import argparse
from pathlib import Path

from build123d import Axis, BuildPart, BuildSketch, Plane, Polyline, Spline, BuildLine, export_step, export_stl, make_face, revolve

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--height", type=float, default=90.0, help="mm")
p.add_argument("--max-diameter", type=float, default=90.0, help="mm, widest point")
p.add_argument("--base", type=float, default=40.0, help="mm, bottom diameter")
p.add_argument("--opening", type=float, default=40.0, help="mm, top opening diameter")
p.add_argument("--bulge-at", type=float, default=0.5, help="where the widest point sits, 0 = bottom, 1 = top")
p.add_argument("--straight", type=float, default=3.0,
               help="mm of straight wall at the bottom before the curve starts (helps the first laps)")
p.add_argument("-o", "--output", type=Path, default=Path(__file__).parent / "bulb_vase")
a = p.parse_args()

H, rmax, rb, rt = a.height, a.max_diameter / 2, a.base / 2, a.opening / 2
zb = a.straight
zm = zb + (H - zb) * a.bulge_at

# outline points (radius, height): straight foot, then a smooth bulge
pts = [
    (rb, zb),
    (rb + (rmax - rb) * 0.62, zb + (zm - zb) * 0.40),
    (rmax, zm),
    (rt + (rmax - rt) * 0.62, zm + (H - zm) * 0.60),
    (rt, H),
]

with BuildPart() as vase:
    with BuildSketch(Plane.XZ):
        with BuildLine():
            Polyline((0, 0), (rb, 0), (rb, zb))               # axis -> foot
            Spline(*pts)                                         # the bulb
            Polyline((rt, H), (0, H), (0, 0))                    # top -> back down the axis
        make_face()
    revolve(axis=Axis.Z)

out = a.output
out.parent.mkdir(parents=True, exist_ok=True)
export_step(vase.part, str(out.with_suffix(".step")))
export_stl(vase.part, str(out.with_suffix(".stl")), tolerance=0.01, angular_tolerance=0.1)
bb = vase.part.bounding_box()
print(f"wrote {out.with_suffix('.step')} and {out.with_suffix('.stl')}")
print(f"size: {bb.size.X:.1f} x {bb.size.Y:.1f} x {bb.size.Z:.1f} mm")
