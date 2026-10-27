#!/usr/bin/env python3
"""Regenerate the parameter comparison images used in the README.

    python docs/make_doc_images.py

Each image shows three values of one setting side by side:
  top row    - the outside of the wall, unrolled flat (lighter = closer to you)
  bottom row - a cut through the wall with each strand drawn to scale
"""
from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

import matplotlib
import numpy as np
import trimesh

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wave_script as ws  # noqa: E402

OUT = Path(__file__).resolve().parent / "images"

# single-hue ramp: inner strands (further away) dark, outer strands (closer) light
STRAND = LinearSegmentedColormap.from_list("strand", ["#0b2a5b", "#1f5fbf", "#8fb8f2"])
LIGHT = "#fbf3df"      # light shining through the gaps
INK = "#222222"
MUTED = "#666666"

WIN_X = 9.0            # half-width of the unrolled view (mm)
WIN_Z = 3.2            # half-height of the unrolled view (mm)
Z_CENTER = 9.0


def make_mesh(s: ws.Settings) -> trimesh.Trimesh:
    m = trimesh.creation.cylinder(radius=30, height=20, sections=256)
    m.apply_translation([s.bed_x / 2, s.bed_y / 2, 10])
    return m


def wall_points(s: ws.Settings):
    s = replace(s, base_layers=0, max_height=16, point_spacing=0.08)
    tp = ws.Toolpath()
    ws.build_wall(make_mesh(s), s, tp)
    pts = np.array([[m.x, m.y, m.z, m.layer] for m in tp.moves if m.kind == "wall"])
    cx, cy = s.bed_x / 2, s.bed_y / 2
    r = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    ang = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
    rise, ratio = ws.wall_geometry(s)
    d = math.sqrt(4 * ratio * ws.mm3_per_mm(s) / math.pi)   # round strand, equal area
    return pts, r, ang, d, tp.waves_per_lap - s.phase_shift, rise


def draw_outside(ax, s: ws.Settings):
    """Outside of the wall unrolled flat and rendered like a photo: each strand is a round
    tube, the strand closest to you wins each pixel, and the background shows through
    wherever no strand covers it."""
    pts, r, ang, d, _, rise = wall_points(s)
    # look at the side opposite the lap start so no lap seam falls inside the view
    arc = (np.mod(ang, 2 * math.pi) - math.pi) * 30.0
    zc_all = pts[:, 2] - d / 2
    dev_all = r - 30.0
    nx, nz = 900, 320
    xs = np.linspace(-WIN_X, WIN_X, nx)
    zs = np.linspace(Z_CENTER - WIN_Z, Z_CENTER + WIN_Z, nz)
    best = np.full((nz, nx), -np.inf)          # depth of the closest strand so far
    shade = np.zeros((nz, nx))
    lim = 2.0                                   # same color scale in every panel
    for L in np.unique(pts[:, 3]):
        m = (pts[:, 3] == L) & (np.abs(arc) < WIN_X + 1)
        if m.sum() < 2:
            continue
        a, zc, dv = arc[m], zc_all[m], dev_all[m]
        o = np.argsort(a)
        a, zc, dv = a[o], zc[o], dv[o]
        if abs(np.median(zc) - Z_CENTER) > WIN_Z + d:
            continue
        zcx = np.interp(xs, a, zc)
        dvx = np.interp(xs, a, dv)
        u = (zs[:, None] - zcx[None, :]) / (d / 2)      # -1..1 across the tube
        cover = np.abs(u) < 1
        depth = dvx[None, :] + np.sqrt(np.clip(1 - u ** 2, 0, 1)) * d / 2
        win = cover & (depth > best)
        best[win] = depth[win]
        tube = np.sqrt(np.clip(1 - u ** 2, 0, 1))       # 1 at the tube's crown, 0 at its edge
        shade[win] = (0.35 + 0.65 * tube)[win]
    img = np.ones((nz, nx, 4))
    img[:] = matplotlib.colors.to_rgba(LIGHT)
    hit = np.isfinite(best)
    col = STRAND(np.clip((best[hit] + lim) / (2 * lim + d), 0, 1))
    col[:, :3] *= shade[hit][:, None]
    img[hit] = col
    ax.imshow(img, origin="lower", extent=(-WIN_X, WIN_X, zs[0], zs[-1]), interpolation="bilinear")
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("#bbbbbb")


def draw_cut(ax, s: ws.Settings):
    pts, r, ang, d, n_waves, rise = wall_points(s)
    rate = n_waves + (s.phase_shift if max(1, s.weave_height) == 1 else 0)  # waves per lap
    t_peak = 0.5 + (0.25 - (rate * 0.5) % 1) / rate     # a wave peak, away from the lap start
    theta = 2 * math.pi * t_peak
    dang = np.abs((ang - theta + math.pi) % (2 * math.pi) - math.pi)
    lim = 2.0                                   # same color scale in every panel
    for L in np.unique(pts[:, 3]):
        idx = np.where(pts[:, 3] == L)[0]
        k = idx[np.argmin(dang[idx])]
        zc = pts[k, 2] - d / 2
        if abs(zc - Z_CENTER) < WIN_Z:
            dev = r[k] - 30.0
            ax.add_patch(Circle((dev, zc), d / 2, color=STRAND((dev + lim) / (2 * lim))))
    ax.set_xlim(-3.2, 3.2)
    ax.set_ylim(Z_CENTER - WIN_Z, Z_CENTER + WIN_Z)
    ax.set_aspect("equal")
    ax.set_facecolor(LIGHT)
    ax.axvline(0, color="#cccccc", lw=1, ls="--", zorder=0)
    ax.set_xlabel("inside  <-  CAD surface  ->  outside", color=MUTED, fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("#bbbbbb")


def comparison(name: str, title: str, subtitle: str, variants: list[tuple[str, ws.Settings]]):
    fig, axes = plt.subplots(2, len(variants), figsize=(4.4 * len(variants), 5.6),
                             gridspec_kw={"height_ratios": [1, 1]})
    fig.patch.set_facecolor("white")
    for col, (label, s) in enumerate(variants):
        draw_outside(axes[0, col], s)
        axes[0, col].set_title(label, color=INK, fontsize=12, fontweight="bold", pad=8)
        draw_cut(axes[1, col], s)
    axes[0, 0].set_ylabel("outside view", color=MUTED, fontsize=10)
    axes[1, 0].set_ylabel("cut through\nthe wall", color=MUTED, fontsize=10)
    fig.suptitle(title, x=0.02, ha="left", color=INK, fontsize=15, fontweight="bold", y=0.995)
    fig.text(0.02, 0.905, subtitle, ha="left", color=MUTED, fontsize=10.5)
    fig.subplots_adjust(left=0.07, right=0.99, top=0.82, bottom=0.07, wspace=0.06, hspace=0.12)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print("wrote", path)


def main():
    base = ws.Settings()
    open_ = replace(base, amplitude=1.5, weave_gap=0.3)

    comparison("overview", "Three looks from the same cylinder",
               "Default solid weave, open weave (--weave-gap 0.3), and chain mail (--stagger 2)",
               [("solid (default)", base),
                ("open weave", open_),
                ("chain mail", replace(open_, stagger=2))])

    comparison("amplitude", "--amplitude: how far the wall moves in and out",
               "Bigger = deeper texture and bigger loops. Shown with the default solid weave.",
               [(f"{a} mm", replace(base, amplitude=a)) for a in (0.5, 1.0, 2.0)])

    comparison("wavelength", "--wavelength: length of one wave",
               "Shorter = tighter, finer pattern. Longer = wider loops.",
               [(f"{w} mm", replace(base, wavelength=w)) for w in (3.0, 5.0, 8.0)])

    comparison("phase_shift", "--phase-shift: how much the wave moves each layer",
               "0 = straight ribs, 0.25 = diagonal twist, 0.5 = woven, crossing strands (default).",
               [(f"{p}", replace(base, phase_shift=p)) for p in (0.0, 0.25, 0.5)])

    comparison("weave_gap", "--weave-gap: open space between strands",
               "Off = packed solid wall. Bigger gap = more light through. Shown with --amplitude 1.5.",
               [("off", replace(base, amplitude=1.5)),
                ("0.2 mm", replace(base, amplitude=1.5, weave_gap=0.2)),
                ("0.4 mm", replace(base, amplitude=1.5, weave_gap=0.4))])

    comparison("stagger", "--stagger: offset every other row of loops",
               "0 = loops line up in columns. 2 = chain mail / brick. 4 = taller rows.",
               [("0 (off)", open_), ("2", replace(open_, stagger=2)), ("4", replace(open_, stagger=4))])

    comparison("weave_height", "--weave-height: taller loops",
               "Laps stacked on each side before the strands cross. 1 = classic weave.",
               [(f"{k}", replace(open_, weave_height=k)) for k in (1, 2, 3)])

    comparison("strand_diameter", "--strand-diameter: thickness of each strand (open weave)",
               "Thicker strands = chunkier look and a stronger part. Shown with --weave-gap 0.3.",
               [(f"{d} mm", replace(open_, strand_diameter=d)) for d in (0.4, 0.6, 0.8)])


if __name__ == "__main__":
    main()
