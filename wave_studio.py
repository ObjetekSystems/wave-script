#!/usr/bin/env python3
"""
Wave Studio - a point-and-click window for Wave Script and Weave Script.

No command line needed: pick a model, move the sliders, click Preview to see the
toolpath, then click Generate to write the file for your printer.

    Windows: double-click  "Wave Studio.bat"
    Or:      python wave_studio.py

Wave Studio doesn't change how the scripts work. It builds the same command you
would type and runs wave_script.py or weave_script.py for you, so every print it
makes can be repeated from the command line (click "Show command" to see it).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

__version__ = "0.1.0"

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "wave_studio_settings.json"

try:
    import matplotlib
    matplotlib.use("TkAgg")
    # the arrow keys turn the 3D view; don't let matplotlib use them for back/forward too
    matplotlib.rcParams["keymap.back"] = ["backspace"]
    matplotlib.rcParams["keymap.forward"] = []
    import matplotlib.image as mpimg
    import numpy as np
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    sys.path.insert(0, str(HERE))
    import weave_script                      # also imports wave_script, trimesh, numpy...
    import trimesh
    IMPORT_ERROR = None
except Exception as e:                        # missing package: explain instead of crashing
    IMPORT_ERROR = e


# --------------------------------------------------------------------------- #
# The settings shown in the window
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    key: str               # name used in presets
    flag: str              # command-line option
    label: str
    lo: float
    hi: float
    step: float
    default: float
    help: str = ""
    kind: str = "float"    # float | int
    optional: bool = False # has an on/off box; off = leave the script's own default


# Wave Script: the wave itself
WAVE_SHAPE = [
    Param("amplitude", "--amplitude", "Amplitude (mm)", 0, 5, 0.05, 1.0,
          "How far the wall moves in and out from the CAD surface."),
    Param("wavelength", "--wavelength", "Wavelength (mm)", 1, 30, 0.1, 5.0,
          "Length of one wave. It is rounded so a whole number of waves fits each lap."),
    Param("waves", "--waves", "Exact waves per lap", 3, 300, 1, 40,
          "Tick this to set an exact number of waves per lap (replaces Wavelength).",
          kind="int", optional=True),
    Param("phase_shift", "--phase-shift", "Phase shift per layer", 0, 1, 0.05, 0.5,
          "0.5 = strands cross each other (woven look). 0 = straight vertical ribs."),
    Param("weave_height", "--weave-height", "Weave height (laps)", 1, 6, 1, 1,
          "Laps stacked on each side before the strands cross. Bigger = taller loops.", kind="int"),
    Param("stagger", "--stagger", "Stagger (laps per row)", 0, 6, 1, 0,
          "Shift every row of loops by half a loop for a chain-mail look. 0 = off, try 2.", kind="int"),
    Param("speed", "--speed", "Wall speed (mm/s)", 5, 100, 1, 25,
          "Print speed for the wavy wall."),
]

# Wave Script: plain band at the bottom (needs Wave Script 0.7.3+)
WAVE_SOLID = [
    Param("solid_bottom", "--solid-bottom", "Solid band at the bottom (mm)", 0.5, 100, 0.5, 10,
          "Tick for a plain, smooth wall from the base up to this height; the wave starts above it. "
          "(The flat floor itself is 'Solid base layers' on the Printing tab.)", optional=True),
    Param("solid_blend", "--solid-blend", "Wave fade-in above it (mm)", 0.5, 15, 0.5, 3.0,
          "Height over which the wave grows from flat to full size above the solid band."),
]

# Wave Script: see-through (open) weave
WAVE_OPEN = [
    Param("weave_gap", "--weave-gap", "Gap between strands (mm)", 0, 1.2, 0.05, 0.3,
          "Tick to make an open, see-through weave. Try 0.3-0.5. Can't be bigger than the strand.",
          optional=True),
    Param("strand_diameter", "--strand-diameter", "Strand thickness (mm)", 0.3, 1.2, 0.05, 0.5,
          "Thickness of each strand in an open weave (default = line width).", optional=True),
]

# Wave Script only: advanced
WAVE_ADVANCED = [
    Param("max_overhang", "--max-overhang", "Max overhang per lap (mm)", 0.05, 0.5, 0.01, 0.15,
          "With Adaptive on: how far one lap may stick out past the lap below. Lower = safer."),
    Param("min_layer_height", "--min-layer-height", "Min layer height (mm)", 0.04, 0.2, 0.01, 0.08,
          "With Adaptive on: the thinnest layer it may use on steep sections."),
    Param("first_layer_speed", "--first-layer-speed", "First layer speed (mm/s)", 5, 50, 1, 15,
          "Speed for the very first layer."),
    Param("base_speed", "--base-speed", "Base speed (mm/s)", 5, 100, 1, 30,
          "Speed for the solid bottom layers."),
    Param("fan_layer", "--fan-layer", "Fan on from layer", 1, 10, 1, 2,
          "Turn the part fan on from this layer.", kind="int"),
    Param("rim_fan", "--rim-fan", "Rim fan (0-1)", 0, 1, 0.05, 1.0,
          "Open weave: part-fan level while closing the weave and printing the rim."),
    Param("aux_fan", "--aux-fan", "Bambu side fan (0-1)", 0, 1, 0.05, 0.0,
          "Bambu auxiliary fan. Default off: uneven side cooling can lift the rim."),
]

# Weave Script: per-pattern values (sliders jump to the pattern's own values)
WEAVE_SHAPE = [
    Param("depth", "--depth", "Depth (mm)", 0, 5, 0.05, 1.0,
          "How far the strands move in and out from the surface."),
    Param("cell", "--cell", "Cell / loop width (mm)", 1, 15, 0.1, 2.5,
          "Width of one loop or opening (half a wavelength)."),
    Param("gap", "--gap", "See-through gap (mm)", 0, 1.2, 0.05, 0.0,
          "0 = solid wall. Bigger = open weave. Can't be bigger than the strand."),
    Param("strand", "--strand", "Strand thickness (mm)", 0.3, 1.2, 0.05, 0.5,
          "Thickness of each strand."),
    Param("wspeed", "--speed", "Wall speed (mm/s)", 5, 60, 1, 25,
          "Print speed for the wall."),
    Param("drift", "--drift", "Diamond drift", 0, 0.1, 0.005, 0.0,
          "Diamonds pattern: 0.02 = soft 25 mm diamonds, 0.05 = bold 10 mm diamonds."),
]
WEAVE_FIELD = {"depth": "depth", "cell": "cell", "gap": "gap", "strand": "strand",
               "wspeed": "speed", "drift": "drift"}       # slider key -> Pattern field

WEAVE_EXTRA = [
    Param("node_flow", "--node-flow", "Crossing flow boost", 1.0, 1.5, 0.01, 1.1,
          "Open patterns: extra plastic where strands cross so the joints fuse (1.0 = off)."),
    Param("blend", "--blend", "Zone blend (mm)", 0, 10, 0.5, 3.0,
          "Distance over which the wave fades out and back in where zones meet."),
]

# Both scripts
PRINTING = [
    Param("layer_height", "--layer-height", "Layer height (mm)", 0.08, 0.4, 0.01, 0.2,
          "Layer height for solid walls and the base. Open weaves set their own rise."),
    Param("line_width", "--line-width", "Line width (mm)", 0.3, 1.0, 0.01, 0.5,
          "Width of each extruded line."),
    Param("flow", "--flow", "Flow multiplier", 0.8, 1.3, 0.01, 1.0,
          "Extrusion multiplier. 1.0 = normal."),
    Param("base_layers", "--base-layers", "Solid base layers", 0, 10, 1, 3,
          "Flat solid bottom layers. 0 = open bottom (lamp shades).", kind="int"),
    Param("top_layers", "--top-layers", "Solid rim laps on top", 0, 10, 1, 2,
          "Solid laps at the top so the weave can't unravel. 0 = none.", kind="int"),
    Param("densify_height", "--densify-height", "Densify band at top (mm)", 0, 10, 0.5, 2.0,
          "Open weaves: height below the rim where the gaps close into a dense band. 0 = off."),
    Param("valley_depth", "--valley-depth", "Valley depth (mm)", 0, 5, 0.05, 0.0,
          "Open weaves: how far the top edge dips between crossings, so the rim fills level. "
          "Leave unticked to use the script's value.", optional=True),
    Param("valley_slope", "--valley-slope", "Valley slope", 0.5, 2.5, 0.05, 1.3,
          "Steepest dip into a valley: 1.0 = 45 degrees, 1.3 = 52 degrees."),
    Param("max_height", "--max-height", "Stop at height (mm)", 1, 300, 1, 20,
          "Tick to stop the print at this height - handy for quick test prints.", optional=True),
]

PRINTER = [
    Param("seam_angle", "--seam-angle", "Seam angle (degrees)", 0, 355, 5, 0,
          "Where each lap starts: 0 = right, 90 = back, 180 = left, 270 = front."),
    Param("nozzle_temp", "--nozzle-temp", "Nozzle temp (C)", 170, 320, 1, 215,
          "Generic printers only. Bambu uses the template's temperatures.", kind="int"),
    Param("bed_temp", "--bed-temp", "Bed temp (C)", 0, 120, 1, 60,
          "Generic printers only.", kind="int"),
    Param("filament_diameter", "--filament-diameter", "Filament diameter (mm)", 1.75, 2.85, 0.05, 1.75,
          "1.75 for almost every modern printer."),
]


BAMBU_HELP = (
    "Bambu printers need a template once per model:\n\n"
    "1. In Bambu Studio, pick your printer, plate and filament.\n"
    "2. Import the SAME model file. Don't move, rotate or scale it.\n"
    "3. Click Slice plate.\n"
    "4. File > Export > Export plate sliced file  (a .gcode.3mf).\n\n"
    "Choose that file as the template here. The template supplies the start sequence, "
    "temperatures and filament; Wave Studio supplies every wall move.\n\n"
    "Open the result in Bambu Studio and click Print plate. Don't change anything in the "
    "Prepare tab, or Bambu Studio will try to re-slice it.")


THEMES = {
    "light": dict(bg="#f0f0f0", panel="#f0f0f0", field="#ffffff", fg="#000000", dim="#555555",
                  border="#a0a0a0", trough="#d9d9d9", select="#cce4ff", accent="#1a5fb4",
                  log="#f6f6f6", tip="#ffffe0", warn="#b05a00", err="#c00000", cmd="#1a5fb4",
                  plot="#ffffff", plot_text="#777777"),
    "dark": dict(bg="#1e1f22", panel="#2b2d31", field="#232428", fg="#e6e6e6", dim="#a0a4ab",
                 border="#45484e", trough="#3a3d42", select="#3d5a80", accent="#6aa6ff",
                 log="#18191c", tip="#3a3d42", warn="#ffb35c", err="#ff6b6b", cmd="#7fb2ff",
                 plot="#1e1f22", plot_text="#a0a4ab"),
}
THEME = dict(THEMES["light"])            # the colors in use right now (tooltips read this)


def decimals(step: float) -> int:
    t = f"{step:g}"
    return len(t.split(".")[1]) if "." in t else 0


# --------------------------------------------------------------------------- #
# Small widgets
# --------------------------------------------------------------------------- #
class Tooltip:
    """Help text that pops up when the mouse rests on a widget."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _schedule(self, _e=None):
        self.job = self.widget.after(450, self._show)

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background=THEME["tip"], foreground=THEME["fg"], relief="solid",
                 borderwidth=1, wraplength=320, padx=6, pady=4).pack()

    def _hide(self, _e=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Slider:
    """Label + slider + number box. Optional ones get a tick box in front."""

    def __init__(self, parent, row: int, p: Param, on_change):
        self.p, self.on_change = p, on_change
        self.dec = 0 if p.kind == "int" else decimals(p.step)
        self.value = p.default
        self.use = tk.BooleanVar(value=False)
        if p.optional:
            lab = ttk.Checkbutton(parent, text=p.label, variable=self.use, command=self._toggled)
        else:
            lab = ttk.Label(parent, text=p.label)
        lab.grid(row=row, column=0, sticky="w", padx=(4, 6), pady=1)
        Tooltip(lab, p.help)
        self.var = tk.DoubleVar(value=p.default)
        self.scale = tk.Scale(parent, from_=p.lo, to=p.hi, resolution=p.step, orient="horizontal",
                              showvalue=False, variable=self.var, length=190,
                              command=self._moved, highlightthickness=0)
        self.scale.grid(row=row, column=1, sticky="ew", pady=1)
        self.scale.bind("<ButtonRelease-1>", lambda e: self.on_change())
        Tooltip(self.scale, p.help)
        self.text = tk.StringVar(value=self.fmt(p.default))
        self.entry = ttk.Entry(parent, textvariable=self.text, width=7, justify="right")
        self.entry.grid(row=row, column=2, padx=4, pady=1)
        self.entry.bind("<Return>", self._typed)
        self.entry.bind("<FocusOut>", self._typed)
        self.use.trace_add("write", lambda *a: self._toggled(notify=False))
        self._toggled(notify=False)

    def fmt(self, v: float) -> str:
        return str(int(round(v))) if self.p.kind == "int" else f"{v:.{self.dec}f}"

    def _moved(self, v):
        self.value = float(v)
        self.text.set(self.fmt(self.value))

    def _typed(self, _e=None):
        try:
            v = float(self.text.get())
        except ValueError:
            self.text.set(self.fmt(self.value))
            return
        changed = abs(v - self.value) > 1e-12
        self.set(v)
        if changed:
            self.on_change()

    def _toggled(self, notify=True):
        state = "normal" if (self.use.get() or not self.p.optional) else "disabled"
        self.scale.configure(state=state)
        self.entry.configure(state=state)
        if notify:
            self.on_change()

    def set(self, v: float):
        self.value = int(round(v)) if self.p.kind == "int" else v
        self.var.set(v)                       # the slider shows the nearest point in range
        self.text.set(self.fmt(self.value))

    @property
    def active(self) -> bool:
        return self.use.get() or not self.p.optional

    def arg(self) -> str:
        return self.fmt(self.value)


class ScrollFrame(ttk.Frame):
    """A frame with a vertical scroll bar (for the long lists of sliders)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.inner.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.inner.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class WaveStudio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Wave Studio {__version__}  -  Wave Script / Weave Script")
        self.geometry("1380x880")
        self.minsize(1100, 700)
        self.sliders: dict[str, Slider] = {}
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="wave_studio_"))
        self.model_height: float | None = None
        self.output_custom = False
        self.last_output: Path | None = None
        self.last_preview: Path | None = None
        self.auto_job = None
        self.pending_preview = False

        self.mode = tk.StringVar(value="wave")          # wave | weave
        self.printer = tk.StringVar(value="bambu")      # bambu | generic
        self.model = tk.StringVar()
        self.template = tk.StringVar()
        self.output = tk.StringVar()
        self.pattern = tk.StringVar(value="lattice")
        self.zones_text = tk.StringVar()
        self.bottom_band = tk.BooleanVar(value=False)
        self.top_band = tk.BooleanVar(value=False)
        self.adaptive = tk.BooleanVar(value=False)
        self.flip = tk.BooleanVar(value=False)
        self.bed_x = tk.StringVar(value="256")
        self.bed_y = tk.StringVar(value="256")
        self.quick = tk.BooleanVar(value=True)
        self.auto = tk.BooleanVar(value=False)
        self.dark = tk.BooleanVar(value=False)
        self.view = tk.StringVar(value="picture")       # picture | 3d
        self.show_base = tk.BooleanVar(value=False)
        self.last_gcode: Path | None = None
        self.paths = None                               # (wall, base) points for the 3D view
        self.paths_key = None
        self.ax3d = None
        self.zoom = 1.0
        self.status = tk.StringVar(value="Pick a model to start.")
        self.summary = tk.StringVar(value="")

        self._build()
        self.load_settings(SETTINGS_FILE, quiet=True)
        self._mode_changed(notify=False)
        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._pump)

    # ----------------------------------------------------------------- layout
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.light_theme = style.theme_use()
        if self.light_theme == "clam":              # dark mode is built on clam
            self.light_theme = "default"
            style.theme_use("default")
        self._style_fonts()

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(pw, width=520)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)

        # --- 1. model / printer / output
        f = ttk.LabelFrame(left, text=" 1. Model and printer ")
        f.pack(fill="x", padx=2, pady=(0, 6))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Model (.stl/.3mf/.obj)").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(f, textvariable=self.model).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(f, text="Browse...", command=self.pick_model).grid(row=0, column=2, padx=4)
        self.size_label = ttk.Label(f, text="", style="Dim.TLabel")
        self.size_label.grid(row=1, column=1, sticky="w")

        pr = ttk.Frame(f)
        pr.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        ttk.Label(pr, text="Printer:").pack(side="left")
        ttk.Radiobutton(pr, text="Bambu (.gcode.3mf)", value="bambu", variable=self.printer,
                        command=self._printer_changed).pack(side="left", padx=6)
        ttk.Radiobutton(pr, text="Other printer (.gcode)", value="generic", variable=self.printer,
                        command=self._printer_changed).pack(side="left", padx=6)

        self.tpl_label = ttk.Label(f, text="Bambu template")
        self.tpl_label.grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self.tpl_entry = ttk.Entry(f, textvariable=self.template)
        self.tpl_entry.grid(row=3, column=1, sticky="ew", pady=2)
        tb = ttk.Frame(f)
        tb.grid(row=3, column=2, padx=4)
        self.tpl_btn = ttk.Button(tb, text="Browse...", command=self.pick_template)
        self.tpl_btn.pack(side="left")
        ttk.Button(tb, text="?", width=2, command=lambda: messagebox.showinfo(
            "Bambu template", BAMBU_HELP)).pack(side="left", padx=(2, 0))

        ttk.Label(f, text="Save as").grid(row=4, column=0, sticky="w", padx=4, pady=2)
        out_e = ttk.Entry(f, textvariable=self.output)
        out_e.grid(row=4, column=1, sticky="ew", pady=2)
        out_e.bind("<Key>", lambda e: setattr(self, "output_custom", True))
        ttk.Button(f, text="Browse...", command=self.pick_output).grid(row=4, column=2, padx=4)

        # --- 2. script choice
        m = ttk.LabelFrame(left, text=" 2. Choose the script ")
        m.pack(fill="x", padx=2, pady=(0, 6))
        r1 = ttk.Radiobutton(m, text="Wave Script  -  every setting by hand", value="wave",
                             variable=self.mode, command=self._mode_changed)
        r1.pack(anchor="w", padx=4)
        r2 = ttk.Radiobutton(m, text="Weave Script  -  pick a named pattern (easier)", value="weave",
                             variable=self.mode, command=self._mode_changed)
        r2.pack(anchor="w", padx=4, pady=(0, 2))

        # --- 3. settings tabs
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True, padx=2)
        self.pattern_tab = ScrollFrame(nb)
        printing_tab = ScrollFrame(nb)
        printer_tab = ScrollFrame(nb)
        nb.add(self.pattern_tab, text="  Pattern  ")
        nb.add(printing_tab, text="  Printing  ")
        nb.add(printer_tab, text="  Printer & placement  ")

        # wave pattern frame
        self.wave_frame = ttk.Frame(self.pattern_tab.inner)
        rb = ttk.Frame(self.wave_frame)
        rb.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(rb, text="Every Wave Script setting, by hand.", style="Dim.TLabel").pack(side="left")
        ttk.Button(rb, text="Reset to defaults", command=self.reset_wave).pack(side="right")
        self._section(self.wave_frame, "Wave shape", WAVE_SHAPE)
        self._section(self.wave_frame, "Solid band at the bottom (optional)", WAVE_SOLID)
        self._section(self.wave_frame, "Open, see-through weave (optional)", WAVE_OPEN)
        adv = self._section(self.wave_frame, "Advanced (Wave Script only)", WAVE_ADVANCED)
        cb = ttk.Checkbutton(adv, text="Adaptive layers on steep walls", variable=self.adaptive,
                             command=self._changed)
        cb.grid(row=99, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        Tooltip(cb, "Measures the wall angle and uses thinner layers and slower speed on steep "
                    "sections. Solid walls only.")

        # weave pattern frame
        self.weave_frame = ttk.Frame(self.pattern_tab.inner)
        pf = ttk.LabelFrame(self.weave_frame, text=" Pattern ")
        pf.pack(fill="x", padx=4, pady=4)
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="Pattern").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        names = list(weave_script.PATTERNS) if IMPORT_ERROR is None else ["lattice"]
        cbx = ttk.Combobox(pf, textvariable=self.pattern, values=names, state="readonly", width=16)
        cbx.grid(row=0, column=1, sticky="w", pady=2)
        cbx.bind("<<ComboboxSelected>>", lambda e: self._pattern_changed())
        ttk.Button(pf, text="Reset to pattern values", command=self._pattern_changed).grid(
            row=0, column=2, padx=4)
        self.pattern_about = ttk.Label(pf, text="", wraplength=420, style="Dim.TLabel")
        self.pattern_about.grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 4))
        self._section(self.weave_frame, "Pattern values (start at the pattern's own)", WEAVE_SHAPE)
        self._section(self.weave_frame, "Joints and zones", WEAVE_EXTRA)

        zf = ttk.LabelFrame(self.weave_frame, text=" Solid bands (optional) ")
        zf.pack(fill="x", padx=4, pady=4)
        zf.columnconfigure(1, weight=1)
        self.bottom_mm = self._mini_slider(zf, 0, self.bottom_band, "Solid band at the bottom (mm)",
                                           "A plain smooth band from the bed up to this height.")
        self.top_mm = self._mini_slider(zf, 1, self.top_band, "Solid band at the top (mm)",
                                        "A plain smooth band of this height at the top of the model.")
        ttk.Label(zf, text="Or type zones (overrides the bands):").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(6, 0))
        ze = ttk.Entry(zf, textvariable=self.zones_text)
        ze.grid(row=3, column=0, columnspan=3, sticky="ew", padx=4, pady=(0, 4))
        ze.bind("<Return>", lambda e: self._changed())
        Tooltip(ze, 'Example:  0-15:solid, 15-70:lattice, 70-:solid\n'
                    'Heights are mm from the bed, or % of the model height. '
                    'Add @depth=2,cell=6 after a pattern to tweak just that zone.')

        # printing / printer tabs
        self._section(printing_tab.inner, "Layers and walls", PRINTING)
        pt = self._section(printer_tab.inner, "Printer and placement", PRINTER)
        ttk.Checkbutton(pt, text="Print the model upside down", variable=self.flip,
                        command=self._changed).grid(row=90, column=0, columnspan=3, sticky="w", padx=4)
        bf = ttk.Frame(pt)
        bf.grid(row=91, column=0, columnspan=3, sticky="w", padx=4, pady=4)
        ttk.Label(bf, text="Bed size X / Y (mm)").pack(side="left")
        ttk.Entry(bf, textvariable=self.bed_x, width=6).pack(side="left", padx=4)
        ttk.Entry(bf, textvariable=self.bed_y, width=6).pack(side="left")
        ttk.Label(bf, text="  (P2S 256, Neptune 4 Max 420)", style="Dim.TLabel").pack(side="left")

        # --- 4. buttons
        b = ttk.LabelFrame(left, text=" 3. Preview, then generate ")
        b.pack(fill="x", padx=2, pady=(6, 0))
        row = ttk.Frame(b)
        row.pack(fill="x", padx=4, pady=4)
        self.prev_btn = ttk.Button(row, text="Preview", style="Big.TButton",
                                   command=lambda: self.run(final=False))
        self.prev_btn.pack(side="left")
        self.gen_btn = ttk.Button(row, text="Generate file", style="Big.TButton",
                                  command=lambda: self.run(final=True))
        self.gen_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        opts = ttk.Frame(b)
        opts.pack(fill="x", padx=4)
        self.quick_mm = self._mini_slider(opts, 0, self.quick, "Quick preview: first (mm)",
                                          "Preview only the bottom part of the print - much faster. "
                                          "Untick to preview the whole height.", default=20, hi=150)
        acb = ttk.Checkbutton(opts, text="Auto-preview when a setting changes", variable=self.auto)
        acb.grid(row=1, column=0, columnspan=3, sticky="w", padx=4)
        row2 = ttk.Frame(b)
        row2.pack(fill="x", padx=4, pady=4)
        ttk.Button(row2, text="Save preset...", command=self.save_preset).pack(side="left")
        ttk.Button(row2, text="Load preset...", command=self.load_preset).pack(side="left", padx=4)
        ttk.Button(row2, text="Show command", command=self.show_command).pack(side="left")
        row3 = ttk.Frame(b)
        row3.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(row3, text="After generating:", style="Dim.TLabel").pack(side="left")
        self.folder_btn = ttk.Button(row3, text="Open folder", command=self.open_folder,
                                     state="disabled")
        self.folder_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(row3, text="Open file (Bambu Studio)", command=self.open_output,
                                   state="disabled")
        self.open_btn.pack(side="left")

        # --- right side: preview + log
        top = ttk.Frame(right)
        top.pack(fill="x")
        # pack the tick box first so a long status message can never push it off screen
        ttk.Checkbutton(top, text="Dark mode", variable=self.dark,
                        command=self.apply_theme).pack(side="right", padx=6)
        ttk.Label(top, textvariable=self.status, style="Head.TLabel").pack(side="left", padx=4)
        ttk.Label(right, textvariable=self.summary).pack(anchor="w", padx=4)
        vb = ttk.Frame(right)
        vb.pack(fill="x", padx=4, pady=(2, 2))
        ttk.Label(vb, text="View:").pack(side="left")
        ttk.Radiobutton(vb, text="Picture", value="picture", variable=self.view,
                        command=self.refresh_view).pack(side="left", padx=(4, 2))
        r3 = ttk.Radiobutton(vb, text="3D (drag to turn)", value="3d", variable=self.view,
                             command=self.refresh_view)
        r3.pack(side="left", padx=2)
        Tooltip(r3, "Turn and tilt the toolpath like in CAD: drag with the left mouse button. "
                    "Mouse wheel zooms. Right-drag also zooms.")
        presets = ttk.Frame(vb)
        presets.pack(side="left", padx=(10, 0))
        for name, elev in (("Side", 4), ("Angled", 25), ("High", 55), ("Top", 89)):
            ttk.Button(presets, text=name, width=7,
                       command=lambda e=elev: self.set_view(e)).pack(side="left", padx=1)
        # second row: hold-down buttons for people without a mouse (trackpad or touch)
        vb2 = ttk.Frame(right)
        vb2.pack(fill="x", padx=4, pady=(0, 2))
        moves = ttk.Frame(vb2)
        moves.pack(side="left")

        def hold(parent, text, cmd, tip):
            b = tk.Button(parent, text=text, width=3, command=cmd, repeatdelay=350,
                          repeatinterval=60, relief="groove")
            b.pack(side="left", padx=1)
            Tooltip(b, tip)
            return b
        ttk.Label(moves, text="Turn").pack(side="left", padx=(0, 2))
        hold(moves, "<", lambda: self.nudge(azim=-5), "Turn left (hold to keep turning). Key: Left arrow")
        hold(moves, ">", lambda: self.nudge(azim=5), "Turn right (hold to keep turning). Key: Right arrow")
        ttk.Label(moves, text="  Tilt").pack(side="left", padx=(0, 2))
        hold(moves, "^", lambda: self.nudge(elev=5), "Tilt to look down from higher. Key: Up arrow")
        hold(moves, "v", lambda: self.nudge(elev=-5), "Tilt to look from lower. Key: Down arrow")
        ttk.Label(moves, text="  Zoom").pack(side="left", padx=(0, 2))
        hold(moves, "+", lambda: self.zoom_by(1.08), "Zoom in. Keys: + or =, or two-finger scroll")
        hold(moves, "-", lambda: self.zoom_by(1 / 1.08), "Zoom out. Key: -")
        ttk.Button(moves, text="Reset view", command=self.reset_view).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(moves, text="Show base", variable=self.show_base,
                        command=lambda: self.draw_3d(keep_view=True)).pack(side="left", padx=8)
        self.view_btns = [presets, moves]
        for key, fn in (("<Left>", lambda: self.nudge(azim=-5)), ("<Right>", lambda: self.nudge(azim=5)),
                        ("<Up>", lambda: self.nudge(elev=5)), ("<Down>", lambda: self.nudge(elev=-5)),
                        ("<plus>", lambda: self.zoom_by(1.1)), ("<equal>", lambda: self.zoom_by(1.1)),
                        ("<KP_Add>", lambda: self.zoom_by(1.1)), ("<minus>", lambda: self.zoom_by(1 / 1.1)),
                        ("<KP_Subtract>", lambda: self.zoom_by(1 / 1.1)), ("<Home>", self.reset_view)):
            self.bind_all(key, lambda e, f=fn: self._view_key(e, f), add="+")
        self.fig = Figure(figsize=(8, 6), dpi=100) if IMPORT_ERROR is None else None
        if self.fig is not None:
            self.ax = self.fig.add_axes([0, 0, 1, 1])
            self.ax.axis("off")
            self.placeholder = self.ax.text(0.5, 0.5, "The preview appears here.\n\nPick a model, then click Preview.",
                         ha="center", va="center", fontsize=13, color="#777")
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            self.canvas.mpl_connect("scroll_event", self._scroll)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.tbar = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
            self.tbar.update()
            self.tbar.pack(fill="x")
        logf = ttk.Frame(right)
        logf.pack(fill="x")
        self.log = tk.Text(logf, height=9, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")

    def _section(self, parent, title: str, params: list[Param]):
        f = ttk.LabelFrame(parent, text=f" {title} ")
        f.pack(fill="x", padx=4, pady=4)
        f.columnconfigure(1, weight=1)
        for i, p in enumerate(params):
            self.sliders[p.key] = Slider(f, i, p, self._changed)
        return f

    def _mini_slider(self, parent, row, boolvar, label, help_, default=10.0, hi=100):
        p = Param("_" + label, "", label, 1, hi, 1, default, help_, optional=True)
        s = Slider(parent, row, p, self._changed)
        s.use = boolvar
        boolvar.trace_add("write", lambda *a: s._toggled(notify=False))
        # re-wire the tick box to the given variable
        for w in parent.grid_slaves(row=row, column=0):
            w.configure(variable=boolvar)
        s._toggled(notify=False)
        return s

    # ----------------------------------------------------------------- theme
    def _style_fonts(self):
        style = ttk.Style(self)
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(10, 6))
        style.configure("Head.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Dim.TLabel", foreground=THEME["dim"])

    def apply_theme(self):
        """Switch every part of the window between light and dark."""
        dark = self.dark.get()
        THEME.clear()
        THEME.update(THEMES["dark" if dark else "light"])
        c = THEME
        style = ttk.Style(self)
        if dark:
            style.theme_use("clam")
            style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                            bordercolor=c["border"], lightcolor=c["panel"], darkcolor=c["panel"],
                            troughcolor=c["trough"], selectbackground=c["select"],
                            selectforeground=c["fg"], insertcolor=c["fg"], arrowcolor=c["fg"],
                            focuscolor=c["accent"])
            style.map(".", foreground=[("disabled", "#6b6f76")],
                      background=[("disabled", c["bg"])])
            style.configure("TButton", background=c["panel"], bordercolor=c["border"],
                            padding=(6, 3))
            style.map("TButton", background=[("pressed", c["select"]), ("active", "#383a40"),
                                             ("disabled", c["bg"])])
            style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"])
            style.map("TEntry", fieldbackground=[("disabled", c["bg"])])
            style.configure("TCombobox", fieldbackground=c["field"], background=c["panel"])
            style.map("TCombobox", fieldbackground=[("readonly", c["field"])],
                      foreground=[("readonly", c["fg"])], selectbackground=[("readonly", c["field"])])
            for w in ("TCheckbutton", "TRadiobutton"):
                style.configure(w, indicatorbackground=c["field"], indicatorforeground=c["fg"])
                style.map(w, background=[("active", c["bg"])],
                          indicatorbackground=[("selected", c["accent"]), ("disabled", c["bg"])])
            style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
            style.configure("TNotebook.Tab", background=c["panel"], foreground=c["dim"])
            style.map("TNotebook.Tab", background=[("selected", c["bg"])],
                      foreground=[("selected", c["fg"])])
            style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
            style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
            style.configure("Vertical.TScrollbar", background=c["panel"], troughcolor=c["bg"])
        else:
            style.theme_use(self.light_theme)
        self._style_fonts()
        self.configure(background=c["bg"])
        for opt, val in (("background", c["field"]), ("foreground", c["fg"]),
                         ("selectBackground", c["select"]), ("selectForeground", c["fg"])):
            self.option_add(f"*TCombobox*Listbox.{opt}", val)
        self._paint(self)
        self.log.configure(background=c["log"], foreground=c["fg"], insertbackground=c["fg"],
                           selectbackground=c["select"])
        for tag in ("warn", "err", "cmd"):
            self.log.tag_configure(tag, foreground=c[tag])
        if self.fig is not None:
            self.fig.set_facecolor(c["plot"])
            if self.placeholder is not None:
                self.placeholder.set_color(c["plot_text"])
            self.canvas.get_tk_widget().configure(background=c["plot"])
            for b in getattr(self.tbar, "_buttons", {}).values():
                try:                                  # matplotlib picks light icons on dark
                    self.tbar._set_image_for_button(b)
                except Exception:
                    pass
            if self.view.get() == "3d" and self.last_gcode is not None:
                self.draw_3d(keep_view=True)
            elif self.last_preview is not None:
                self.show_image(self.last_preview)      # redraw the picture in the new colors
            else:
                self.canvas.draw_idle()
        dark_title_bar(self, dark)

    def _paint(self, w):
        """Plain tk widgets (sliders, scroll areas, the plot toolbar) don't follow ttk themes."""
        c = THEME
        cls = w.winfo_class()
        try:
            if cls in ("Frame", "Canvas", "Tk", "Toplevel"):
                w.configure(background=c["bg"])
                if cls == "Canvas":
                    w.configure(highlightthickness=0)
            elif cls == "Scale":
                w.configure(background=c["bg"], foreground=c["fg"], troughcolor=c["trough"],
                            activebackground=c["accent"], highlightbackground=c["bg"])
            elif cls in ("Label", "Button", "Checkbutton"):
                w.configure(background=c["bg"], foreground=c["fg"], activebackground=c["panel"],
                            activeforeground=c["fg"])
                if cls == "Checkbutton":
                    w.configure(selectcolor=c["select"])
        except tk.TclError:
            pass
        for child in w.winfo_children():
            self._paint(child)

    # ------------------------------------------------------------- reactions
    def _changed(self):
        if self.auto.get() and self.model.get():
            if self.auto_job:
                self.after_cancel(self.auto_job)
            self.auto_job = self.after(700, self._auto_preview)

    def _auto_preview(self):
        self.auto_job = None
        if self.proc:
            self.pending_preview = True
            self.stop(quiet=True)
        else:
            self.run(final=False)

    def _mode_changed(self, notify=True):
        weave = self.mode.get() == "weave"
        (self.wave_frame.pack_forget if weave else self.weave_frame.pack_forget)()
        (self.weave_frame if weave else self.wave_frame).pack(fill="x")
        if weave:
            self._pattern_about()
        self._suggest_output()
        if notify:
            self._changed()

    def _pattern_about(self):
        if IMPORT_ERROR is None:
            pat = weave_script.PATTERNS[self.pattern.get()]
            kind = "open (see-through)" if pat.gap > 0 else "solid wall"
            self.pattern_about.configure(text=f"{pat.about}.  ({kind}, {pat.waveform} wave)")

    def _pattern_changed(self):
        if IMPORT_ERROR is None:
            pat = weave_script.PATTERNS[self.pattern.get()]
            for key, field in WEAVE_FIELD.items():
                self.sliders[key].set(getattr(pat, field))
            self._pattern_about()
        self._changed()

    def reset_wave(self):
        """Put every Wave Script setting on the Pattern tab back to the script's defaults."""
        for p in WAVE_SHAPE + WAVE_SOLID + WAVE_OPEN + WAVE_ADVANCED:
            sl = self.sliders[p.key]
            sl.set(p.default)
            if p.optional:
                sl.use.set(False)
        self.adaptive.set(False)
        self.status.set("Wave settings reset to defaults.")
        self._changed()

    def _printer_changed(self):
        state = "normal" if self.printer.get() == "bambu" else "disabled"
        for w in (self.tpl_entry, self.tpl_btn):
            w.configure(state=state)
        self._suggest_output()
        self._changed()

    def _suggest_output(self):
        if self.output_custom or not self.model.get():
            return
        m = Path(self.model.get())
        ext = ".gcode.3mf" if self.printer.get() == "bambu" else ".gcode"
        self.output.set(str(m.with_name(f"{m.stem}_{self.mode.get()}{ext}")))

    # ----------------------------------------------------------------- files
    def pick_model(self):
        p = filedialog.askopenfilename(title="Choose a model",
                                       initialdir=self._start_dir(self.model.get(), "examples"),
                                       filetypes=[("3D models", "*.stl *.3mf *.obj"), ("All files", "*.*")])
        if p:
            self.set_model(p)

    def set_model(self, p: str):
        self.model.set(p)
        self.output_custom = False
        self._suggest_output()
        self.model_height = None
        self.size_label.configure(text="")
        try:
            mesh = trimesh.load(p, force="mesh")
            x, y, z = mesh.extents
            self.model_height = float(z)
            self.size_label.configure(text=f"Size: {x:.1f} x {y:.1f} x {z:.1f} mm")
            self.top_mm.scale.configure(to=max(10, int(z)))
            self.bottom_mm.scale.configure(to=max(10, int(z)))
            self.quick_mm.scale.configure(to=max(10, int(z)))
            self.sliders["solid_bottom"].scale.configure(to=max(10, int(z)))
        except Exception as e:
            self.size_label.configure(text=f"Couldn't read the model: {e}")
        self.status.set("Model loaded. Click Preview.")
        self._changed()

    def pick_template(self):
        p = filedialog.askopenfilename(title="Choose the Bambu plate sliced file",
                                       initialdir=self._start_dir(self.template.get(), "examples"),
                                       filetypes=[("Bambu plate sliced file", "*.gcode.3mf *.3mf"),
                                                  ("All files", "*.*")])
        if p:
            self.template.set(p)

    def pick_output(self):
        bambu = self.printer.get() == "bambu"
        cur = Path(self.output.get()) if self.output.get() else None
        p = filedialog.asksaveasfilename(
            title="Save the print file as",
            initialdir=str(cur.parent) if cur else str(HERE),
            initialfile=cur.name if cur else "",
            defaultextension=".gcode.3mf" if bambu else ".gcode",
            filetypes=[("Bambu sliced file", "*.gcode.3mf")] if bambu else [("G-code", "*.gcode")])
        if p:
            if bambu and not p.endswith(".gcode.3mf"):
                p = p[:-4] + ".gcode.3mf" if p.endswith(".3mf") else p + ".gcode.3mf"
            self.output.set(p)
            self.output_custom = True

    @staticmethod
    def _start_dir(current: str, fallback: str) -> str:
        if current and Path(current).parent.exists():
            return str(Path(current).parent)
        return str(HERE / fallback if (HERE / fallback).exists() else HERE)

    # ------------------------------------------------------------- arguments
    def build_args(self, final: bool, out: Path) -> list[str] | None:
        weave = self.mode.get() == "weave"
        model = Path(self.model.get())
        args = [str(model), "-o", str(out)]
        if final and self.printer.get() == "bambu":
            args += ["--bambu-template", self.template.get()]

        def add(p: Param):
            s = self.sliders[p.key]
            if not s.active:
                return
            if not p.optional and abs(s.value - p.default) < 1e-9:
                return                              # same as the script's default
            args.extend([p.flag, s.arg()])

        if weave:
            args += ["--pattern", self.pattern.get()]
            pat = weave_script.PATTERNS[self.pattern.get()]
            for p in WEAVE_SHAPE:                   # only values moved away from the pattern's
                s = self.sliders[p.key]
                if abs(s.value - getattr(pat, WEAVE_FIELD[p.key])) > 1e-9:
                    args.extend([p.flag, s.arg()])
            for p in WEAVE_EXTRA:
                add(p)
            zones = self.zones_string()
            if zones:
                args += ["--zones", zones]
        else:
            for p in WAVE_SHAPE + WAVE_SOLID + WAVE_OPEN + WAVE_ADVANCED:
                add(p)
            if self.adaptive.get():
                args.append("--adaptive")
        for p in PRINTING + PRINTER:
            add(p)
        if self.flip.get():
            args.append("--flip")
        try:
            bx, by = float(self.bed_x.get()), float(self.bed_y.get())
        except ValueError:
            messagebox.showerror("Bed size", "Bed size X and Y must be numbers (mm).")
            return None
        if (bx, by) != (256.0, 256.0):
            args += ["--bed-size", f"{bx:g}", f"{by:g}"]
        if not final and self.quick.get():
            lim = self.quick_mm.value
            ms = self.sliders["max_height"]
            if not ms.active or ms.value > lim:
                if "--max-height" in args:
                    i = args.index("--max-height")
                    del args[i:i + 2]
                args += ["--max-height", f"{lim:g}"]
        return args

    def zones_string(self) -> str:
        typed = self.zones_text.get().strip()
        if typed:
            return typed
        parts, pat = [], self.pattern.get()
        lo = self.bottom_mm.value if self.bottom_band.get() else 0
        if lo:
            parts.append(f"0-{lo:g}:solid")
        if self.top_band.get():
            h = self.model_height
            if h is None:
                return ", ".join(parts)
            hi = max(lo + 1, h - self.top_mm.value)
            parts.append(f"{lo:g}-{hi:.1f}:{pat}")
            parts.append(f"{hi:.1f}-:solid")
        return ", ".join(parts)

    def check_inputs(self, final: bool) -> bool:
        model = self.model.get()
        if not model or not Path(model).exists():
            messagebox.showwarning("No model", "Choose a model file first.")
            return False
        if final and self.printer.get() == "bambu":
            t = self.template.get()
            if not t or not Path(t).exists():
                messagebox.showwarning("Bambu template needed",
                                       "Choose the Bambu template file first.\n\n" + BAMBU_HELP)
                return False
        if final and not self.output.get():
            messagebox.showwarning("Save as", "Choose where to save the print file.")
            return False
        # catch the most common mistake before running
        if self.mode.get() == "weave":
            gap, strand = self.sliders["gap"].value, self.sliders["strand"].value
        else:
            g, d = self.sliders["weave_gap"], self.sliders["strand_diameter"]
            gap = g.value if g.active else 0
            strand = d.value if d.active else self.sliders["line_width"].value
        if gap > strand + 1e-9:
            messagebox.showwarning("Gap too big",
                                   f"The gap ({gap:g} mm) can't be bigger than the strand "
                                   f"({strand:g} mm), or the strands won't touch where they cross.")
            return False
        return True

    def command_text(self, args: list[str]) -> str:
        script = "weave_script.py" if self.mode.get() == "weave" else "wave_script.py"

        def q(a):
            return f'"{a}"' if (" " in a or "," in a) else a
        return "python " + script + " " + " ".join(q(a) for a in args)

    # ----------------------------------------------------------------- running
    def run(self, final: bool):
        if self.proc:
            return
        if not self.check_inputs(final):
            return
        model = Path(self.model.get())
        if final:
            out = Path(self.output.get())
            out.parent.mkdir(parents=True, exist_ok=True)
        else:
            out = self.tmpdir / f"{model.stem}_{self.mode.get()}.gcode"
        args = self.build_args(final, out)
        if args is None:
            return
        script = HERE / ("weave_script.py" if self.mode.get() == "weave" else "wave_script.py")
        self.final = final
        self.out = out
        self.preview_png = out.with_name(out.name.split(".")[0] + "_preview.png")
        if self.preview_png.exists():
            self.preview_png.unlink()

        self.log.delete("1.0", "end")
        self.log.insert("end", self.command_text(args) + "\n\n", "cmd")
        what = "Generating the print file" if final else "Making the preview"
        self.status.set(f"{what}...  (a few seconds; tall models take longer)")
        self.summary.set("")
        for b in (self.prev_btn, self.gen_btn):
            b.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        env = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
        self.proc = subprocess.Popen(
            [python_exe(), "-u", str(script), *args], cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.lines.put(("line", line))
        proc.wait()
        self.lines.put(("done", proc.returncode))

    def _pump(self):
        try:
            while True:
                kind, val = self.lines.get_nowait()
                if kind == "line":
                    tag = "warn" if "!" in val[:6] else ("err" if "Error" in val or "Traceback" in val else None)
                    self.log.insert("end", val, tag)
                    self.log.see("end")
                else:
                    self._finished(val)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _finished(self, rc):
        self.proc = None
        for b in (self.prev_btn, self.gen_btn):
            b.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        text = self.log.get("1.0", "end")
        if rc != 0:
            if self.pending_preview:
                self.pending_preview = False
                self.status.set("Settings changed - updating the preview...")
                self.run(final=False)
                return
            last = [l for l in text.strip().splitlines() if l.strip()][-1:] or ["unknown error"]
            self.status.set("Stopped." if rc in (-9, 1) and "Stopped" in text else
                            f"Something went wrong: {last[0][:140]}")
            return
        info = {}
        for l in text.splitlines():
            l = l.strip()
            for key in ("layers:", "filament:", "time:", "waves:"):
                if l.startswith(key):
                    info[key[:-1]] = l[len(key):].strip()
        bits = [f"{k}: {v}" for k, v in info.items()]
        if "! Steep" in text:
            bits.append("WARNING: steep sections may sag (see the log)")
        self.summary.set("   |   ".join(bits))
        self.last_preview = self.preview_png
        self.last_gcode = self.out
        self.refresh_view(keep_view=True)
        if self.final:
            self.last_output = self.out
            self.status.set(f"Saved: {self.out.name}")
            self.open_btn.configure(state="normal")
            self.folder_btn.configure(state="normal")
        else:
            part = " (quick preview - bottom part only)" if self.quick.get() else ""
            self.status.set(f"Preview ready{part}. Adjust, or click Generate file.")
        if self.pending_preview:
            self.pending_preview = False
            self.run(final=False)

    def stop(self, quiet=False):
        if self.proc:
            if not quiet:
                self.lines.put(("line", "\nStopped by you.\n"))
            self.proc.kill()

    def show_image(self, path: Path):
        if self.fig is None or not path.exists():
            return
        self.last_preview = path
        img = mpimg.imread(str(path))
        if self.dark.get():
            img = darken(img)
        self.fig.clear()
        self.ax3d = None
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.placeholder = None
        self.ax.imshow(img)
        self.ax.axis("off")
        self.canvas.draw_idle()

    # ----------------------------------------------------------------- 3D view
    def refresh_view(self, keep_view=False):
        three = self.view.get() == "3d"
        state = "normal" if three else "disabled"
        for frame in self.view_btns:
            for w in frame.winfo_children():
                try:
                    w.configure(state=state)
                except tk.TclError:
                    pass
        if three and self.last_gcode is not None:
            self.draw_3d(keep_view=keep_view)
        elif not three and self.last_preview is not None:
            self.show_image(self.last_preview)

    def draw_3d(self, keep_view=False):
        if self.fig is None or self.last_gcode is None or not self.last_gcode.exists():
            return
        key = (str(self.last_gcode), self.last_gcode.stat().st_mtime)
        if key != self.paths_key:
            try:
                self.paths = read_toolpath(self.last_gcode)
            except Exception as e:
                self.status.set(f"Couldn't read the toolpath for the 3D view: {e}")
                return
            self.paths_key = key
        wall, base = self.paths
        if len(wall) == 0:
            return
        elev, azim = 25, -60
        if keep_view and self.ax3d is not None:
            elev, azim = self.ax3d.elev, self.ax3d.azim
        c = THEME
        self.fig.clear()
        self.placeholder = None
        ax = self.fig.add_subplot(111, projection="3d")
        self.ax3d = ax
        self.fig.subplots_adjust(0, 0, 1, 1)
        w = thin(wall, 60000)
        ax.plot(w[:, 0], w[:, 1], w[:, 2], lw=0.45, color="#e8743b" if self.dark.get() else "#d9622b")
        if self.show_base.get() and len(base):
            b = thin(base, 20000)
            ax.plot(b[:, 0], b[:, 1], b[:, 2], lw=0.3, color="#888888")
        pts = wall[~np.isnan(wall[:, 0])]
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        span = np.maximum(hi - lo, 1e-3)
        ax.set_xlim(lo[0], hi[0])
        ax.set_ylim(lo[1], hi[1])
        ax.set_zlim(0, hi[2])
        aspect = (span[0], span[1], hi[2])
        try:
            ax.set_box_aspect(aspect, zoom=self.zoom)   # true proportions, like CAD
        except TypeError:
            ax.set_box_aspect(aspect)
        ax.set_facecolor(c["plot"])
        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.set_pane_color((1, 1, 1, 0.03) if self.dark.get() else (0.95, 0.95, 0.95, 1))
            axis.label.set_color(c["dim"])
            axis._axinfo["grid"]["color"] = c["border"]
        ax.tick_params(colors=c["dim"], labelsize=8)
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
        ax.view_init(elev=elev, azim=azim)
        self.canvas.draw_idle()

    def set_view(self, elev):
        if self.ax3d is not None:
            self.ax3d.view_init(elev=elev, azim=self.ax3d.azim)
            self.canvas.draw_idle()

    def nudge(self, azim=0.0, elev=0.0):
        if self.ax3d is not None:
            e = max(-90.0, min(90.0, self.ax3d.elev + elev))
            self.ax3d.view_init(elev=e, azim=self.ax3d.azim + azim)
            self.canvas.draw_idle()

    def reset_view(self):
        if self.ax3d is not None:
            self.zoom = 1.0
            self.zoom_by(1.0)
            self.ax3d.view_init(elev=25, azim=-60)
            self.canvas.draw_idle()

    def _view_key(self, event, fn):
        """Arrow / + / - keys move the 3D view - unless you're typing in a box or on a slider."""
        if self.view.get() != "3d" or self.ax3d is None:
            return
        cls = event.widget.winfo_class() if hasattr(event.widget, "winfo_class") else ""
        if cls in ("Entry", "TEntry", "Scale", "TCombobox", "Text", "Spinbox", "TSpinbox"):
            return
        fn()
        return "break"

    def zoom_by(self, f):
        self.zoom = min(6.0, max(0.4, self.zoom * f))
        if self.ax3d is not None:
            try:
                self.ax3d.set_box_aspect(self.ax3d.get_box_aspect(), zoom=self.zoom)
            except TypeError:
                pass
            self.canvas.draw_idle()

    def _scroll(self, event):
        if self.ax3d is not None:
            self.zoom_by(1.1 if event.button == "up" else 1 / 1.1)

    # ----------------------------------------------------------------- extras
    def show_command(self):
        if not self.model.get():
            messagebox.showinfo("Show command", "Choose a model first.")
            return
        out = Path(self.output.get() or "output.gcode")
        args = self.build_args(final=True, out=out)
        if args is None:
            return
        cmd = self.command_text(args)
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.log.insert("end", "\nThe same print from the command line (copied to the clipboard):\n")
        self.log.insert("end", cmd + "\n", "cmd")
        self.log.see("end")

    def open_output(self):
        if self.last_output and self.last_output.exists():
            open_path(self.last_output)

    def open_folder(self):
        if self.last_output:
            open_path(self.last_output.parent)

    # ----------------------------------------------------------------- presets
    def collect(self) -> dict:
        return {
            "version": __version__, "mode": self.mode.get(), "printer": self.printer.get(),
            "model": self.model.get(), "template": self.template.get(),
            "pattern": self.pattern.get(), "zones": self.zones_text.get(),
            "bottom_band": self.bottom_band.get(), "bottom_mm": self.bottom_mm.value,
            "top_band": self.top_band.get(), "top_mm": self.top_mm.value,
            "adaptive": self.adaptive.get(), "flip": self.flip.get(),
            "bed": [self.bed_x.get(), self.bed_y.get()],
            "quick": self.quick.get(), "quick_mm": self.quick_mm.value, "auto": self.auto.get(),
            "dark": self.dark.get(), "view": self.view.get(), "show_base": self.show_base.get(),
            "values": {k: s.value for k, s in self.sliders.items()},
            "enabled": {k: s.use.get() for k, s in self.sliders.items() if s.p.optional},
        }

    def apply(self, d: dict):
        for var, key in ((self.mode, "mode"), (self.printer, "printer"), (self.template, "template"),
                         (self.view, "view"),
                         (self.pattern, "pattern"), (self.zones_text, "zones")):
            if key in d:
                var.set(d[key])
        for var, key in ((self.bottom_band, "bottom_band"), (self.top_band, "top_band"),
                         (self.adaptive, "adaptive"), (self.flip, "flip"), (self.quick, "quick"),
                         (self.auto, "auto"), (self.dark, "dark"),
                         (self.show_base, "show_base")):
            if key in d:
                var.set(bool(d[key]))
        for sl, key in ((self.bottom_mm, "bottom_mm"), (self.top_mm, "top_mm"),
                        (self.quick_mm, "quick_mm")):
            if key in d:
                sl.set(d[key])
        if "bed" in d:
            self.bed_x.set(d["bed"][0])
            self.bed_y.set(d["bed"][1])
        for k, v in d.get("values", {}).items():
            if k in self.sliders:
                self.sliders[k].set(v)
        for k, v in d.get("enabled", {}).items():
            if k in self.sliders:
                self.sliders[k].use.set(bool(v))
                self.sliders[k]._toggled(notify=False)
        for s in (self.bottom_mm, self.top_mm, self.quick_mm):
            s._toggled(notify=False)
        self._printer_changed()
        self._mode_changed(notify=False)
        m = d.get("model")
        if m and Path(m).exists():
            self.set_model(m)
        self.apply_theme()

    def save_preset(self):
        p = filedialog.asksaveasfilename(title="Save preset", initialdir=str(HERE),
                                         defaultextension=".json",
                                         filetypes=[("Wave Studio preset", "*.json")])
        if p:
            d = self.collect()
            Path(p).write_text(json.dumps(d, indent=2), encoding="utf-8")
            self.status.set(f"Preset saved: {Path(p).name}")

    def load_preset(self):
        p = filedialog.askopenfilename(title="Load preset", initialdir=str(HERE),
                                       filetypes=[("Wave Studio preset", "*.json")])
        if p:
            self.load_settings(Path(p))

    def load_settings(self, path: Path, quiet=False):
        try:
            self.apply(json.loads(path.read_text(encoding="utf-8")))
            if not quiet:
                self.status.set(f"Preset loaded: {path.name}")
        except FileNotFoundError:
            if not quiet:
                messagebox.showerror("Load preset", f"Not found: {path}")
            elif IMPORT_ERROR is None:
                self._pattern_changed()      # first start: weave sliders at lattice values
        except Exception as e:
            if not quiet:
                messagebox.showerror("Load preset", f"Couldn't read the preset:\n{e}")

    def _close(self):
        self.stop(quiet=True)
        try:
            SETTINGS_FILE.write_text(json.dumps(self.collect(), indent=2), encoding="utf-8")
        except OSError:
            pass
        self.destroy()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def python_exe() -> str:
    """The Python that runs the scripts. pythonw.exe (no console) has no output, so use python.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def read_toolpath(path: Path):
    """Wall and base points from a file Wave Studio wrote (.gcode or Bambu .gcode.3mf).

    Only the scripts' own moves are read: from the toolpath marker until the end G-code, so
    the printer's purge line and the like don't show. Breaks (travel moves) become NaN rows.
    """
    import zipfile
    import numpy as np
    if path.name.lower().endswith(".3mf"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.startswith("Metadata/plate_") and n.endswith(".gcode"))
            text = z.read(name).decode("utf-8", errors="replace")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    i = text.find("toolpath ----")
    if i < 0:
        raise ValueError("no Wave Script toolpath in this file")
    nan = (float("nan"),) * 3
    wall, base = [], []
    x = y = z = 0.0
    feat = None
    for line in text[i:].splitlines()[1:]:
        if line.startswith(";"):
            if line.startswith("; FEATURE:"):
                f = line[10:].strip()
                if f not in ("Outer wall", "Bottom surface"):
                    break
                feat = f
            elif line.startswith("; ----") or "machine_end_gcode_start" in line.lower() \
                    or "filament end gcode" in line.lower():
                break
            continue
        if not line.startswith("G1"):
            continue
        nx, ny, nz, e = x, y, z, 0.0
        for tok in line.split(";")[0].split()[1:]:
            k, v = tok[0], tok[1:]
            try:
                if k == "X":
                    nx = float(v)
                elif k == "Y":
                    ny = float(v)
                elif k == "Z":
                    nz = float(v)
                elif k == "E":
                    e = float(v)
            except ValueError:
                pass
        moved = (nx, ny) != (x, y)
        target = base if feat == "Bottom surface" else wall
        if moved and e > 0:
            if not target or target[-1] is nan:
                target.append((x, y, z))
            target.append((nx, ny, nz))
        elif moved:
            for lst in (wall, base):
                if lst and lst[-1] is not nan:
                    lst.append(nan)
        x, y, z = nx, ny, nz
    return (np.array(wall, dtype=float).reshape(-1, 3), np.array(base, dtype=float).reshape(-1, 3))


def thin(a, limit: int):
    """Keep at most about `limit` points of a path (every n-th point, breaks kept)."""
    import numpy as np
    if len(a) <= limit:
        return a
    step = int(np.ceil(len(a) / limit))
    keep = np.zeros(len(a), dtype=bool)
    keep[::step] = True
    keep |= np.isnan(a[:, 0])
    return a[keep]


def darken(img):
    """Dark version of a preview picture: white paper and black text swap, colors stay."""
    import numpy as np
    rgb = img[..., :3].astype(float)
    if rgb.max() > 1.0:
        rgb /= 255.0
    grey = (rgb.max(axis=2) - rgb.min(axis=2)) < 0.06
    v = rgb.mean(axis=2)
    bg = np.array([0x1e, 0x1f, 0x22]) / 255
    fg = np.array([0xe6, 0xe6, 0xe6]) / 255
    out = rgb.copy()
    gap = np.abs(v - 0x22 / 255) < 0.025     # the close-up's dark see-through gaps: keep them
    flip = grey & ~gap                       # paper, grid, text and its soft edges: swap
    out[flip] = bg + (1 - v[flip])[:, None] * (fg - bg)
    return out


def dark_title_bar(win: tk.Tk, dark: bool):
    """Windows 10/11: make the title bar match. Does nothing elsewhere."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):                 # 20 = Windows 11 / new Win 10, 19 = older Win 10
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        w, h = win.winfo_width(), win.winfo_height()   # nudge so Windows repaints the bar
        win.geometry(f"{w + 1}x{h}")
        win.geometry(f"{w}x{h}")
    except Exception:
        pass


def open_path(p: Path):
    if sys.platform.startswith("win"):
        os.startfile(str(p))                       # .gcode.3mf opens in Bambu Studio
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def main():
    if IMPORT_ERROR is not None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Wave Studio - missing parts",
            f"A Python package is missing:\n\n{IMPORT_ERROR}\n\n"
            "Open a Command Prompt in the Wave Script folder and run:\n\n"
            "    pip install -r requirements.txt\n\nthen start Wave Studio again.")
        return
    app = WaveStudio()
    app.mainloop()


if __name__ == "__main__":
    main()
