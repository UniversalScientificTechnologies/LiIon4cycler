#!/usr/bin/env python3
"""Test of a single BQ34Z100 gauge (LION1CELL01B) connected directly to USBI2C01A, without a cell.

For checking a board before it goes into the cycler, or a board suspected to be faulty.

Wiring (no I2CHUB02 in between):
  USBI2C01A SV2 row 1 GND  -> LION1CELL01B SV3 row 4 GND
  USBI2C01A SV2 row 2 SDA  -> LION1CELL01B SV3 row 1 SDA
  USBI2C01A SV2 row 4 SCL  -> LION1CELL01B SV3 row 2 SCL
  pull-ups: USBI2C01A has none and LION1CELL01B has none -> 4.7-10 kOhm from SDA and SCL to
            USBI2C01A SV2 row 3 (VDD, jumper SV3 at 3.3 V), or connect I2CPWM01A to the same bus
            (it has 10 kOhm pull-ups)
  power:    the gauge runs from the cell voltage (+BAT).  Without a cell connect a lab supply
            3.0-4.2 V, current limit ~100 mA, to the WAGO terminals: + to SV7 (+BAT), - to SV8 (GND).
            The supply current does not flow through the 10 mOhm shunt, so the gauge reads ~0 mA.
            Do not connect a supply and a cell at the same time.

Steps:
  1. identify the gauge @ 0x55: device type, firmware, hardware, chemistry, sealed
  2. read voltage, current, temperature, SoC, capacities (raw values, no calibration)
  3. read back the data flash configuration for its firmware and compare with GAUGE_CONFIGS
  4. with --configure: write the configuration (only differing values) and read it back again
Exit code is the number of errors (0 = all OK).

Usage:
  ./gaugetest.py                  read-only test
  ./gaugetest.py --configure      also write the cycler configuration into the gauge
  ./gaugetest.py --configure --new-cell   ... and reset the learned values (Qmax, Ra, cycles)
  ./gaugetest.py --watch 2        afterwards print the readings every 2 s (Ctrl+C ends)
  ./gaugetest.py --cycler 9BFD    several USBI2C01A connected: choose by CP2112 serial
"""
import argparse
import sys
import time

import hwtest as hw

U_MIN_MV, U_MAX_MV = 2500, 4500     # plausible gauge supply voltage
I_MAX_MA = 20                       # without a cell nothing should flow through the shunt
T_MIN_C, T_MAX_C = 0.0, 60.0        # board thermistor, room temperature expected


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configure", action="store_true",
                    help="write the cycler data flash configuration (only differing values)")
    ap.add_argument("--new-cell", action="store_true",
                    help="with --configure: also reset the learned values (Qmax, Ra tables, cycle count)")
    ap.add_argument("--watch", type=float, metavar="S", help="then print the readings every S seconds")
    hw.add_cycler_arg(ap)
    args = ap.parse_args()
    if args.new_cell and not args.configure:
        ap.error("--new-cell needs --configure")

    errors, warnings = [], []

    def fail(msg):
        errors.append(msg)
        print(f"  ERROR: {msg}")

    def warn(msg):
        warnings.append(msg)
        print(f"  WARNING: {msg}")

    bus, serial = hw.open_cycler(args, calibrated=False)
    print(f"USBI2C01A {serial}: gauge test, direct connection, RAW values")
    for a in hw.scan_mux(bus):
        warn(f"I2CHUB02 (0x{a:02X}) found on the bus - this test talks to the gauge directly; "
             f"for gauges behind the hub use ./hwtest.py")
        bus.write(a, [0])                 # all hub channels off
    bus.mux_ch = None

    print(f"[1] gauge @ 0x{hw.GAUGE_ADDR:02X}: identification")
    try:
        g = hw.identify_gauge(bus)
    except OSError as e:
        fail(f"gauge not responding ({e})")
        print("  check: supply 3.0-4.2 V on the WAGO terminals (or a cell), SDA/SCL not swapped "
              "(SV3 row 1 = SDA, row 2 = SCL), common GND, pull-ups on SDA/SCL")
        return finish(errors, warnings)
    fw = g["fw"]
    print(f"  DEVICE_TYPE 0x{g['dev']:04X}  FW 0x{fw:04X}  HW 0x{g['hw']:04X}  CHEM 0x{g['chem']:04X}  "
          f"{'SEALED' if g['sealed'] else 'unsealed'}  - {hw.GAUGE_FW_INFO.get(fw, 'UNKNOWN firmware')}")
    if g["dev"] != 0x0100:
        fail(f"unexpected device type 0x{g['dev']:04X} (BQ34Z100 = 0x0100)")
    if fw not in hw.GAUGE_CONFIGS:
        warn(f"unknown firmware 0x{fw:04X} (known: {hw.KNOWN_FW}) - data flash layout unknown, "
             f"configuration not checked; current = AverageCurrent() only")

    print("[2] readings")
    try:
        d = hw.read_gauge_retry(bus)
    except OSError as e:
        fail(f"readout failed ({e})")
        return finish(errors, warnings)
    print_reading(d)
    if not U_MIN_MV <= d["U_mV"] <= U_MAX_MV:
        fail(f"voltage {d['U_mV']} mV outside {U_MIN_MV}-{U_MAX_MV} mV (supply connected? voltage divider?)")
    if abs(d["I_mA"]) > I_MAX_MA:
        warn(f"current {d['I_mA']} mA, expected ~0 mA without a cell (shunt / SRP-SRN filter?)")
    if not T_MIN_C <= d["T_C"] <= T_MAX_C:
        fail(f"temperature {d['T_C']} C outside {T_MIN_C:g}-{T_MAX_C:g} C (thermistor?)")

    if fw in hw.GAUGE_CONFIGS:
        if args.configure:
            print("[3] configuration: writing (only differing values)")
            if d["U_mV"] < hw.FLASH_MIN_MV:
                fail(f"voltage {d['U_mV']} mV < {hw.FLASH_MIN_MV} mV - data flash cannot be written")
            else:
                try:
                    changes = hw.configure_gauge(bus, new_cell=args.new_cell)
                    print("  already configured" if not changes else f"  updated ({len(changes)} values)")
                    for c in changes:
                        print(f"         {c}")
                except (OSError, RuntimeError) as e:
                    fail(f"configuration failed ({e})")
        print(f"[{4 if args.configure else 3}] data flash read-back (FW 0x{fw:04X} layout)")
        try:
            diff = 0
            for name, val, exp, ok in hw.read_gauge_config(bus, fw):
                print(f"         {name:<58} {val:>6}" + ("" if ok else f"   <-- expected {exp}"))
                diff += not ok
            if diff:
                msg = f"{diff} data flash value(s) differ from the cycler configuration"
                if args.configure:
                    fail(msg)
                else:
                    warn(msg + " - run with --configure to write it")
        except OSError as e:
            fail(f"data flash read-back failed ({e})")

    if args.watch:
        print(f"\nreadings every {args.watch:g} s (Ctrl+C ends)")
        try:
            while True:
                try:
                    print_reading(hw.read_gauge_retry(bus))
                except OSError as e:
                    print(f"  read error {e}")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print()
    return finish(errors, warnings)


def print_reading(d):
    print(f"  U {d['U_mV']} mV  I {d['I_mA']} mA{'' if d['I_inst'] else ' (average)'}  T {d['T_C']} C  "
          f"SoC {d['SoC_%']}%  MaxErr {d['MaxErr_%']}%  Rem {d['RemCap_mAh']} mAh  "
          f"Full {d['FullCap_mAh']} mAh  Design {d['DesCap_mAh']} mAh")


def finish(errors, warnings):
    print("\nRESULT: " + ("all OK" if not errors else f"{len(errors)} error(s)")
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    for e in errors:
        print(f"  - ERROR: {e}")
    for w in warnings:
        print(f"  - WARNING: {w}")
    return len(errors)


if __name__ == "__main__":
    sys.exit(main())
