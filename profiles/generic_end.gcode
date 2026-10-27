; ---- Generic end G-code ----
M104 S0                   ; nozzle heater off
M140 S0                   ; bed heater off
M107                      ; fan off
G91                       ; relative positioning
G1 Z10 F1200              ; lift
G90
G1 X0 Y200 F6000          ; present the print
M84                       ; motors off
; ---- end of end G-code ----
