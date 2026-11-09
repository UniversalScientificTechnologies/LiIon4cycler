# TODO

- [ ] Adjust the discharge currents. Proposal: buy 10 Ω load resistors for all channels. With a 10 Ω load the discharge current at 3.5 V is 343 mA.
- [ ] Set the charge current on all chargers (ch3 currently has a different charge current than the others). Recommended value: about 350 mA, the same as the discharge current.
- [ ] Calibrate the voltage and current measurement of the BQ34Z100 gauges against a precise meter.
- [ ] Choose the discharge end voltage (default 3500 mV under load).
- [ ] Decide whether to enable Impedance Track in the gauges.
- [ ] Verify the GPIO read-back (report `0x03`) in `alloff.py` on more than one converter.
