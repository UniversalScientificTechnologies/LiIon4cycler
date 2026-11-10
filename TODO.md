# TODO

- [ ] Adjust the discharge currents. Proposal: buy 10 Ω load resistors for all channels. With a 10 Ω load the discharge current at 3.5 V is 343 mA.
- [ ] Set the charge current on all chargers (ch3 currently has a different charge current than the others). Recommended value: about 350 mA, the same as the discharge current.
  - Before lowering the current, extend or disable the BQ24103 safety timer. It is set by C3 (100 nF) on the TTC pin of LION1CELL01B: 2.6 min/nF ≈ 4.3 h. At 350 mA the charger would stop after about 1500 mAh. Either use a bigger C3 (for example 330 nF ≈ 14 h), or connect TTC (SV4 row 3) to GND to disable the timer; check that option in the BQ24103 datasheet first.
- [ ] Calibrate the voltage and current measurement of the BQ34Z100 gauges against a precise meter.
- [x] Choose the discharge end voltage: 3.5 V for the initial discharge, 2.8 V for the capacity discharge (under load).
- [ ] Replace the load switches with a 4× N-FET module with AC-coupled gate drive, as originally planned. No manufactured module was available, so two REL1SW01B modules switched statically are used instead. As a result a load stays on if the program crashes, and `alloff.py` must be run before inserting cells.
- [ ] Verify the GPIO read-back (report `0x03`) in `alloff.py` on more than one converter.
