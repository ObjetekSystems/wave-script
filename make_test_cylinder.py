#!/usr/bin/env python3
"""Make simple test shapes for Wave Script.

    python make_test_cylinder.py                          # 60 mm wide x 60 mm tall cylinder
    python make_test_cylinder.py --diameter 80 --height 100
    python make_test_cylinder.py --flare 55               # flared vase for testing --adaptive

With --flare, the shape is straight for the bottom third, flares outward at the given angle
(degrees from vertical) through the middle third, then goes straight again.
"""
import argparse
import math
from pathlib import Path

import numpy as np
import trimesh

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--diameter", type=float, default=60.0, help="mm (base diameter)")
p.add_argument("--height", type=float, default=60.0, help="mm")
p.add_argument("--flare", type=float, default=0.0, help="flare angle in degrees from vertical (0 = plain cylinder)")
p.add_argument("-o", "--output", type=Path, help="output STL (default: examples/test_cylinder.stl or test_flare.stl)")
a = p.parse_args()

out = a.output or Path(__file__).parent / "examples" / ("test_flare.stl" if a.flare else "test_cylinder.stl")
r0, H = a.diameter / 2, a.height

if a.flare <= 0:
    mesh = trimesh.creation.cylinder(radius=r0, height=H, sections=256)
    mesh.apply_translation([0, 0, H / 2])   # sit on z = 0
else:
    slope = math.tan(math.radians(a.flare))
    z1, z2 = H / 3, 2 * H / 3

    def radius(z):
        return r0 + slope * (min(max(z, z1), z2) - z1)

    zs = np.linspace(0, H, 241)
    profile = np.array([[0, 0]] + [[radius(z), z] for z in zs] + [[0, H]])
    mesh = trimesh.creation.revolve(profile, sections=256)

out.parent.mkdir(parents=True, exist_ok=True)
mesh.export(out)
w = mesh.extents
print(f"wrote {out}  ({w[0]:.0f} x {w[1]:.0f} x {w[2]:.0f} mm)")
