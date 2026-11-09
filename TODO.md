# TODO

- [ ] Adjust the discharge currents. Proposal: buy 10 Ω load resistors for all channels. With a 10 Ω load the discharge current at 3.5 V is 343 mA.
- [ ] Set the charge current on all chargers (ch3 currently has a different charge current than the others). Recommended value: about 350 mA, the same as the discharge current.
- [ ] Calibrate the voltage and current measurement of the BQ34Z100 gauges against a precise meter.
- [x] Choose the discharge end voltage: 3.5 V for the initial discharge, 2.8 V for the capacity discharge (under load).
- [ ] Replace the load switches with a 4× N-FET module with AC-coupled gate drive, as originally planned. No manufactured module was available, so two REL1SW01B modules switched statically are used instead. As a result a load stays on if the program crashes, and `alloff.py` must be run before inserting cells.
- [ ] Verify the GPIO read-back (report `0x03`) in `alloff.py` on more than one converter.
