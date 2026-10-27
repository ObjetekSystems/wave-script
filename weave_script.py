#!/usr/bin/env python3
"""
Weave Script - pick a weave pattern by name and print it on any simple CAD shape.

Wave Script (wave_script.py) gives you every knob. Weave Script is the easy companion:
choose a pattern, optionally a few height zones, and it sets the rest for you.

    python weave_script.py examples/bulb_vase.stl --pattern lattice
    python weave_script.py examples/bulb_vase.stl --zones "0-15:solid, 15-70:lattice, 70-:solid"
    python weave_script.py --list                 (show every pattern)

Bambu printers: add  --bambu-template template.gcode.3mf  exactly like Wave Script.
It needs wave_script.py in the same folder (it borrows the slicing and G-code parts).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import wave_script as ws
from wave_script import Move, Settings, Toolpath

__version__ = "0.4.5"


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
@dataclass
class Pattern:
    name: str
    about: str
    waveform: str = "sine"     # sine | triangle | square | scallop
    shift: float = 0.5         # fraction of a wave the strands move per crossing (0 = ribs)
    group: int = 1             # laps stacked on each side before the strands cross
    stagger: int = 0           # laps per row; every other row shifts half a loop
    depth: float = 1.0         # mm in/out from the CAD surface (0 = smooth wall)
    cell: float = 2.5          # mm width of one loop / opening (half a wavelength)
    gap: float = 0.0           # mm see-through gap between strands (0 = packed, solid wall)
    strand: float = 0.5        # mm strand thickness
    speed: float = 25.0        # mm/s
    bridge_speed: float = 15.0 # mm/s where a strand isn't resting on the lap below
    valley: float = 0.0        # mm the top edge dips between crossings (measured; 0 = unknown)
    drift: float = 0.0         # waves per lap that alternate laps drift in opposite directions
                               # (0 = off); crossings then line up in diagonals -> big diamonds


PATTERNS = {p.name: p for p in [
    Pattern("lattice", "Deep open pleats with see-through columns (lamp shades)",
            waveform="triangle", depth=2.0, cell=5.0, gap=0.3, strand=0.6, speed=10, bridge_speed=10,
            valley=2.25),   # settings and valley depth from test 15L5 (clean rim)
    Pattern("diamonds", "Big diamonds: crossings line up in diagonals (experimental)",
            waveform="triangle", depth=2.0, cell=5.0, gap=0.3, strand=0.6, speed=10, bridge_speed=10,
            drift=0.02, valley=2.25),   # drift 0.02 keeps each lap over the lap two below (like lattice)
    Pattern("diamond", "Classic tight woven texture, solid wall (Wave Script's default look)",
            depth=1.0, cell=2.5),
    Pattern("chainmail", "Open rings, each row tucked between the loops below",
            depth=1.5, cell=3.0, stagger=2, gap=0.3, strand=0.5, speed=15, bridge_speed=12,
            valley=0.9),    # dip measured 0.8-1.0 mm on a no-rim band (test 16)
    Pattern("basket", "Over-under bands three strands tall, like a woven basket",
            depth=1.5, cell=4.0, group=3),
    Pattern("twill", "Diagonal woven lines (denim-style twill)",
            depth=1.0, cell=3.0, shift=0.25),
    Pattern("scales", "Overlapping rounded scales (fish scale / dragon skin)",
            waveform="scallop", depth=1.2, cell=3.0, group=2),
    Pattern("brick", "Rectangular blocks offset like brickwork",
            waveform="square", depth=1.0, cell=4.0, group=3),
    Pattern("ribs", "Straight vertical ribs, always prints (good for bands and test prints)",
            depth=1.0, cell=2.5, shift=0.0),
    Pattern("solid", "Smooth plain wall, no wave (bases, caps and rims in --zones)",
            depth=0.0, shift=0.0),
]}


def waveform(name: str, x: np.ndarray) -> np.ndarray:
    """Wave shape over one period (x in waves), values -1..1."""
    if name == "triangle":
        return (2 / math.pi) * np.arcsin(np.clip(np.sin(2 * math.pi * x), -1, 1))
    if name == "square":
        k = 3.0                                  # edge sharpness; higher = boxier
        return np.tanh(k * np.sin(2 * math.pi * x)) / math.tanh(k)
    if name == "scallop":
        return 2 * np.abs(np.sin(math.pi * x)) - 1   # outward arches, sharp inward cusps
    return np.sin(2 * math.pi * x)


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #
@dataclass
class Zone:
    lo: float
    hi: float
    pat: Pattern


OVERRIDABLE = {"depth": float, "cell": float, "gap": float, "strand": float,
               "speed": float, "group": int, "stagger": int, "shift": float, "drift": float}


def apply_overrides(p: Pattern, text: str) -> Pattern:
    """'depth=2,cell=6' -> a copy of the pattern with those values."""
    changes = {}
    for item in filter(None, (t.strip() for t in text.split(","))):
        if "=" not in item:
            sys.exit(f"Zone setting '{item}' should look like name=value (e.g. depth=2)")
        k, v = (t.strip() for t in item.split("=", 1))
        if k not in OVERRIDABLE:
            sys.exit(f"Unknown zone setting '{k}'. Use: {', '.join(OVERRIDABLE)}")
        changes[k] = OVERRIDABLE[k](v)
    return replace(p, **changes)


def parse_height(text: str, height: float, default: float) -> float:
    text = text.strip()
    if not text:
        return default
    if text.endswith("%"):
        return float(text[:-1]) / 100 * height
    return float(text)


def parse_zones(text: str | None, default: Pattern, height: float) -> list[Zone]:
    """'0-15:solid, 15-70:lattice@depth=2, 70-:solid' -> sorted zones covering 0..height.

    Heights are mm from the bed (or % of the model height). Anything not covered by a zone
    uses --pattern.
    """
    if not text:
        return [Zone(0.0, height, default)]
    zones = []
    # split on commas that start a new zone (a digit or '-' after the comma), not settings
    for part in re.split(r",\s*(?=[\d.]*%?\s*-)", text.strip()):
        m = re.fullmatch(r"\s*([\d.]*%?)\s*-\s*([\d.]*%?)\s*:\s*(\w+)\s*(?:@(.*))?", part)
        if not m:
            sys.exit(f"Can't read zone '{part}'. Example: \"0-15:solid, 15-70:lattice, 70-:solid\"")
        lo = parse_height(m.group(1), height, 0.0)
        hi = parse_height(m.group(2), height, height)
        name = m.group(3).lower()
        if name not in PATTERNS:
            sys.exit(f"Unknown pattern '{name}' in --zones. Run with --list to see them.")
        pat = apply_overrides(apply_cli(PATTERNS[name]), m.group(4) or "")
        if hi <= lo:
            sys.exit(f"Zone '{part.strip()}' ends before it starts.")
        zones.append(Zone(lo, min(hi, height), pat))
    zones.sort(key=lambda z: z.lo)
    # fill holes with the default pattern
    full, z = [], 0.0
    for zn in zones:
        if zn.lo < z - 1e-6:
            sys.exit("Zones overlap; check the heights in --zones.")
        if zn.lo > z + 1e-6:
            full.append(Zone(z, zn.lo, default))
        full.append(zn)
        z = zn.hi
    if z < height - 1e-6:
        full.append(Zone(z, height, default))
    return full


CLI_OVERRIDES: dict = {}


def apply_cli(p: Pattern) -> Pattern:
    """Command-line --depth/--cell/... apply to every pattern that has a wave."""
    if p.name == "solid":
        return replace(p, **{k: v for k, v in CLI_OVERRIDES.items() if k in ("strand", "speed")})
    return replace(p, **CLI_OVERRIDES)


# --------------------------------------------------------------------------- #
# Toolpath
# --------------------------------------------------------------------------- #
def drift_offset(drift: float, lap: int, t: np.ndarray, n_waves: int,
                 freeze: int | None = None) -> np.ndarray:
    """Extra wave shift for the big-diamond pattern.

    Even laps drift forward `drift` waves per lap and odd laps drift back, so each family of
    laps carries its waves along a diagonal and the crossings between the two families line up
    in criss-crossing diagonals. The jump between one lap's family and the next is spread
    evenly over the whole lap, so the seam has no kink. Stops changing in the densify band (`freeze`).
    """
    if drift <= 0 or lap <= 0:
        return np.zeros_like(t)
    if freeze is not None and lap >= freeze:
        lap = freeze - 1

    def val(k):
        return (drift * k * (1 if k % 2 == 0 else -1)) % 1.0
    now, before = val(lap), val(lap - 1)
    d = (now - before + 0.5) % 1.0 - 0.5             # shortest way round
    # spread the jump evenly over the whole lap, like the normal weave's half-wave shift.
    # Squeezing it into the first wavelength (0.4.0-0.4.3) distorted the loops at the seam
    # (test 17s: deformed patch on the +X side, where every lap starts).
    return before + d * t


def lap_rise(p: Pattern, s: Settings) -> float:
    """Open patterns rise (strand + gap)/2 per lap; packed ones one layer height."""
    if p.gap > 0:
        return (p.strand + p.gap) / 2
    return s.layer_height


def strand_ratio(p: Pattern, s: Settings) -> float:
    """Round open-weave strand vs. a normal line, in extrusion."""
    return (math.pi * p.strand ** 2 / 4) / ws.mm3_per_mm(s)


def build_weave_wall(mesh, s: Settings, zones: list[Zone], blend: float, node_flow: float,
                     tp: Toolpath, densify: float = 2.0, valley: float | None = None,
                     valley_slope: float = 1.3) -> None:
    h = s.layer_height
    top = mesh.bounds[1][2]
    if s.max_height:
        top = min(top, s.max_height)
    z0 = (s.base_layers + 1) * h

    def zone_at(z):
        for zn in zones:
            if z < zn.hi:
                return zn
        return zones[-1]

    boundaries = [zn.lo for zn in zones[1:]]          # where one pattern hands over to the next
    # Top rim, same as Wave Script 0.6.6 (confirmed in its test 15):
    # open pattern at the top -> close the gap over `densify` mm with the wave unchanged, then
    # "closing" laps that repeat the last lap's path and shrink the wave 0.15 mm per lap, so
    # each lap stacks right on the one below, then the flat rim. Fading the wave out while the
    # strands are still open (as before) left the rim with nothing to grip and it came loose.
    top_pat = zone_at(max(z0, top - 1e-3)).pat
    open_top = s.top_layers > 0 and top_pat.gap > 0 and top_pat.depth > 0
    n_close = math.ceil(top_pat.depth / ws.CLOSE_STEP) if open_top else 0
    # Valley fill (test 15L3): open strands sag between crossings, so the top edge is a row of
    # peaks (crossings) and valleys (`valley` mm deep, measured on a print without a rim). A
    # rim printed at one height only touches the peaks and peels off. With valley > 0 the
    # closing laps start down in each valley, on the sagged strand, and rise a little faster
    # there than at the peaks until the top is level. The densify band is skipped, since the
    # valley depth is measured on the plain weave.
    if valley is None:
        valley = top_pat.valley           # the pattern's measured value (lattice: test 15L5)
    valley = valley if open_top else 0.0
    if valley > 0:
        n_close = max(n_close, math.ceil(valley / ws.VALLEY_STEP))
    rim_h = (max(0, s.top_layers) + n_close) * h
    dense_len = densify if (open_top and valley <= 0) else 0.0
    # open patterns: closing and rim laps as wide as the strands, so the top band matches the
    # body instead of looking thin (test 15L4)
    rim_w = max(s.line_width, top_pat.strand) if open_top else s.line_width
    if valley > 0:
        # valley-fill band: at least 0.6 mm lines at 10 mm/s, as on the lattice that held
        # (test 15L5). Chainmail's 0.5 mm lines at 12 mm/s came loose on one side (test 18).
        rim_w = max(rim_w, ws.VALLEY_WIDTH)
    z_dense = top - rim_h - dense_len
    # patterns that cross on every lap (group 1, e.g. chainmail, lattice) switch to stacked
    # pairs with the stagger frozen in the densify band, like the control print that held
    # (see wave_script 0.6.9)
    cross_every = open_top and max(1, top_pat.group) == 1
    freeze = None                     # zone-local lap where the densify band starts
    if s.top_layers > 0 and not open_top:
        # solid patterns: fade the wave out before the rim, so the rim has a continuous
        # strand to sit on
        boundaries.append(top - rim_h)

    def blend_factor(z: np.ndarray) -> np.ndarray:
        """0 at a zone boundary, 1 once `blend` mm away, so patterns swap at zero depth."""
        f = np.ones_like(z)
        for b in boundaries:
            if blend > 0:
                f = np.minimum(f, np.clip(np.abs(z - b) / blend, 0, 1))
        return f

    lap = 0
    rim_lap = 0                       # 0 = weaving, 1..n_close = closing laps, then rim
    last_weave = (0, 0)               # (lap, zone start lap) of the last weave lap
    z_prev_end = z0
    last_rise = 0.0
    prev_pts = prev_w = prev_xy = None
    fill = ws.ValleyFill(valley, n_close)
    cur_zone = None
    zone_lap0 = 0                     # lap where the current zone started
    n_waves = 3
    steep = []
    bridged = [0, 0]
    tp.zone_report = []
    while True:
        zn = zone_at(z_prev_end + 1e-6)
        p = zn.pat
        if zn is not cur_zone:
            cur_zone, zone_lap0 = zn, lap
            first = ws.outline_at(mesh, min(max(z_prev_end, h / 2), top - 1e-3))
            if first is None:
                break
            n_waves = max(3, round(first.exterior.length / (2 * p.cell))) if p.depth > 0 else 3
            tp.zone_report.append((zn.lo, zn.hi, p, n_waves))
        open_lap = p.gap > 0 and not rim_lap
        rise = 0.0 if lap == 0 else (h if rim_lap else lap_rise(p, s))
        dense_f = 0.0
        if open_lap and dense_len > 0 and lap > 0 and z_prev_end >= z_dense:
            # densify band: close the gap and blend to flat lines, wave unchanged
            dense_f = min(1.0, max(0.0, (z_prev_end - z_dense) / dense_len))
            if cross_every and freeze is None:
                freeze = lap - zone_lap0 + 1
            rise = rise + (h - rise) * dense_f
        z_start = z_prev_end
        z_end = z0 if lap == 0 else z_start + rise
        if not rim_lap and lap > 0 and z_end > top - rim_h + 1e-6:
            if s.top_layers <= 0 or lap < 2:
                break
            rim_lap = 1
            last_weave = (lap - 1, zone_lap0)
            continue
        if rim_lap > n_close + s.top_layers:
            break
        sample_h = rise if rise > 0 else h
        poly = ws.outline_at(mesh, max(h / 2, min(z_end - sample_h / 2, top - 1e-3)))
        if poly is None:
            break
        n = max(16, int(poly.exterior.length / s.point_spacing))
        pts = ws.resample_ring(poly, n, prev_pts[0] if prev_pts is not None else None)
        nrm = ws.outward_normals(pts)
        t = np.arange(n) / n
        z = np.full(n, z_end) if lap == 0 else z_start + t * rise

        w = t
        if prev_pts is not None:
            w = ws.track_wave_position(pts, prev_pts, prev_w, t)
        prev_pts, prev_w = pts, w

        amp = p.depth * blend_factor(z)
        cfg = SimpleNamespace(phase_shift=p.shift, weave_height=p.group, stagger=p.stagger)
        if rim_lap and open_top:
            if rim_lap <= n_close:
                amp = amp * (1 - (rim_lap - 1 + t) / n_close)    # shrink 0.15 mm per lap
            else:
                amp = np.zeros(n)
            # repeat the last weave lap's pattern so each lap stacks exactly on the one below;
            # the small phase jump at the lap start is blended in over one wavelength
            L, L0 = last_weave
            loc = L - L0 + 1

            def phase_at(ww):
                return (n_waves * (L + ww) + ws.band_shift(cfg, loc, ww, n_waves, freeze)
                        + ws.stagger_offset(cfg, loc, ww, n_waves, freeze)
                        + drift_offset(p.drift, loc, ww, n_waves, freeze))
            ends = phase_at(np.array([0.0, 1.0]))
            jump = (ends[1] - ends[0] + 0.5) % 1.0 - 0.5
            phase = phase_at(w) + jump * (1 - np.clip(w * n_waves, 0, 1))
        else:
            if rim_lap == 1:
                amp = amp * (1 - t)
            elif rim_lap > 1:
                amp = np.zeros(n)
            # pattern shift, borrowed from Wave Script (counted from the start of this zone)
            local = lap - zone_lap0 + 1 if lap > 0 else 0
            shift = (ws.band_shift(cfg, local, w, n_waves, freeze)
                     + ws.stagger_offset(cfg, local, w, n_waves, freeze)
                     + drift_offset(p.drift, local, w, n_waves, freeze))
            phase = n_waves * (lap + w) + shift
        offset = amp * waveform(p.waveform, phase)
        xy = pts + nrm * offset[:, None]
        layer_no = s.base_layers + 1 + lap
        speed = s.first_layer_speed if layer_no == 1 else p.speed

        # steep-wall warning (same rule as Wave Script)
        if lap > 0 and not rim_lap and lap % 5 == 0:
            slope = ws.wall_slope(mesh, z_start, max(1.0, 3 * rise), top)
            if rise * slope > s.max_overhang * 1.05:
                steep.append((z_end, math.degrees(math.atan(slope))))

        if lap == 0:
            tp.moves.append(Move(*xy[0], z_end, 0.0, s.travel_speed, layer_no, "travel"))
            e = np.ones(n)
        else:
            gap_under = last_rise + (rise - last_rise) * t
            if open_lap:
                e = (t if lap == 1 else np.ones(n)) * strand_ratio(p, s)
                if dense_f > 0:
                    flat = np.array([ws.line_area(s.line_width, g) for g in gap_under]) / ws.mm3_per_mm(s)
                    e = e * (1 - dense_f) + flat * dense_f
            else:
                lw = rim_w if rim_lap else s.line_width
                e = np.array([ws.line_area(lw, g) for g in gap_under]) / ws.mm3_per_mm(s)
            if rim_lap:
                speed = min(speed, 15.0)
            if open_top and (rim_lap or dense_f > 0):
                speed = min(speed, ws.VALLEY_SPEED if valley > 0 else 12.0)   # tested speeds
            vf = fill.lap(rim_lap, w, t) if rim_lap else None
            if vf is not None:
                # valley fill: start down in each valley and level the top (ws.ValleyFill)
                z = z - vf[0]
                e = np.array([ws.line_area(rim_w, g)
                              for g in gap_under + vf[1]]) / ws.mm3_per_mm(s)

        spd = np.full(n, speed)
        if open_lap and lap > 0 and prev_xy is not None:
            dist = ws.support_distance(xy, prev_xy)
            free = dist > p.strand / 2
            node = ~free
            free = np.convolve(np.concatenate([free[-2:], free, free[:2]]).astype(float),
                               np.ones(5), mode="same")[2:-2] > 0
            spd[free] = np.minimum(spd[free], p.bridge_speed)
            e = np.where(node, e * node_flow, e)       # a little extra where strands cross
            bridged[0] += int(free.sum())
            bridged[1] += n
            # remember where this lap bridges, as a sag profile along the wave position
            if valley > 0:
                fill.note(ws.sag_profile(xy, prev_xy, w, p.strand, valley, valley_slope))
        prev_xy = xy

        for i in range(1 if lap == 0 else 0, n):
            tp.moves.append(Move(xy[i, 0], xy[i, 1], z[i], float(e[i]), float(spd[i]), layer_no, "wall"))
        lap += 1
        z_prev_end = z_end
        last_rise = rise
        if rim_lap:
            rim_lap += 1
    tp.layers = s.base_layers + lap
    tp.steep = steep
    tp.bridged = bridged


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def save_weave_preview(tp: Toolpath, zones: list[Zone], out_path: Path, title: str) -> None:
    """3D toolpath plus a close-up of each zone, seen slightly from above so the in/out
    swing of the strands shows (outer strands drawn on top, strand size to scale)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    wall = np.array([[m.x, m.y, m.z] for m in tp.moves if m.kind == "wall"])
    lay = np.array([m.layer for m in tp.moves if m.kind == "wall"])
    cx, cy = (wall[:, 0].max() + wall[:, 0].min()) / 2, (wall[:, 1].max() + wall[:, 1].min()) / 2
    shown = [zn for zn in zones if zn.pat.depth > 0][:3] or zones[:1]
    fig = plt.figure(figsize=(6 + 5 * len(shown), 7))

    ax = fig.add_subplot(1, len(shown) + 1, 1, projection="3d")
    step = max(1, len(wall) // 60000)
    ax.plot(wall[::step, 0], wall[::step, 1], wall[::step, 2], lw=0.3, color="#d9622b")
    ax.view_init(elev=12, azim=-60)
    rng = np.ptp(wall, axis=0).max() / 2
    mid = (wall.max(axis=0) + wall.min(axis=0)) / 2
    ax.set_xlim(mid[0] - rng, mid[0] + rng)
    ax.set_ylim(mid[1] - rng, mid[1] + rng)
    ax.set_zlim(0, 2 * rng)
    ax.set_title("Toolpath (3D)")

    # angle measured from the -X side, opposite the lap start, so the seam stays out of view
    ang = np.mod(np.arctan2(wall[:, 1] - cy, wall[:, 0] - cx), 2 * math.pi) - math.pi
    r = np.hypot(wall[:, 0] - cx, wall[:, 1] - cy)
    tilt = 0.35                                       # how much of the in/out swing shows
    for k, zn in enumerate(shown):
        ax2 = fig.add_subplot(1, len(shown) + 1, k + 2)
        p = zn.pat
        zc = (max(zn.lo, wall[:, 2].min()) + min(zn.hi, wall[:, 2].max())) / 2
        win_z = min(5.0, (zn.hi - zn.lo) / 2)
        sel = (np.abs(ang) < 0.6) & (np.abs(wall[:, 2] - zc) < win_z)
        if not sel.any():
            continue
        r0 = np.median(r[sel])
        segs, depth = [], []
        for L in np.unique(lay[sel]):
            idx = np.where(sel & (lay == L))[0]
            idx = idx[np.argsort(ang[idx])]
            x = ang[idx] * r0
            dv = r[idx] - np.median(r[idx])           # in/out of this lap (ignores wall lean)
            y = wall[idx, 2] - tilt * dv              # outward = lower, like looking down a bit
            for i in range(len(idx) - 1):
                if abs(x[i + 1] - x[i]) < 1.0:
                    segs.append([(x[i], y[i]), (x[i + 1], y[i + 1])])
                    depth.append((dv[i] + dv[i + 1]) / 2)
        depth = np.array(depth)
        order = np.argsort(depth)                     # inner first, outer strands on top
        ax2.set_xlim(-12, 12)
        ax2.set_ylim(zc - win_z, zc + win_z)
        ax2.set_aspect("equal")
        fig.canvas.draw()
        ppm = ax2.transData.transform((1, 0))[0] - ax2.transData.transform((0, 0))[0]
        size = p.strand if p.gap > 0 else 0.45
        lw = size * ppm * 72 / fig.dpi
        lim = max(p.depth, 0.1)
        lc = LineCollection([segs[i] for i in order], linewidths=lw, cmap="Oranges",
                            norm=plt.Normalize(-lim * 1.6, lim), capstyle="round")
        lc.set_array(depth[order])
        ax2.add_collection(lc)
        ax2.set_facecolor("#1d1d1d")
        kind = "open" if p.gap > 0 else "solid"
        ax2.set_title(f"{p.name} ({kind}) around Z {zc:.0f} mm")
        ax2.set_xlabel("along the wall (mm)")
        ax2.set_ylabel("Z (mm)")
    fig.suptitle(title + "   (close-ups seen slightly from above; bright = outer strands)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_patterns() -> None:
    print("Weave Script patterns (use with --pattern or inside --zones):\n")
    for p in PATTERNS.values():
        kind = "open" if p.gap > 0 else "solid"
        print(f"  {p.name:<10} {p.about}")
        if p.depth > 0:
            print(f"  {'':<10} depth {p.depth} mm, cell {p.cell} mm, {kind} wall, {p.waveform} wave")
    print('\nZones example:  --zones "0-15:solid, 15-70:lattice@depth=2, 70-:solid"')


def parse_args(argv=None):
    d = Settings()
    p = argparse.ArgumentParser(
        description="Pick a weave pattern by name and turn an STL into G-code. "
                    "For every individual setting use wave_script.py instead.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("stl", type=Path, nargs="?", help="input model (.stl, .obj, .3mf)")
    p.add_argument("--list", action="store_true", help="show every pattern and exit")
    p.add_argument("-o", "--output", type=Path, help="output file (default: <model>_weave.gcode or .gcode.3mf)")
    p.add_argument("--bambu-template", type=Path,
                   help="a .gcode.3mf sliced in Bambu Studio for your printer; output becomes a .gcode.3mf")

    g = p.add_argument_group("pattern")
    g.add_argument("--pattern", default="lattice", choices=list(PATTERNS), help="the weave to print")
    g.add_argument("--zones", help='height bands, e.g. "0-15:solid, 15-70:lattice, 70-:solid" '
                                   '(mm from the bed or %%; gaps use --pattern)')
    g.add_argument("--blend", type=float, default=3.0,
                   help="mm over which the wave fades out and back in where zones meet")
    g.add_argument("--depth", type=float, help="override: mm the strands move in/out")
    g.add_argument("--cell", type=float, help="override: mm width of one loop / opening")
    g.add_argument("--gap", type=float, help="override: mm see-through gap (0 = solid wall; max = strand)")
    g.add_argument("--strand", type=float, help="override: mm strand thickness")
    g.add_argument("--drift", type=float,
                   help="override (diamonds): waves per lap that alternate laps drift apart; "
                        "0.02 = soft 25 mm diamonds, 0.05 = bold 10 mm diamonds")
    g.add_argument("--node-flow", type=float, default=1.1,
                   help="open patterns: extra extrusion where strands cross, so joints fuse (1.0 = off)")

    g = p.add_argument_group("printing")
    g.add_argument("--speed", type=float, help="override: wall speed mm/s (default from the pattern)")
    g.add_argument("--layer-height", type=float, default=d.layer_height, help="solid patterns and base")
    g.add_argument("--line-width", type=float, default=d.line_width)
    g.add_argument("--flow", type=float, default=d.flow, help="extrusion multiplier")
    g.add_argument("--base-layers", type=int, default=d.base_layers, help="0 = no bottom (lamp shades)")
    g.add_argument("--top-layers", type=int, default=2,
                   help="solid rim laps on top (open patterns close up first, see --densify-height)")
    g.add_argument("--valley-depth", type=float, default=None,
                   help="open patterns: mm the top edge dips between crossings on a print with "
                        "--top-layers 0; the closing laps fill the dips so the rim sits level "
                        "(default: the pattern's measured value, lattice 2.25)")
    g.add_argument("--valley-slope", type=float, default=1.3,
                   help="steepest dip into a valley (rise over run): 1.0 = 45 degrees, 1.3 = 52 degrees")
    g.add_argument("--densify-height", type=float, default=2.0,
                   help="open patterns: mm below the rim where the gaps close into a dense band (0 = off)")
    g.add_argument("--filament-diameter", type=float, default=d.filament_diameter)
    g.add_argument("--nozzle-temp", type=int, default=d.nozzle_temp, help="generic printers only")
    g.add_argument("--bed-temp", type=int, default=d.bed_temp, help="generic printers only")
    g.add_argument("--max-height", type=float, help="stop at this height (mm) - handy for quick tests")

    g = p.add_argument_group("printer / placement")
    g.add_argument("--bed-size", type=float, nargs=2, default=[d.bed_x, d.bed_y], metavar=("X", "Y"))
    g.add_argument("--flip", action="store_true", help="print the model upside down")
    g.add_argument("--center", type=float, nargs=2, metavar=("X", "Y"), help="default: bed center")
    g.add_argument("--seam-angle", type=float, default=0.0,
                   help="where each lap starts, in degrees: 0 = right (+X), 90 = back, 180 = left, 270 = front")
    g.add_argument("--start-gcode", type=Path, default=d.start_gcode)
    g.add_argument("--end-gcode", type=Path, default=d.end_gcode)
    g.add_argument("--no-preview", action="store_true", help="skip the preview image")
    p.add_argument("--version", action="version", version=f"Weave Script {__version__}")
    return p, p.parse_args(argv)


def main(argv=None) -> None:
    parser, a = parse_args(argv)
    if a.list:
        print_patterns()
        return
    if a.stl is None:
        parser.error("give a model file, or use --list to see the patterns")
    if not a.stl.exists():
        sys.exit(f"File not found: {a.stl}")
    if a.bambu_template and not a.bambu_template.exists():
        sys.exit(f"Template not found: {a.bambu_template}")

    for k in ("depth", "cell", "gap", "strand", "speed", "drift"):
        if getattr(a, k) is not None:
            CLI_OVERRIDES[k] = getattr(a, k)

    ws.SEAM_ANGLE = a.seam_angle
    s = Settings(
        layer_height=a.layer_height, line_width=a.line_width, filament_diameter=a.filament_diameter,
        flow=a.flow, base_layers=a.base_layers, top_layers=a.top_layers,
        nozzle_temp=a.nozzle_temp, bed_temp=a.bed_temp, bed_x=a.bed_size[0], bed_y=a.bed_size[1],
        center_x=a.center[0] if a.center else None, center_y=a.center[1] if a.center else None,
        max_height=a.max_height, flip=a.flip, flavor="bambu" if a.bambu_template else "generic",
        start_gcode=a.start_gcode, end_gcode=a.end_gcode)

    print(f"Weave Script {__version__}")
    print(f"  model:    {a.stl}")
    mesh = ws.load_mesh(a.stl, s)
    ext = mesh.extents
    print(f"  size:     {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm")

    default = apply_cli(PATTERNS[a.pattern])
    zones = parse_zones(a.zones, default, float(ext[2]))
    for zn in zones:
        p = zn.pat
        if p.gap > p.strand:
            sys.exit(f"{p.name}: gap ({p.gap}) can't be bigger than the strand ({p.strand} mm) "
                     "or the strands won't touch where they cross.")
        if p.gap > 0 and p.shift == 0:
            print(f"  ! Note: {p.name} has a gap but no crossing (shift 0), so it won't hold together.")

    # the preview and file header describe the main (tallest) patterned zone
    main_zone = max(zones, key=lambda zn: (zn.pat.depth > 0, zn.hi - zn.lo)).pat
    s.amplitude = max(zn.pat.depth for zn in zones) or 0.01
    s.phase_shift = main_zone.shift
    s.weave_height = main_zone.group
    s.wavelength = 2 * main_zone.cell
    if main_zone.gap > 0:
        s.weave_gap, s.strand_diameter = main_zone.gap, main_zone.strand

    tp = Toolpath()
    if s.base_layers > 0:
        ws.build_base(mesh, s, tp)
    build_weave_wall(mesh, s, zones, a.blend, a.node_flow, tp, a.densify_height,
                     a.valley_depth, a.valley_slope)
    main_waves = next((nw for _, _, p, nw in tp.zone_report if p is main_zone), 3)
    tp.waves_per_lap = main_waves + s.phase_shift      # what the preview expects
    body, stats = ws.write_body(tp, s)

    stem = a.stl.stem
    if a.output:
        out = a.output
    elif a.bambu_template:
        out = a.stl.with_name(f"{stem}_weave.gcode.3mf")
    else:
        out = a.stl.with_name(f"{stem}_weave.gcode")
    if a.bambu_template:
        ws.write_bambu(body, a.bambu_template, out)
    else:
        ws.write_generic(body, s, out, stats)

    print("  zones:")
    for lo, hi, p, nw in tp.zone_report:
        hi_txt = f"{hi:.1f}" if hi < ext[2] - 1e-6 else "top"
        if p.depth > 0:
            kind = f"open, {p.gap} mm gaps" if p.gap > 0 else "solid"
            print(f"      Z {lo:.1f}-{hi_txt}: {p.name} ({nw} waves/lap, depth {p.depth} mm, {kind})")
        else:
            print(f"      Z {lo:.1f}-{hi_txt}: {p.name}")
    print(f"  layers:   {stats['layers']}  (height {stats['height']:.1f} mm)")
    opened = [zn.pat for zn in zones if zn.pat.gap > 0]
    if opened:
        print(f"  open weave: strands slow to {min(p.bridge_speed for p in opened):g} mm/s between"
              f" crossings; crossings get x{a.node_flow} flow so they fuse")
    if tp.steep:
        ranges, cur = [], [tp.steep[0][0], tp.steep[0][0], tp.steep[0][1]]
        for z, ang in tp.steep[1:]:
            if z - cur[1] < 3.0:
                cur[1], cur[2] = z, max(cur[2], ang)
            else:
                ranges.append(cur)
                cur = [z, z, ang]
        ranges.append(cur)
        print("  ! Steep sections - strands may sag here:")
        for z1, z2, ang in ranges[:6]:
            print(f"      Z {z1:.0f}-{z2:.0f} mm, up to {ang:.0f} degrees")
        print("    Lower --speed or --depth there, or use wave_script.py --adaptive for solid walls.")
    print(f"  filament: {stats['filament_m']:.2f} m  (~{stats['grams']:.0f} g PLA)")
    print(f"  time:     ~{stats['minutes']:.0f} min (moves only, no heat-up)")
    print(f"  wrote:    {out}")

    if not a.no_preview:
        prev = out.with_name(out.name.split(".")[0] + "_preview.png")
        names = " / ".join(dict.fromkeys(zn.pat.name for zn in zones))
        save_weave_preview(tp, zones, prev, f"{stem}: {names}")
        print(f"  preview:  {prev}")


if __name__ == "__main__":
    main()
