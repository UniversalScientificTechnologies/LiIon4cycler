#!/usr/bin/env python3
"""Monitor only: all chargers OFF, all loads OFF, print every gauge every POLL_S seconds.

Optional CSV log (--log). Ctrl+C ends; chargers stay OFF (use --restore to switch them
back ON = hardware default on exit).
"""
import argparse
import csv
import sys
import time
from datetime import datetime

import hwtest as hw
from cycling import CHANNELS, POLL_S, Chargers, read_cell


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interval", type=float, default=POLL_S, help=f"period [s] (default {POLL_S})")
    ap.add_argument("--log", action="store_true", help="write monitor_YYYYmmdd_HHMMSS.csv")
    ap.add_argument("--restore", action="store_true", help="switch chargers back ON on exit")
    args = ap.parse_args()

    bus = hw.Bus(hw.find_cp2112_bus())
    chargers = Chargers()                    # all chargers OFF (/CE high)
    hw.pwm_leds(bus, hw.FETS_OFF)            # LED4..7 low -> loads OFF, indicators LED0..3 OFF
    print("chargers OFF, loads OFF  (Ctrl+C to quit)")

    log = writer = None
    if args.log:
        name = datetime.now().strftime("monitor_%Y%m%d_%H%M%S.csv")
        log = open(name, "w", newline="")
        writer = csv.writer(log)
        writer.writerow(["time", "ch", "U_mV", "I_mA", "T_C", "SoC_%", "RemCap_mAh", "FullCap_mAh"])
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
                    d = read_cell(bus, ch)
                    cols.append(f"     {d['U_mV']:>7} {d['I_mA']:>7} {d['T_C']:>7.1f} {d['SoC_%']:>3}%")
                    if writer:
                        writer.writerow([now.isoformat(timespec="seconds"), ch, d["U_mV"], d["I_mA"],
                                         d["T_C"], d["SoC_%"], d["RemCap_mAh"], d["FullCap_mAh"]])
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
