#!/usr/bin/env python3
"""Monitor only: all chargers OFF, all loads OFF, print every gauge every POLL_S seconds.

Shows and logs the RAW gauge values (calibration.csv is NOT applied) - use it to compare the
gauges with a reference meter when calibrating.

Optional CSV log (--log). Ctrl+C ends; chargers stay OFF (use --restore to switch them
back ON = hardware default on exit).
"""
import argparse
import csv
import sys
import time
from datetime import datetime

import hwtest as hw
from cycling import CHANNELS, POLL_S, Chargers


def read_raw(bus, ch):
    hw.mux_select(bus, ch)
    return hw.read_gauge_retry(bus)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interval", type=float, default=POLL_S, help=f"period [s] (default {POLL_S})")
    ap.add_argument("--log", action="store_true", help="write monitor_YYYYmmdd_HHMMSS.csv")
    ap.add_argument("--restore", action="store_true", help="switch chargers back ON on exit")
    hw.add_cycler_arg(ap)
    args = ap.parse_args()

    bus, serial = hw.open_cycler(args, calibrated=False)
    chargers = Chargers()                    # all chargers OFF (/CE high)
    hw.pwm_leds(bus, hw.FETS_OFF)            # LED4..7 low -> loads OFF, indicators LED0..3 OFF
    print(f"Cycler {serial}: chargers OFF, loads OFF, RAW gauge values (no calibration)  (Ctrl+C to quit)")

    log = writer = None
    if args.log:
        name = datetime.now().strftime(f"monitor_{serial}_%Y%m%d_%H%M%S.csv")
        log = open(name, "w", newline="")
        writer = csv.writer(log)
        writer.writerow(["time", "ch", "U_mV", "I_mA", "T_C", "SoC_%", "RemCap_mAh", "FullCap_mAh", "cal"])
        print(f"logging to {name}")

    hdr = " | ".join(f"ch{ch}:    U mV    I mA     T C  SoC" for ch in CHANNELS)
    t0 = time.time()
    n = 0
    try:
        while True:
            if n % 20 == 0:
                print(f"{'time':>8} | {hdr}")
            n += 1
            now = datetime.now()
            cols = []
            for ch in CHANNELS:
                try:
                    d = read_raw(bus, ch)
                    cols.append(f"     {d['U_mV']:>7} {d['I_mA']:>7} {d['T_C']:>7.1f} {d['SoC_%']:>3}%")
                    if writer:
                        writer.writerow([now.isoformat(timespec="seconds"), ch, d["U_mV"], d["I_mA"],
                                         d["T_C"], d["SoC_%"], d["RemCap_mAh"], d["FullCap_mAh"],
                                         0])
                except OSError as e:
                    cols.append(f"     {'ERROR ' + str(e.errno):>27}")
            hw.mux_select(bus, None)
            if log:
                log.flush()
            print(f"{now:%H:%M:%S} | " + " | ".join(cols), flush=True)
            t0 += args.interval
            time.sleep(max(0.0, t0 - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()
        if args.restore:
            for ch in CHANNELS:
                chargers.set(ch, True)
            print("\nchargers ON (default)")
        else:
            print("\nchargers left OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
