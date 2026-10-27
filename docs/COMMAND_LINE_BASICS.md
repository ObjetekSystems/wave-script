# Command line basics (for first-timers)

Wave Script and Weave Script don't have buttons or menus. You run them by typing a
**command** into a black **Command Prompt** window. If you've never done that, this page is
for you. It takes about five minutes, and after that you can mostly copy and paste the
example commands from the guides.

## 1. Open a Command Prompt in the right folder

1. Open the **Wave Script** folder in File Explorer (the folder with `wave_script.py` in it).
2. Click once in the **address bar** at the top, where the folder path is shown.
3. Type `cmd` and press **Enter**.

A black window opens, and the line before the blinking cursor ends with the folder name, for
example:

```
C:\Users\you\Documents\Wave Script>
```

That means you're in the right place. Every command in the guides is typed (or pasted) here,
then you press **Enter** to run it.

> **Pasting:** right-click inside the window, or press **Ctrl+V**.

## 2. What a command is made of

Here's a typical command, split into its parts:

```
python weave_script.py examples/test_cylinder.stl --pattern lattice --max-height 15
```

| Part | What it means |
|---|---|
| `python` | Start Python. |
| `weave_script.py` | Which script to run (`wave_script.py` or `weave_script.py`). |
| `examples/test_cylinder.stl` | Your model file. |
| `--pattern lattice` | A **setting**: its name starts with two dashes, then a space, then its value. |
| `--max-height 15` | Another setting: only print the first 15 mm. |

Things to know:

- **Spaces separate the parts.** Put exactly one space between each part.
- **The order of the settings doesn't matter.** `--max-height 15 --pattern lattice` works
  just as well.
- **Leave a setting out and it uses its default.** You only type the ones you want to change.
- **Some settings are on/off switches** with no value after them, for example `--flip`,
  `--adaptive` and `--no-preview`. Typing the switch turns it on.
- **Two dashes, no space inside the name:** `--max-height`, not `- -max-height` or
  `--max height`.

## 3. Pointing at your own model

The examples use the included test files in the `examples` folder. To use your own STL:

**Easiest way: drag and drop.** Type the start of the command, including a space at the end:

```
python weave_script.py 
```

Then drag your STL file from File Explorer into the black window. Windows types the full path
for you. Then add any settings and press **Enter**.

**Or copy the STL into the Wave Script folder** and just type its name:

```
python weave_script.py my_vase.stl --pattern basket
```

**File names with spaces need quotes** around the whole path:

```
python weave_script.py "C:\Users\you\Desktop\My Vase.stl" --pattern basket
```

(Drag and drop adds the quotes for you.)

## 4. Bambu printers: add the template

Bambu printers need the `.gcode.3mf` file format, which the scripts make only when you give
them a **template**: a file you slice once in Bambu Studio from the same STL (the main
README explains how). Add it with `--bambu-template`:

```
python weave_script.py my_vase.stl --pattern lattice --bambu-template my_vase_template.gcode.3mf
```

Without `--bambu-template`, you get a plain `.gcode` file for other printers (Prusa, Creality,
Klipper and so on). Bambu Studio won't print that one.

## 5. Running it again with changes

You'll often run the same command several times with small changes. You don't have to retype
it:

- Press the **Up arrow** key to bring back the last command.
- Use the **Left/Right arrows** to move to the part you want to change, edit it, and press
  **Enter**.
- Keep a text file (Notepad) of commands you like, and paste them back in later.

## 6. Reading the result

When it finishes, the script prints a short summary like this:

```
  zones:
      Z 0.0-top: lattice (19 waves/lap, depth 2.5 mm, open, 0.45 mm gaps)
  layers:   41  (height 15.0 mm)
  time:     ~22 min (moves only, no heat-up)
  wrote:    examples/test_cylinder_weave.gcode.3mf
  preview:  examples/test_cylinder_weave_preview.png
```

- **wrote:** the file to open in Bambu Studio (or send to your printer).
- **preview:** a picture of the toolpath. Open it and check the pattern before printing.
- Lines starting with **`!`** are warnings worth reading, for example steep sections where
  strands may sag.

The new files are saved **next to your STL**, with `_wave` or `_weave` added to the name.

## 7. Seeing every setting

```
python weave_script.py --help
python weave_script.py --list
python wave_script.py --help
```

`--help` lists every setting with its default. `--list` (Weave Script only) shows every
pattern.

## 8. Common problems

| Message | What to do |
|---|---|
| `'python' is not recognized...` | Python isn't installed, or "Add python.exe to PATH" wasn't ticked during setup. Reinstall Python and tick that box. Or try `py` instead of `python`. |
| `No module named 'trimesh'` (or numpy, shapely...) | The libraries aren't installed yet. Run `python -m pip install -r requirements.txt` once. |
| `can't open file '...weave_script.py'` | The window isn't in the Wave Script folder. Close it and repeat step 1. |
| `File not found: ...stl` | Check the spelling of the file name, or use drag and drop (step 3). |
| `error: unrecognized arguments: ...` | A setting name is misspelled, or a space is missing or extra. Compare with the example, character by character. |
| `error: argument --pattern: invalid choice` | That pattern name doesn't exist. Run `python weave_script.py --list`. |
| `Template not found` | Check the template file's name and folder, or drag it into the window after `--bambu-template `. |
