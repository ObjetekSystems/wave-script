# Making a Bambu template

Wave Script and Weave Script write every nozzle move themselves, but a Bambu Lab printer also
needs its own **start sequence**: heating, bed leveling, nozzle wipe, purge line and AMS
loading. Instead of guessing it, both scripts borrow it from a file that **Bambu Studio sliced
for your printer**. That file is the *template*.

You make it once per model (and again if you change material). The same template works for
`wave_script.py` and `weave_script.py`.

> The repository doesn't include a template. It holds your printer, plate and filament
> settings, so everyone makes their own.

## What the template provides

| From the template | From the script |
|---|---|
| Start and end sequence (homing, bed leveling, purge, AMS) | Every wall move: the wave, weave and rim |
| Nozzle and bed temperatures | Speeds, flow and fan changes |
| Filament type and color shown in Bambu Studio | Layer heights and the solid base |
| Printer and plate type | |

The template's own walls, infill and slicer settings are thrown away, so it doesn't matter
which layer height or infill you slice it with.

## Step by step

1. **Open Bambu Studio** and start a new project.
2. **Pick the printer** you'll print on (P2S, P1S, X1, A1...), the **nozzle** (0.4 mm) and the
   **plate type** in the Prepare tab.
3. **Pick the filament you'll print with** and put that spool in AMS slot 1 (or the external
   spool). The template's temperatures come from this filament profile.
4. **Import the same STL you'll run through the script.** Drag it onto the plate, or use
   **File > Import**.
5. **Don't move, rotate or scale it.** Leave it where Bambu Studio puts it, at the center of the
   plate. The scripts also put the model at the plate center, so the bed leveling covers the
   right area.
6. Click **Slice plate**.
7. Choose **File > Export > Export plate sliced file** and save it in the Wave Script
   `examples` folder, for example as `template.gcode.3mf`. Give each model its own name, such
   as `template_bulb.gcode.3mf`.

Use **Export plate sliced file**, not "Export G-code" and not "Save project". Only the plate
sliced file (`.gcode.3mf`) has what the scripts need.

## Using it

Add `--bambu-template` and the path to the template. It works the same in both scripts:

```
python wave_script.py examples/test_cylinder.stl --bambu-template examples/template.gcode.3mf
python weave_script.py examples/test_cylinder.stl --pattern lattice --bambu-template examples/template.gcode.3mf
```

The output is a `.gcode.3mf` file. Open it in Bambu Studio; it goes straight to the Preview tab
with nothing to re-slice. Click **Print plate**, and in the send window map **Filament 1** to the
AMS slot or external spool you want.

Don't change anything in the Prepare tab. Bambu Studio would try to re-slice, can't re-slice
custom G-code, and the model disappears. If that happens, just reopen the file.

## When to make a new template

| You change... | New template? |
|---|---|
| The model (a different STL) | **Yes.** Bed leveling covers the model's footprint. |
| The material (PLA to PETG, for example) | **Yes.** Temperatures come from the template. |
| The printer, nozzle or plate type | **Yes.** |
| Only the color, same material | No. Pick the spool in the send window. |
| Pattern or settings in the script | No. |

## Special cases

- **Printing upside down (`--flip`):** flip the model in Bambu Studio the same way before
  slicing the template, so bed leveling covers the right area.
- **Model not at the plate center:** if you move the model in Bambu Studio, pass the same
  position to the script with `--center X Y` (in mm).
- **Rotating or scaling in Bambu Studio doesn't carry over.** The scripts read the STL file
  directly. Rotate or scale the model in your CAD app and export a new STL.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Template not found` | The path must include the folder, for example `examples/template.gcode.3mf`. |
| `Could not find Bambu's start/end G-code markers` | Re-export with **File > Export > Export plate sliced file**. |
| `The template has no sliced G-code inside` | You saved a project 3MF. Slice the plate, then use **Export plate sliced file**. |
| Loading the output changes the color in slot 1 | Normal: the color comes from the template. Map the spool in the send window. |
| Wrong temperatures | Re-export the template with the right filament selected. |

## Other printers

Non-Bambu printers (Prusa, Creality, Elegoo, Klipper...) don't use a template. Leave off
`--bambu-template` to get a plain `.gcode` file, and check `profiles/generic_start.gcode` and
`profiles/generic_end.gcode` for your printer. See the README, "Other printers".
