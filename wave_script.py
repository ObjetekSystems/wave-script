#!/usr/bin/env python3
"""
Wave Script - turn a simple CAD shape (STL) into FullControl-style wavy G-code.

No slicer needed: the script slices the model itself (with trimesh), traces the
outside of each layer, adds a sine wave to the outline, and spirals upward like
vase mode. Shifting the wave by half a wavelength every layer makes the strands
cross each other, giving the woven "lattice" look.

Quick start:
    python wave_script.py examples/test_cylinder.stl

Bambu printers (recommended path):
    1. Slice the SAME STL normally in Bambu Studio, then
       File > Export > Export plate sliced file  ->  template.gcode.3mf
    2. python wave_script.py examples/test_cylinder.stl --bambu-template template.gcode.3mf
    3. Open the new *_wave.gcode.3mf in Bambu Studio and send it to the printer.

Run `python wave_script.py --help` for every setting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
import trimesh
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.polygon import orient

__version__ = "0.7.2"

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    # wave
    amplitude: float = 1.0        # mm, how far the wall moves in/out from the CAD surface
    wavelength: float = 5.0       # mm, target length of one wave (rounded to fit the outline)
    waves: int | None = None      # exact number of waves per lap (overrides wavelength)
    phase_shift: float = 0.5      # fraction of a wave to shift each layer (0.5 = woven, 0 = ribbed)
    # printing
    layer_height: float = 0.2
    line_width: float = 0.5
    filament_diameter: float = 1.75
    flow: float = 1.0             # extrusion multiplier
    base_layers: int = 3          # solid flat bottom layers (0 = open bottom, e.g. lamp shade)
    point_spacing: float = 0.3    # mm between points along the outline
    adaptive: bool = False        # thinner layers + slower on steep walls
    max_overhang: float = 0.15    # mm a lap may stick out past the one below
    min_layer_height: float = 0.08
    densify_height: float = 2.0   # mm below the top where an open weave closes its gaps (0 = off)
    valley_depth: float = 0.0     # mm the top edge dips between crossings (measured with no rim)
    valley_slope: float = 1.3     # steepest dip into a valley (rise over run)
    aux_fan: float = 0.0          # Bambu side (auxiliary) fan 0..1; Bambu's start G-code turns it on
    rim_fan: float = 1.0          # part-fan level while closing an open weave and printing the rim
    weave_height: int = 1         # laps stacked on each side before the strands cross (taller loops)
    top_layers: int = 2           # solid rim laps on top so the weave can't unravel (0 = none)
    tracking: bool = True         # carry each wave's position up from the lap below
    stagger: int = 0              # laps per row of loops; rows shift half a loop (0 = off)
    weave_gap: float | None = None        # mm open gap between strands (see-through weave)
    strand_diameter: float | None = None  # mm round strand size for the open weave
    speed: float = 25.0           # mm/s for the wavy wall
    base_speed: float = 30.0      # mm/s for the solid base
    first_layer_speed: float = 15.0
    travel_speed: float = 150.0
    retract: float = 0.8          # mm
    fan_layer: int = 2            # turn part fan on from this layer (1-based)
    nozzle_temp: int = 215        # generic printers only (Bambu uses the template's temps)
    bed_temp: int = 60
    # placement
    bed_x: float = 256.0
    bed_y: float = 256.0
    center_x: float | None = None
    center_y: float | None = None
    max_height: float | None = None
    flip: bool = False            # print the model upside down
    # output
    flavor: str = "generic"       # generic | bambu
    start_gcode: Path = HERE / "profiles" / "generic_start.gcode"
    end_gcode: Path = HERE / "profiles" / "generic_end.gcode"
    filament_density: float = 1.24  # g/cm^3 (PLA)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def load_mesh(path: Path, s: Settings) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(str(path), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        sys.exit(f"Could not read a 3D mesh from {path}")
    if not mesh.is_watertight:
        print("  ! Warning: the mesh is not watertight; slices may have gaps.")
    if s.flip:
        # turn the model upside down (180 degrees around the X axis)
        mesh.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0]))
    lo, hi = mesh.bounds
    cx = s.center_x if s.center_x is not None else s.bed_x / 2
    cy = s.center_y if s.center_y is not None else s.bed_y / 2
    # sit on the bed (z=0) and center on the plate
    mesh.apply_translation([cx - (lo[0] + hi[0]) / 2, cy - (lo[1] + hi[1]) / 2, -lo[2]])
    return mesh


_warned_multi = False


def outline_at(mesh: trimesh.Trimesh, z: float) -> Polygon | None:
    """Return the largest outside outline of the model at height z (CCW)."""
    global _warned_multi
    path = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if path is None:
        return None
    planar, to_3d = path.to_2D()
    polys = []
    # polygons_closed: every closed loop on its own (no rtree needed); holes are merged away below
    for poly in planar.polygons_closed:
        if poly is None:
            continue
        xy = np.asarray(poly.exterior.coords)
        pts = np.column_stack([xy, np.zeros(len(xy)), np.ones(len(xy))]) @ to_3d.T
        polys.append(Polygon(pts[:, :2]))
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    polys = [p for p in polys if not p.is_empty and p.area > 1.0]
    if not polys:
        return None
    # mesh seams (e.g. where two lofted sections meet) can produce overlapping outlines for
    # what is really one shape; merge them before deciding whether there are separate parts
    merged = shapely.union_all(polys)
    pieces = list(merged.geoms) if isinstance(merged, MultiPolygon) else [merged]
    polys = [Polygon(g.exterior) for g in pieces if g.area > 1.0]
    if not polys:
        return None
    if len(polys) > 1 and not _warned_multi:
        print("  ! Warning: the model has more than one separate outline on some layers;"
              " only the largest is printed.")
        _warned_multi = True
    return orient(max(polys, key=lambda p: p.area), sign=1.0)


SEAM_ANGLE = 0.0   # degrees; where the first lap starts (0 = +X / right, 90 = back, 180 = left)


def resample_ring(poly: Polygon, n: int, start_hint: np.ndarray | None = None) -> np.ndarray:
    """n evenly spaced points around the outline.

    The lap starts at the point closest to `start_hint` (the previous lap's start), so the
    seam where one lap hands over to the next stays in one place even when the outline
    twists. Without a hint it starts at SEAM_ANGLE (default +X, the right side).
    """
    ring = LineString(poly.exterior.coords)
    length = ring.length
    if start_hint is not None:
        s0 = ring.project(Point(start_hint[0], start_hint[1]))
    else:
        c = poly.centroid
        ang = math.radians(SEAM_ANGLE)
        s0 = ring.project(Point(c.x + 10_000 * math.cos(ang), c.y + 10_000 * math.sin(ang)))
    d = (s0 + length * np.arange(n) / n) % length
    pts = shapely.get_coordinates(shapely.line_interpolate_point(ring, d))
    return pts


def outward_normals(pts: np.ndarray) -> np.ndarray:
    """Outward unit normals for a CCW closed loop of points."""
    tang = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
    norm = np.column_stack([tang[:, 1], -tang[:, 0]])
    lens = np.linalg.norm(norm, axis=1, keepdims=True)
    lens[lens == 0] = 1
    return norm / lens


# --------------------------------------------------------------------------- #
# Toolpath
# --------------------------------------------------------------------------- #
@dataclass
class Move:
    x: float
    y: float
    z: float
    e_scale: float     # 0 = travel, otherwise fraction of a full-height line
    speed: float       # mm/s
    layer: int         # 1-based layer number
    kind: str          # "base" | "wall" | "travel"
    fan: float | None = None   # part-fan level 0..1 from this move on (None = unchanged)


@dataclass
class Toolpath:
    moves: list[Move] = field(default_factory=list)
    layers: int = 0
    waves_per_lap: float = 0.0
    steep: list = field(default_factory=list)


def build_base(mesh: trimesh.Trimesh, s: Settings, tp: Toolpath) -> None:
    """Solid flat bottom made of concentric rings, outside to inside."""
    h, w = s.layer_height, s.line_width
    for layer in range(1, s.base_layers + 1):
        z = layer * h
        poly = outline_at(mesh, z - h / 2)
        if poly is None:
            sys.exit("The model has no outline near the bed. Is it lying flat?")
        speed = s.first_layer_speed if layer == 1 else s.base_speed
        k = 0
        while True:
            inner = poly.buffer(-(w / 2 + k * w), join_style="mitre")
            if inner.is_empty:
                break
            geoms = inner.geoms if isinstance(inner, MultiPolygon) else [inner]
            for g in geoms:
                g = orient(g, sign=1.0)
                n = max(8, int(g.exterior.length / s.point_spacing))
                pts = resample_ring(g, n)
                tp.moves.append(Move(*pts[0], z, 0.0, s.travel_speed, layer, "travel"))
                for p in list(pts[1:]) + [pts[0]]:
                    tp.moves.append(Move(*p, z, 1.0, speed, layer, "base"))
            k += 1
    tp.layers = s.base_layers


def wall_slope(mesh: trimesh.Trimesh, z: float, dz: float, top: float) -> float:
    """Steepest lean of the wall between z and z + dz, as horizontal mm per vertical mm.

    Compares the outline at two heights and finds how far the upper outline sits from the
    lower one at the worst point around the shape. Works for walls leaning out or in.
    0 = straight up, 1 = 45 degrees, 1.73 = 60 degrees.
    """
    z_hi = min(z + dz, top - 1e-3)
    if z_hi <= z + 1e-3:
        return 0.0
    lo = outline_at(mesh, z)
    hi = outline_at(mesh, z_hi)
    if lo is None or hi is None:
        return 0.0
    pts = resample_ring(lo, 180)
    dist = shapely.distance(shapely.points(pts), hi.exterior)
    return float(np.max(dist)) / (z_hi - z)


def build_wall(mesh: trimesh.Trimesh, s: Settings, tp: Toolpath) -> None:
    """Wavy single wall that spirals upward (vase mode)."""
    h = s.layer_height
    hw, wall_ratio = wall_geometry(s)    # wall rise per lap, extrusion vs. a normal line
    open_weave = s.weave_gap is not None or s.strand_diameter is not None
    top = mesh.bounds[1][2]
    if s.max_height:
        top = min(top, s.max_height)
    z0 = (s.base_layers + 1) * h

    first = outline_at(mesh, max(z0 - h / 2, h / 2))
    if first is None:
        sys.exit("Could not find the model outline above the base.")
    if s.waves:
        n_waves = s.waves
    else:
        n_waves = max(3, round(first.exterior.length / s.wavelength))
    W = n_waves + s.phase_shift          # wave count per lap including the per-layer shift
    tp.waves_per_lap = W

    # Finishing the top: first a few "closing" laps that stop the weave crossing and shrink
    # the wave a little each lap, so every lap sits right on top of the one below; then the
    # flat rim laps. Going straight from an open weave to a flat rim leaves the rim bridging
    # between crossings, and it can pull loose as it cools.
    open_top = (s.weave_gap is not None or s.strand_diameter is not None) and s.top_layers > 0 \
        and s.densify_height > 0
    # 0.6.6: the closing laps always start from the full wave (as in 0.6.1, the best rim in
    # testing). Shrinking the wave inside the densify band (0.6.4/0.6.5) made the top come loose.
    close_from = s.amplitude
    n_close = math.ceil(close_from / CLOSE_STEP) if s.top_layers > 0 else 0
    # valley fill (0.7.0, from Weave Script test 15L5): open strands sag between crossings, so
    # the top edge is peaks and valleys and a flat rim only touches the peaks. The closing
    # laps start down in each valley and level the top; the densify band is skipped.
    valley = s.valley_depth if (open_weave and s.top_layers > 0) else 0.0
    if valley > 0:
        n_close = max(n_close, math.ceil(valley / VALLEY_STEP))
    fill = ValleyFill(valley, n_close)
    # open weaves: closing and rim laps as wide as the strands
    rim_w = max(s.line_width, s.strand_diameter or s.line_width) if open_weave else s.line_width
    if valley > 0:
        rim_w = max(rim_w, VALLEY_WIDTH)   # as on the lattice that held (Weave Script test 15L5)
    rim_h = (max(0, s.top_layers) + n_close) * h   # room saved at the top
    lap = 0
    last_weave_lap = 0
    rim_lap = 0                            # 0 = still weaving, 1..n_close = closing, then rim
    z_prev_end = z0
    prev_rise = hw
    # Open weaves: over the last few mm before the top, shrink the gap to zero so the weave
    # becomes a dense, fully bonded band (like the solid weave) before it closes into the rim.
    # A solid rim sitting straight on an open weave only touches it at the crossings and can
    # curl or lift as it cools.
    dense_len = s.densify_height if (open_weave and s.top_layers > 0 and valley <= 0) else 0.0
    z_dense = top - rim_h - dense_len
    dense_f = 0.0
    # Weaves that cross on every lap (weave height 1) have no lap stacked on the one below, so
    # the rim peeled off chain mail as one bundle (Weave Script tests). In the densify band they
    # switch to stacked pairs (weave height 2) with the stagger frozen: the structure that held
    # in test 15 and in the Weave Script control print.
    cross_every = open_weave and max(1, s.weave_height) == 1
    freeze = None                          # lap where the densify band starts
    last_rise = 0.0                        # how much the previous lap rose (lap 0 is flat)
    last_factor = 1.0                      # adaptive speed factor of the previous lap
    prev_pts = None                        # previous lap's outline points ...
    prev_w = None                          # ... and the wave position at each of them
    prev_xy = None                         # previous lap's actual path
    steep = []                             # (z, angle) where sagging is likely
    while True:
        # how far this lap rises: normal, adaptive (thinner on steep walls) or rim
        slope = 0.0
        if lap == 0:
            rise = 0.0
        elif rim_lap:
            rise = h
        else:
            slope = wall_slope(mesh, z_prev_end, max(1.0, 3 * hw), top)
            rise = hw
            if dense_len > 0 and z_prev_end >= z_dense:
                # close the gap and blend to solid lines over the whole band (0.6.2-0.6.3)
                dense_f = min(1.0, max(0.0, (z_prev_end - z_dense) / dense_len))
                if cross_every and freeze is None:
                    freeze = lap
                rise = hw + (h - hw) * dense_f
            if s.adaptive and not open_weave and slope > 1e-6:
                target = min(h, max(s.min_layer_height, s.max_overhang / slope))
                rise = min(target, prev_rise * 1.15)   # thin down at once, grow back gently
        z_start = z_prev_end
        z_end = z0 if lap == 0 else z_start + rise
        if not rim_lap and lap > 0 and z_end > top - rim_h + 1e-6:
            if s.top_layers <= 0 or lap < 2:
                break
            rim_lap = 1                    # switch to closing/rim and redo this lap
            last_weave_lap = lap - 1
            continue
        if rim_lap > n_close + s.top_layers:
            break
        sample_h = rise if rise > 0 else h
        poly = outline_at(mesh, max(h / 2, min(z_end - sample_h / 2, top - 1e-3)))
        if poly is None:
            break
        n = max(16, int(poly.exterior.length / s.point_spacing))
        pts = resample_ring(poly, n, prev_pts[0] if prev_pts is not None else None)
        nrm = outward_normals(pts)
        t = np.arange(n) / n
        amp = np.full(n, s.amplitude)
        if rim_lap and rim_lap <= n_close:
            amp = close_from * (1 - (rim_lap - 1 + t) / n_close)   # shrink a little per lap
        elif rim_lap:
            amp = np.zeros(n)
        # wave position around the lap (0..1). Without tracking it's simply the fraction of
        # the way around this lap's outline. With tracking, each point takes the position of
        # the nearest point on the lap below, so waves stay stacked even when the outline
        # morphs, twists or changes width.
        w = t
        if s.tracking and prev_pts is not None:
            w = track_wave_position(pts, prev_pts, prev_w, t)
        prev_pts, prev_w = pts, w
        # the per-layer shift and stagger follow the wave position too, so they line up
        # with the laps below instead of with this lap's start point
        if rim_lap:
            # closing/rim: repeat the last weave lap's pattern so each lap stacks exactly on
            # the one below. A spiral weave doesn't repeat exactly lap to lap, so the small
            # phase jump at the lap start is blended in over one wavelength.
            L = last_weave_lap

            def phase_at(ww):
                return (n_waves * (L + ww) + band_shift(s, L, ww, n_waves, freeze)
                        + stagger_offset(s, L, ww, n_waves, freeze))
            ends = phase_at(np.array([0.0, 1.0]))
            jump = (ends[1] - ends[0] + 0.5) % 1.0 - 0.5
            phase = phase_at(w) + jump * (1 - np.clip(w / (1.0 / n_waves), 0, 1))
        else:
            shift = band_shift(s, lap, w, n_waves, freeze) + stagger_offset(s, lap, w, n_waves, freeze)
            phase = n_waves * (lap + w) + shift
        offset = amp * np.sin(2 * math.pi * phase)
        xy = pts + nrm * offset[:, None]
        layer_no = s.base_layers + 1 + lap
        speed = s.first_layer_speed if layer_no == 1 else s.speed

        # overhang this lap: how far it sticks out past the lap below
        overhang = rise * slope
        if not rim_lap and lap > 0 and overhang > s.max_overhang * 1.05:
            steep.append((z_end, math.degrees(math.atan(slope))))
        if s.adaptive and not rim_lap and lap > 0:
            # slow down on steep laps so each strand has longer to cool
            speed_factor = 1.0
            if slope > 0:
                lean = min(1.0, (slope * h) / max(s.max_overhang, 1e-6))   # 0 = straight
                speed_factor = 1.0 - 0.4 * max(0.0, lean - 0.5) / 0.5      # down to 60%
            if overhang > s.max_overhang:
                speed_factor = min(speed_factor, max(0.5, s.max_overhang / overhang))
            # slow down at once, but speed back up gently (max 8% per lap) so the flow
            # doesn't jump where a steep section turns vertical
            speed_factor = min(max(0.5, speed_factor), last_factor * 1.08)
            last_factor = speed_factor
            speed *= speed_factor

        if lap == 0:
            tp.moves.append(Move(*xy[0], z_end, 0.0, s.travel_speed, layer_no, "travel"))
            z = np.full(n, z_end)
            e = np.ones(n)
        else:
            z = z_start + t * rise
            # the real gap under this lap: in a spiral it blends from the previous lap's rise
            # to this lap's rise (lap 1 starts at zero because lap 0 is flat)
            gap = last_rise + (rise - last_rise) * t
            if rim_lap or not open_weave:
                # flat lines that exactly fill the gap down to the lap below. This matters
                # for the rim: after an open weave the first rim lap sits up to a full weave
                # rise above the strands, and a normal-height line wouldn't reach them.
                lw = rim_w if rim_lap else s.line_width
                vf = fill.lap(rim_lap, w, t) if rim_lap else None
                if vf is not None:
                    z = z - vf[0]
                    gap = gap + vf[1]
                e = np.array([line_area(lw, g) for g in gap]) / mm3_per_mm(s)
            else:
                ratio = wall_ratio
                if dense_f > 0:
                    # blend from the open-weave strand to a normal line filling the gap
                    solid = np.array([line_area(s.line_width, g) for g in gap]) / mm3_per_mm(s)
                    ratio = wall_ratio * (1 - dense_f) + solid * dense_f
                e = (t if lap == 1 else np.ones(n)) * ratio
            if rim_lap:
                speed = min(speed, 15.0)   # slow rim so it bonds to the crossings below
            if open_weave and (rim_lap or dense_f > 0):
                speed = min(speed, VALLEY_SPEED if valley > 0 else 12.0)   # open weaves: tested speeds
        if open_weave and valley > 0 and not rim_lap and lap > 0 and prev_xy is not None:
            fill.note(sag_profile(xy, prev_xy, w, s.strand_diameter or s.line_width,
                                  valley, s.valley_slope))
        prev_xy = xy
        start = 1 if lap == 0 else 0
        # open weaves: part fan at --rim-fan for the densify band, closing laps and rim
        fan = s.rim_fan if (open_weave and s.rim_fan < 1.0 and (rim_lap or dense_f > 0)) else None
        for i in range(start, n):
            tp.moves.append(Move(xy[i, 0], xy[i, 1], z[i], float(e[i]), speed, layer_no, "wall",
                                 fan if i == start else None))
        lap += 1
        z_prev_end = z_end
        last_rise = rise
        if rise > 0 and not rim_lap:
            prev_rise = rise
        if rim_lap:
            rim_lap += 1
    tp.layers = s.base_layers + lap
    tp.steep = steep


def support_distance(xy: np.ndarray, prev_xy: np.ndarray) -> np.ndarray:
    """For each point of a lap, the sideways distance (mm) to the lap printed below it.

    Small = the strand rests on the one below; more than about half a strand = it's bridging.
    Used by Weave Script (weave_script.py) to slow down and add flow on bridged spans.
    """
    ring = LineString(np.vstack([prev_xy, prev_xy[:1]]))
    return shapely.distance(shapely.points(xy), ring)


CLOSE_STEP = 0.15   # mm the wave may shrink per lap while closing the top
VALLEY_STEP = 0.2   # mm of valley depth taken out per closing lap (bead at most ~0.4 mm tall)
VALLEY_WIDTH = 0.6  # mm, narrowest closing/rim line with valley fill (Weave Script tests 15L5, 18)
VALLEY_SPEED = 10.0 # mm/s, fastest closing/rim speed with valley fill


def sag_profile(xy: np.ndarray, prev_xy: np.ndarray, w: np.ndarray, strand: float,
                valley: float, slope: float):
    """Where a lap bridges between crossings, as a sag profile along the wave position.

    Returns (positions 0..1 sorted, profile, capped profile) or None when the lap rests on
    the one below everywhere or nowhere. The profile is 0 at the crossings and 1 mid-span (a
    sine over each bridged span). The capped profile keeps a dip of `valley` mm no steeper
    than `slope` (rise over run), so the nozzle clears the peaks on either side.
    """
    n = len(xy)
    unsup = support_distance(xy, prev_xy) > strand / 2
    if not unsup.any() or unsup.all():
        return None
    prof = np.zeros(n)
    cap = np.zeros(n)
    step = float(np.mean(np.linalg.norm(np.diff(xy, axis=0), axis=1)))
    k0 = int(np.argmin(unsup))                   # start the scan on a supported point
    idx = (np.arange(n) + k0) % n
    run = []
    for i in list(idx) + [idx[0]]:
        if unsup[i] and i not in run:
            run.append(i)
        elif run:
            m = len(run)
            prof[run] = np.sin(math.pi * (np.arange(m) + 0.5) / m)
            cap[run] = prof[run] * min(1.0, slope * m * step / (math.pi * max(valley, 1e-6)))
            run = []
    ww = np.mod(w, 1.0)
    order = np.argsort(ww)
    return ww[order], prof[order], cap[order]


class ValleyFill:
    """Closing laps that start down in each valley of a sagging open weave and level the top.

    Lap k of n sits `valley * profile * f` below its normal height (f goes 1 -> 0), and its
    bead is thicker by however much deeper the point below it sat.
    """

    def __init__(self, valley: float, n_close: int):
        self.valley, self.n_close = valley, n_close
        self.sag = None            # latest sag_profile() of a weave lap
        self.dip_below = None

    def note(self, sag):
        if sag is not None:
            self.sag = sag

    def lap(self, rim_lap: int, w: np.ndarray, t: np.ndarray):
        """(dip, extra bead height) per point for closing lap `rim_lap`, or None."""
        if self.valley <= 0 or self.sag is None or not 1 <= rim_lap <= self.n_close:
            return None
        sw, prof, cap = self.sag
        ww = np.mod(w, 1.0)
        f = 1 - (rim_lap - 1 + t) / self.n_close
        dip = self.valley * np.interp(ww, sw, cap, period=1.0) * f
        if rim_lap == 1:
            below = self.valley * np.interp(ww, sw, prof, period=1.0)
        elif self.dip_below is None or len(self.dip_below) != len(t):
            below = dip + self.valley / self.n_close
        else:
            below = self.dip_below
        self.dip_below = dip
        return dip, np.maximum(0.0, below - dip)


def track_wave_position(pts: np.ndarray, prev_pts: np.ndarray, prev_w: np.ndarray,
                        t: np.ndarray) -> np.ndarray:
    """Wave position for each point of a new lap, inherited from the nearest point below.

    Positions are fractions of a lap (0..1). The result is kept close to `t` (the plain
    fraction around this lap) by unwrapping, and lightly smoothed so tiny mesh facets don't
    add jitter.
    """
    ring = np.vstack([prev_pts, prev_pts[:1]])
    seg = np.linalg.norm(np.diff(ring, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    w_ext = np.concatenate([prev_w, [prev_w[0] + 1.0]])
    # keep prev_w increasing along the ring so interpolation between vertices is sane
    w_ext = prev_w[0] + np.unwrap((w_ext - prev_w[0]) * 2 * math.pi) / (2 * math.pi)
    line = LineString(ring)
    s = shapely.line_locate_point(line, shapely.points(pts))
    w_raw = np.interp(s, cum, w_ext)
    d = (w_raw - t + 0.5) % 1.0 - 0.5          # offset from the plain position, -0.5..0.5
    d = np.unwrap(d * 2 * math.pi) / (2 * math.pi)
    k = 7                                       # circular moving average
    pad = np.concatenate([d[-k:], d, d[:k]])
    d = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="same")[k:-k]
    return t + d


def flip_shift(s: Settings, lap: int, t: np.ndarray, n_waves: int) -> np.ndarray:
    """Total wave shift (in waves) from --phase-shift, applied once every `weave_height` laps.

    weave_height 1: the wave moves phase_shift every lap (the classic weave).
    weave_height N: N laps stack right on top of each other, then the next lap jumps to the
    other side and crosses the lap below it (blended in over one wavelength). The loops
    become N times taller, and every lap still rests on the one below it.
    """
    k = max(1, s.weave_height)
    if lap == 0:
        return np.zeros_like(t)
    if k == 1:
        return s.phase_shift * (lap - 1 + t)
    now = s.phase_shift * (lap // k)
    if lap % k != 0:
        return np.full_like(t, now)
    before = s.phase_shift * (lap // k - 1)
    ramp = 1.0 / n_waves
    return before + (now - before) * np.clip(t / ramp, 0, 1)


def band_shift(s, lap: int, t: np.ndarray, n_waves: int, band: int | None = None) -> np.ndarray:
    """flip_shift, except that a weave crossing on every lap switches to stacked pairs from
    lap `band` on (the densify band under the rim): one lap stacks on the lap below, the next
    crosses, and so on. It carries on smoothly from the lap before the band."""
    if band is None or lap < band or max(1, s.weave_height) != 1:
        return flip_shift(s, lap, t, n_waves)
    m = lap - band
    before = s.phase_shift * (band - 1) + s.phase_shift * (m // 2)
    if m % 2 == 0:
        return np.full_like(t, before, dtype=float)       # stack on the lap below
    return before + s.phase_shift * np.clip(t * n_waves, 0, 1)   # cross, blended in


def stagger_offset(s: Settings, lap: int, t: np.ndarray, n_waves: int,
                   freeze: int | None = None) -> np.ndarray:
    """Extra wave shift (in waves) that offsets every other row of loops by half a loop.

    Loops form between crossing points, which are half a wave apart. Shifting the wave by a
    quarter wave moves the crossings halfway between the old ones, so each row of loops sits
    between the loops of the row below (chain-mail / brick look). The shift is blended in over
    one wavelength at the start of the lap so there's no sudden jump in the path.
    """
    if s.stagger <= 0:
        return np.zeros_like(t)
    if freeze is not None and lap >= freeze:
        # densify band: keep the offset of the lap just before the band
        return np.full_like(t, 0.25 * (((freeze - 1) // s.stagger) % 2), dtype=float)
    now = 0.25 * ((lap // s.stagger) % 2)
    before = 0.25 * (((lap - 1) // s.stagger) % 2) if lap > 0 else now
    if now == before:
        return np.full_like(t, now)
    ramp = 1.0 / n_waves
    k = np.clip(t / ramp, 0, 1)
    return before + (now - before) * k


def build_toolpath(mesh: trimesh.Trimesh, s: Settings) -> Toolpath:
    tp = Toolpath()
    if s.base_layers > 0:
        build_base(mesh, s, tp)
    build_wall(mesh, s, tp)
    return tp


# --------------------------------------------------------------------------- #
# G-code
# --------------------------------------------------------------------------- #
def wall_geometry(s: Settings) -> tuple[float, float]:
    """How far the wall rises per lap, and its extrusion relative to a normal line.

    Normal mode: rise = layer height, normal flat line.
    Open weave: strands are round (strand_diameter). Strands one lap apart touch only where
    they cross; strands two laps apart sit right above each other with `weave_gap` of open
    space between them, so the rise per lap is (strand + gap) / 2.
    """
    if s.weave_gap is None and s.strand_diameter is None:
        return s.layer_height, 1.0
    d = s.strand_diameter or s.line_width
    rise = (d + s.weave_gap) / 2 if s.weave_gap else s.layer_height
    strand_area = math.pi * d ** 2 / 4
    return rise, strand_area / mm3_per_mm(s)


def line_area(w: float, h: float) -> float:
    """Cross-section of a line (rounded-rectangle model used by Orca/Bambu)."""
    return (w - h) * h + math.pi * (h / 2) ** 2


def mm3_per_mm(s: Settings) -> float:
    """Cross-section of a normal line at the normal layer height."""
    return line_area(s.line_width, s.layer_height)


def write_body(tp: Toolpath, s: Settings) -> tuple[list[str], dict]:
    fil_area = math.pi * (s.filament_diameter / 2) ** 2
    e_per_mm = mm3_per_mm(s) / fil_area * s.flow
    fan = "M106 P1 S255" if s.flavor == "bambu" else "M106 S255"
    fan_off = "M106 P1 S0" if s.flavor == "bambu" else "M107"

    out = [
        f"; ---- Wave Script {__version__} toolpath ----",
        "G90 ; absolute XYZ",
        "M83 ; relative extrusion",
        fan_off,
    ]
    if s.flavor == "bambu":
        # Bambu's start G-code switches on the side (auxiliary) fan. It blows from one side
        # only, so a thin wall cools unevenly and the top ring can lift where it hits hardest.
        out.append(f"M106 P2 S{int(round(255 * max(0.0, min(1.0, s.aux_fan))))} ; side fan")
    x = y = None
    z = 0.0
    cur_layer = 0
    total_e = 0.0
    total_time = 0.0
    last_kind = None
    last_pct = -1
    n_moves = len(tp.moves)

    for i, m in enumerate(tp.moves):
        if m.layer != cur_layer:
            cur_layer = m.layer
            out += ["; CHANGE_LAYER", f"; Z_HEIGHT: {m.z:.3f}",
                    f"; LAYER_HEIGHT: {s.layer_height:.3f}", f";LAYER:{cur_layer - 1}"]
            if cur_layer == s.fan_layer:
                out.append(fan)
        if m.fan is not None and cur_layer >= s.fan_layer:
            level = int(round(255 * max(0.0, min(1.0, m.fan))))
            out.append(f"M106 P1 S{level}" if s.flavor == "bambu" else f"M106 S{level}")
        if m.kind != last_kind and m.kind != "travel":
            feat = "Bottom surface" if m.kind == "base" else "Outer wall"
            out += [f"; FEATURE: {feat}", f"; LINE_WIDTH: {s.line_width:.3f}"]
            last_kind = m.kind

        if s.flavor == "bambu":
            pct = int(100 * i / n_moves)
            if pct != last_pct:
                out.append(f"M73 P{pct}")
                last_pct = pct

        if m.kind == "travel":
            out.append(f"G1 E-{s.retract:.2f} F2400")
            out.append(f"G1 Z{max(z, m.z) + 1.0:.3f} F1200")
            out.append(f"G1 X{m.x:.3f} Y{m.y:.3f} F{s.travel_speed * 60:.0f}")
            out.append(f"G1 Z{m.z:.3f} F1200")
            out.append(f"G1 E{s.retract:.2f} F2400")
            if x is not None:
                total_time += math.hypot(m.x - x, m.y - y) / s.travel_speed
            x, y, z = m.x, m.y, m.z
            last_kind = "travel"
            continue

        dist = math.sqrt((m.x - x) ** 2 + (m.y - y) ** 2 + (m.z - z) ** 2) if x is not None else 0
        e = dist * e_per_mm * m.e_scale
        total_e += e
        total_time += dist / m.speed
        zpart = f" Z{m.z:.3f}" if abs(m.z - z) > 1e-5 else ""
        out.append(f"G1 X{m.x:.3f} Y{m.y:.3f}{zpart} E{e:.5f} F{m.speed * 60:.0f}")
        x, y, z = m.x, m.y, m.z

    out += [f"G1 E-{s.retract:.2f} F2400", f"G1 Z{z + 5:.3f} F1200", fan_off,
            "; ---- end Wave Script toolpath ----"]
    grams = total_e * fil_area / 1000 * s.filament_density
    stats = {"filament_m": total_e / 1000, "grams": grams, "minutes": total_time / 60,
             "height": z, "layers": tp.layers}
    return out, stats


def fill_template(text: str, s: Settings) -> str:
    return (text.replace("{nozzle_temp}", str(s.nozzle_temp))
                .replace("{bed_temp}", str(s.bed_temp)))


def write_generic(body: list[str], s: Settings, out_path: Path, stats: dict) -> None:
    start = fill_template(s.start_gcode.read_text(), s)
    end = fill_template(s.end_gcode.read_text(), s)
    header = [
        f"; generated by Wave Script {__version__}",
        f"; amplitude={s.amplitude} wavelength={s.wavelength} phase_shift={s.phase_shift}",
        f"; layer_height={s.layer_height} line_width={s.line_width}",
        f"; estimated filament: {stats['filament_m']:.2f} m / {stats['grams']:.1f} g",
        f"; estimated print time: {stats['minutes']:.0f} min",
    ]
    out_path.write_text("\n".join(header) + "\n" + start.rstrip() + "\n"
                        + "\n".join(body) + "\n" + end.rstrip() + "\n")


# Bambu Studio marks where its machine start / end G-code begin and end.
START_MARKERS = ["MACHINE_START_GCODE_END"]
END_MARKERS = ["MACHINE_END_GCODE_START", "; filament end gcode", ";filament end gcode"]
SPIRAL_OFF = "; spiral_mode = 0"
SPIRAL_ON = "; spiral_mode = 1"


def write_bambu(body: list[str], template: Path, out_path: Path) -> None:
    """Swap our toolpath into a .gcode.3mf that Bambu Studio sliced for this printer."""
    with zipfile.ZipFile(template) as zin:
        names = zin.namelist()
        gname = next((n for n in names if n.startswith("Metadata/plate_") and n.endswith(".gcode")), None)
        if gname is None:
            sys.exit("The template has no sliced G-code inside. In Bambu Studio use "
                     "File > Export > Export plate sliced file (.gcode.3mf).")
        lines = zin.read(gname).decode("utf-8", errors="replace").splitlines()

        def find(markers, start=0):
            for i in range(start, len(lines)):
                low = lines[i].lower()
                if any(mk.lower() in low for mk in markers):
                    return i
            return None

        i_start = find(START_MARKERS)
        i_end = find(END_MARKERS, (i_start or 0) + 1)
        if i_start is None or i_end is None:
            sys.exit("Could not find Bambu's start/end G-code markers in the template "
                     f"({START_MARKERS[0]} / {END_MARKERS[0]}). Please open an issue and attach the file.")

        # Tell Bambu Studio's previewer this is a spiral (vase) print. Without it, every tiny
        # Z step of the spiral counts as a new "layer" and the preview takes forever to load.
        head = [SPIRAL_ON if ln.strip() == SPIRAL_OFF else ln for ln in lines[: i_start + 1]]
        tail = [SPIRAL_ON if ln.strip() == SPIRAL_OFF else ln for ln in lines[i_end:]]
        new_gcode = "\n".join(head + body + tail) + "\n"
        data = new_gcode.encode("utf-8")
        md5_name = gname + ".md5"

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == gname:
                    zout.writestr(item, data)
                elif item.filename == "Metadata/project_settings.config":
                    raw = zin.read(item.filename)
                    try:
                        cfg = json.loads(raw)
                        if "spiral_mode" in cfg:
                            cfg["spiral_mode"] = "1"
                        raw = json.dumps(cfg, indent=4).encode("utf-8")
                    except ValueError:
                        pass
                    zout.writestr(item, raw)
                elif item.filename == md5_name:
                    old = zin.read(md5_name).decode().strip()
                    digest = hashlib.md5(data).hexdigest()
                    zout.writestr(item, digest.upper() if old.isupper() else digest)
                else:
                    zout.writestr(item, zin.read(item.filename))


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def save_preview(tp: Toolpath, s: Settings, out_path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wall = np.array([[m.x, m.y, m.z] for m in tp.moves if m.kind == "wall"])
    base = np.array([[m.x, m.y, m.z] for m in tp.moves if m.kind == "base"])
    fig = plt.figure(figsize=(16, 12))

    ax = fig.add_subplot(2, 2, 1, projection="3d")
    step = max(1, len(wall) // 60000)
    ax.plot(wall[::step, 0], wall[::step, 1], wall[::step, 2], lw=0.35, color="#d9622b")
    if len(base):
        ax.plot(base[::4, 0], base[::4, 1], base[::4, 2], lw=0.3, color="#888888")
    ax.view_init(elev=12, azim=-60)       # low angle so narrow sections aren't hidden
    ax.set_title("Toolpath (3D)")
    rng = np.ptp(wall, axis=0).max() / 2
    mid = (wall.max(axis=0) + wall.min(axis=0)) / 2
    ax.set_xlim(mid[0] - rng, mid[0] + rng)
    ax.set_ylim(mid[1] - rng, mid[1] + rng)
    ax.set_zlim(0, 2 * rng)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")

    # side close-up: one patch of wall seen from the +X side, showing the weave
    ax2 = fig.add_subplot(2, 2, 2)
    cx, cy = mid[0], mid[1]
    ang = np.arctan2(wall[:, 1] - cy, wall[:, 0] - cx)
    zmid = wall[:, 2].min() + 0.5 * np.ptp(wall[:, 2])
    win = 12 * s.layer_height
    sel = (np.abs(ang) < 0.5) & (np.abs(wall[:, 2] - zmid) < win)
    seg = wall.copy()
    seg[~sel] = np.nan
    ax2.plot(seg[:, 1], seg[:, 0] - cx, lw=1.2, color="#d9622b")
    ax2.set_title("Close-up from above: ~24 layers on the +X side")
    ax2.set_xlabel("Y (mm, along the wall)")
    ax2.set_ylabel("distance from center (mm)")
    ax2.set_aspect("equal", adjustable="datalim")

    # cut through the wall at a wave peak: each strand drawn at its real size
    from matplotlib.patches import Ellipse
    ax3 = fig.add_subplot(2, 2, 4)
    n_waves = tp.waves_per_lap - s.phase_shift
    rate = n_waves + (s.phase_shift if max(1, s.weave_height) == 1 else 0)  # waves per lap
    t_peak = 0.5 + (0.25 - (rate * 0.5) % 1) / rate     # a wave peak, away from the lap start
    theta = 2 * math.pi * t_peak
    rise, ratio = wall_geometry(s)
    # an unsupported strand pulls into a round shape: draw it at its equal-area diameter
    sw = sh = math.sqrt(4 * ratio * mm3_per_mm(s) / math.pi)
    lay = np.array([m.layer for m in tp.moves if m.kind == "wall"])
    zc = wall[:, 2].min() + 0.5 * np.ptp(wall[:, 2])
    dang = np.abs((ang - theta + math.pi) % (2 * math.pi) - math.pi)
    rs, zs = [], []
    for L in np.unique(lay):
        idx = np.where(lay == L)[0]
        k = idx[np.argmin(dang[idx])]
        if abs(wall[k, 2] - zc) < 8 * rise:
            r = math.hypot(wall[k, 0] - cx, wall[k, 1] - cy)
            rs.append(r)
            zs.append(wall[k, 2] - sh / 2)
            ax3.add_patch(Ellipse((r, wall[k, 2] - sh / 2), sw, sh, color="#d9622b"))
    if rs:
        ax3.set_xlim(min(rs) - 1.5, max(rs) + 1.5)
        ax3.set_ylim(min(zs) - 0.5, max(zs) + 0.5)
    ax3.set_aspect("equal")
    ax3.set_title("Cut through the wall (strands to scale)")
    ax3.set_xlabel("distance from center (mm)")
    ax3.set_ylabel("Z (mm)")

    # unrolled outside view: strands near the outside drawn dark, inside light
    ax4 = fig.add_subplot(2, 2, 3)
    r_all = np.hypot(wall[:, 0] - cx, wall[:, 1] - cy)
    arc = ang * np.median(r_all)
    win_z = 14 * rise
    sel4 = (np.abs(arc) < 12) & (np.abs(wall[:, 2] - zc) < win_z)
    if sel4.any():
        dev = r_all - np.median(r_all[sel4])
        order = np.argsort(dev[sel4])             # draw outer strands last (on top)
        xs, zs4, cs = arc[sel4][order], wall[sel4, 2][order], dev[sel4][order]
        # strand height on screen: scale marker to the strand size in data units
        ax4.set_xlim(-12, 12)
        ax4.set_ylim(zc - win_z, zc + win_z)
        ax4.set_aspect("equal")
        fig.canvas.draw()
        ppm = ax4.transData.transform((1, 0))[0] - ax4.transData.transform((0, 0))[0]
        ax4.scatter(xs, zs4, c=cs, cmap="Oranges", s=(sw * ppm * 0.9) ** 2, marker="o",
                    linewidths=0, vmin=-s.amplitude, vmax=s.amplitude)
        ax4.set_xlim(-12, 12)
        ax4.set_ylim(zc - win_z, zc + win_z)
    ax4.set_aspect("equal")
    ax4.set_facecolor("#222222")
    ax4.set_title("Outside view, unrolled (dark orange = outer strands)")
    ax4.set_xlabel("along the wall (mm)")
    ax4.set_ylabel("Z (mm)")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    d = Settings()
    p = argparse.ArgumentParser(
        description="Turn an STL into wavy, woven-strand G-code (FullControl style). No slicer needed.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("stl", type=Path, help="input model (.stl, .obj, .3mf)")
    p.add_argument("-o", "--output", type=Path, help="output file (default: <model>_wave.gcode or .gcode.3mf)")
    p.add_argument("--bambu-template", type=Path,
                   help="a .gcode.3mf sliced in Bambu Studio for your printer; output becomes a .gcode.3mf")

    g = p.add_argument_group("wave")
    g.add_argument("--amplitude", type=float, default=d.amplitude, help="mm in/out from the surface")
    g.add_argument("--wavelength", type=float, default=d.wavelength, help="mm per wave (approx.)")
    g.add_argument("--waves", type=int, default=d.waves, help="exact waves per lap (overrides --wavelength)")
    g.add_argument("--phase-shift", type=float, default=d.phase_shift,
                   help="wave shift per layer: 0.5 = woven strands, 0 = straight ribs")

    g = p.add_argument_group("printing")
    g.add_argument("--layer-height", type=float, default=d.layer_height)
    g.add_argument("--line-width", type=float, default=d.line_width)
    g.add_argument("--filament-diameter", type=float, default=d.filament_diameter)
    g.add_argument("--flow", type=float, default=d.flow, help="extrusion multiplier")
    g.add_argument("--base-layers", type=int, default=d.base_layers, help="0 = no bottom")
    g.add_argument("--speed", type=float, default=d.speed, help="wall speed mm/s")
    g.add_argument("--base-speed", type=float, default=d.base_speed)
    g.add_argument("--first-layer-speed", type=float, default=d.first_layer_speed)
    g.add_argument("--adaptive", action="store_true",
                   help="measure the wall angle and use thinner layers + slower speed on steep sections")
    g.add_argument("--max-overhang", type=float, default=d.max_overhang,
                   help="mm one lap may stick out past the lap below (lower = safer, slower)")
    g.add_argument("--min-layer-height", type=float, default=d.min_layer_height,
                   help="thinnest layer --adaptive may use")
    g.add_argument("--valley-depth", type=float, default=d.valley_depth,
                   help="open weave: mm the top edge dips between crossings on a print with "
                        "--top-layers 0; the closing laps fill the dips so the rim sits level")
    g.add_argument("--valley-slope", type=float, default=d.valley_slope,
                   help="steepest dip into a valley (rise over run): 1.0 = 45 degrees, 1.3 = 52 degrees")
    g.add_argument("--densify-height", type=float, default=d.densify_height,
                   help="open weave: mm below the top where the gaps close into a dense band (0 = off)")
    g.add_argument("--aux-fan", type=float, default=d.aux_fan,
                   help="Bambu: side (auxiliary) fan level 0-1 (default off; uneven side cooling can lift the rim)")
    g.add_argument("--rim-fan", type=float, default=d.rim_fan,
                   help="open weave: part-fan level 0-1 while closing and printing the rim (less curl)")
    g.add_argument("--weave-height", type=int, default=d.weave_height,
                   help="laps stacked on each side before the strands cross; bigger = taller loops")
    g.add_argument("--top-layers", type=int, default=d.top_layers,
                   help="solid rim laps on top so the weave can't unravel (0 = none)")
    g.add_argument("--no-tracking", action="store_true",
                   help="don't carry wave positions up from the lap below (the pre-0.6 behaviour)")
    g.add_argument("--stagger", type=int, default=d.stagger,
                   help="laps per row of loops; each row shifts half a loop for a chain-mail look (0 = off, try 2)")
    g.add_argument("--weave-gap", type=float,
                   help="open weave: mm of see-through gap between strands (try 0.3-0.5; max = strand size)")
    g.add_argument("--strand-diameter", type=float,
                   help="open weave: strand thickness in mm (default = --line-width)")
    g.add_argument("--point-spacing", type=float, default=d.point_spacing)
    g.add_argument("--fan-layer", type=int, default=d.fan_layer)
    g.add_argument("--nozzle-temp", type=int, default=d.nozzle_temp, help="generic printers only")
    g.add_argument("--bed-temp", type=int, default=d.bed_temp, help="generic printers only")
    g.add_argument("--max-height", type=float, help="stop at this height (mm) - handy for quick tests")

    g = p.add_argument_group("printer / placement")
    g.add_argument("--bed-size", type=float, nargs=2, default=[d.bed_x, d.bed_y], metavar=("X", "Y"))
    g.add_argument("--flip", action="store_true",
                   help="print the model upside down (Bambu Studio rotations don't carry over)")
    g.add_argument("--center", type=float, nargs=2, metavar=("X", "Y"), help="default: bed center")
    g.add_argument("--seam-angle", type=float, default=0.0,
                   help="where each lap starts, in degrees: 0 = right (+X), 90 = back, 180 = left, 270 = front")
    g.add_argument("--start-gcode", type=Path, default=d.start_gcode)
    g.add_argument("--end-gcode", type=Path, default=d.end_gcode)
    g.add_argument("--no-preview", action="store_true", help="skip the preview image")
    p.add_argument("--version", action="version", version=f"Wave Script {__version__}")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = parse_args(argv)
    global SEAM_ANGLE
    SEAM_ANGLE = a.seam_angle
    s = Settings(
        amplitude=a.amplitude, wavelength=a.wavelength, waves=a.waves, phase_shift=a.phase_shift,
        layer_height=a.layer_height, line_width=a.line_width, filament_diameter=a.filament_diameter,
        flow=a.flow, base_layers=a.base_layers, point_spacing=a.point_spacing, speed=a.speed,
        base_speed=a.base_speed, first_layer_speed=a.first_layer_speed, fan_layer=a.fan_layer,
        nozzle_temp=a.nozzle_temp, bed_temp=a.bed_temp, bed_x=a.bed_size[0], bed_y=a.bed_size[1],
        center_x=a.center[0] if a.center else None, center_y=a.center[1] if a.center else None,
        max_height=a.max_height, flip=a.flip, weave_gap=a.weave_gap, stagger=a.stagger, tracking=not a.no_tracking, weave_height=a.weave_height, top_layers=a.top_layers, densify_height=a.densify_height, valley_depth=a.valley_depth, valley_slope=a.valley_slope, rim_fan=a.rim_fan, aux_fan=a.aux_fan,
        adaptive=a.adaptive, max_overhang=a.max_overhang, min_layer_height=a.min_layer_height, strand_diameter=a.strand_diameter, flavor="bambu" if a.bambu_template else "generic",
        start_gcode=a.start_gcode, end_gcode=a.end_gcode)

    if not a.stl.exists():
        sys.exit(f"File not found: {a.stl}")
    if a.bambu_template and not a.bambu_template.exists():
        sys.exit(f"Template not found: {a.bambu_template}")

    stem = a.stl.stem
    if a.output:
        out = a.output
    elif a.bambu_template:
        out = a.stl.with_name(f"{stem}_wave.gcode.3mf")
    else:
        out = a.stl.with_name(f"{stem}_wave.gcode")

    print(f"Wave Script {__version__}")
    print(f"  model:    {a.stl}")
    mesh = load_mesh(a.stl, s)
    ext = mesh.extents
    print(f"  size:     {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm")
    lo, hi = mesh.bounds
    if (lo[0] - s.amplitude < 0 or lo[1] - s.amplitude < 0
            or hi[0] + s.amplitude > s.bed_x or hi[1] + s.amplitude > s.bed_y):
        print("  ! Warning: the print extends past the bed edges.")

    if s.weave_gap is not None:
        d = s.strand_diameter or s.line_width
        if s.weave_gap > d:
            sys.exit(f"--weave-gap ({s.weave_gap}) can't be bigger than the strand ({d} mm) "
                     "or the layers won't touch at the crossings.")
        if s.phase_shift == 0:
            print("  ! Note: --weave-gap needs --phase-shift 0.5 to open up; with 0 the wall stays solid.")
        if s.amplitude < d:
            print(f"  ! Note: amplitude {s.amplitude} is less than the strand size {d};"
                  " the openings will be very small.")
        print(f"  weave:    {d} mm strands, {s.weave_gap} mm gaps, rising {(d + s.weave_gap) / 2:.3f} mm per lap")

    tp = build_toolpath(mesh, s)
    body, stats = write_body(tp, s)

    if a.bambu_template:
        write_bambu(body, a.bambu_template, out)
    else:
        write_generic(body, s, out, stats)

    print(f"  waves:    {tp.waves_per_lap - s.phase_shift:.0f} per lap, shift {s.phase_shift} per layer")
    print(f"  layers:   {stats['layers']}  (height {stats['height']:.1f} mm)")
    if s.adaptive:
        print(f"  adaptive: on (max overhang {s.max_overhang} mm, layers {s.min_layer_height}-{s.layer_height} mm)")
    if tp.steep:
        # group neighbouring steep laps into height ranges
        ranges, cur = [], [tp.steep[0][0], tp.steep[0][0], tp.steep[0][1]]
        for z, ang in tp.steep[1:]:
            if z - cur[1] < 1.0:
                cur[1], cur[2] = z, max(cur[2], ang)
            else:
                ranges.append(cur)
                cur = [z, z, ang]
        ranges.append(cur)
        print("  ! Steep sections - strands may sag here:")
        for z1, z2, ang in ranges[:8]:
            print(f"      Z {z1:.1f}-{z2:.1f} mm, up to {ang:.0f} degrees")
        if s.weave_gap is not None or s.strand_diameter is not None:
            print("    Open weaves can't use thinner layers; lower --speed or --amplitude, or redesign the slope.")
        elif not s.adaptive:
            print("    Try --adaptive to use thinner layers and slow down on these sections.")
        else:
            print("    Already at the thinnest layer; try a lower --min-layer-height or a gentler slope.")
    print(f"  filament: {stats['filament_m']:.2f} m  (~{stats['grams']:.0f} g PLA)")
    print(f"  time:     ~{stats['minutes']:.0f} min (moves only, no heat-up)")
    print(f"  wrote:    {out}")

    if not a.no_preview:
        prev = out.with_name(out.name.split(".")[0] + "_preview.png")
        save_preview(tp, s, prev, f"{stem}: amplitude {s.amplitude} mm, "
                                  f"{tp.waves_per_lap - s.phase_shift:.0f} waves/lap, shift {s.phase_shift}")
        print(f"  preview:  {prev}")


if __name__ == "__main__":
    main()
