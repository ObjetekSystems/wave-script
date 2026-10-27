; ---- Generic start G-code (Marlin / Klipper style) ----
; Edit this file to match your printer. {nozzle_temp} and {bed_temp}
; are filled in by Wave Script from --nozzle-temp / --bed-temp.
M140 S{bed_temp}          ; start heating bed
M104 S{nozzle_temp}       ; start heating nozzle
G28                       ; home all axes
M190 S{bed_temp}          ; wait for bed
M109 S{nozzle_temp}       ; wait for nozzle
G90                       ; absolute positioning
M83                       ; relative extrusion
G92 E0
; purge line along the front-left edge
G1 Z0.3 F1200
G1 X5 Y20 F6000
G1 X5 Y120 E15 F1200
G1 X5.5 Y120 F1200
G1 X5.5 Y20 E15 F1200
G1 Z2 F1200
; ---- end of start G-code ----
