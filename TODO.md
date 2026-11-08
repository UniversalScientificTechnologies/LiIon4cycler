# TODO

- [ ] Adjust the discharge currents.
- [ ] Set the charge current on all chargers (ch3 currently has a different charge current than the others).
- [ ] Calibrate the voltage and current measurement of the BQ34Z100 gauges against a precise meter.
- [ ] Choose the discharge end voltage (default 3500 mV under load).
- [ ] Decide whether to enable Impedance Track in the gauges.
- [ ] Verify the GPIO read-back (report `0x03`) in `alloff.py` on more than one converter.
