"""Regenerate docs/images/weave_patterns.png (one close-up per Weave Script pattern).

    python docs/make_weave_images.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402

import weave_script as wv  # noqa: E402

NAMES = ["lattice", "diamond", "chainmail", "basket", "twill", "scales", "brick", "ribs"]
stl = HERE.parent / "examples" / "test_cylinder.stl"
tiles = []
with tempfile.TemporaryDirectory() as tmp:
    for name in NAMES:
        out = Path(tmp) / f"{name}.gcode"
        wv.main([str(stl), "--pattern", name, "--max-height", "20", "-o", str(out)])
        im = Image.open(Path(tmp) / f"{name}_preview.png")
        w, h = im.size
        tiles.append(im.crop((w // 2, int(h * 0.3), w, int(h * 0.72))))
fig, axes = plt.subplots(4, 2, figsize=(12, 13))
for ax, im, name in zip(axes.flat, tiles, NAMES):
    ax.imshow(im)
    ax.axis("off")
fig.suptitle("Weave Script patterns (close-ups, bright = outer strands)")
fig.tight_layout()
fig.savefig(HERE / "images" / "weave_patterns.png", dpi=110)
print("wrote", HERE / "images" / "weave_patterns.png")
