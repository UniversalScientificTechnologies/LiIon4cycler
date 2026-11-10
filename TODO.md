# TODO

- [ ] Adjust the discharge currents. Proposal: buy 10 Ω load resistors for all channels. With a 10 Ω load the discharge current at 3.5 V is 343 mA.
- [ ] Set the charge current to about 350 mA on all chargers (the same as the discharge current; ch3 currently has a different charge current than the others):
  - [ ] Replace R19 and R18 on every LION1CELL01B with 30 kΩ (charge ≈ 333 mA, pre-charge and termination ≈ 33 mA). Done experimentally on one charger so far; do the rest once it works.
  - [ ] Connect TTC (SV4 row 3) to GND on every board to disable the BQ24103 safety timer (≈ 4.3 h with C3 = 100 nF). The charge time limit is in `cycling.py`.
  - [ ] Check in the BQ24103 datasheet whether TTC on GND also disables the charge termination.
- [ ] Calibrate the voltage and current measurement of the BQ34Z100 gauges against a precise meter.
- [x] Choose the discharge end voltage: 3.5 V for the initial discharge, 2.8 V for the capacity discharge (under load).
- [ ] Replace the load switches with a 4× N-FET module with AC-coupled gate drive, as originally planned. No manufactured module was available, so two REL1SW01B modules switched statically are used instead. As a result a load stays on if the program crashes, and `alloff.py` must be run before inserting cells.
- [ ] Verify the GPIO read-back (report `0x03`) in `alloff.py` on more than one converter.
