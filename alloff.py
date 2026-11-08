#!/usr/bin/env python3
"""Put the hardware of one cycler into a defined safe state and exit:
  - all chargers OFF  (CP2112 GPIO -> /CE high; the CP2112 keeps this as long as it is USB-powered)
  - all loads OFF     (PCA9531 LED4..LED7 low), indicators LED0..LED3 off

Run it before connecting power supplies or cells.  Refuses to run while another script
(cycling.py, transport.py, ...) uses the same cycler (--force overrides).

Usage:
  ./alloff.py                  the only connected cycler
  ./alloff.py --cycler 9BFD    cycler with CP2112 serial ending ...9BFD
  ./alloff.py --all            every connected cycler that is not in use
"""
import argparse
import sys

import hwtest as hw
from cycling import CE_GPIO, CE_MASK, Chargers


REP_GPIO_GET = 0x03     # CP2112 "Get GPIO Values" -> [0x03, pin levels]


def switch_off(serial):
    rc = 0
    try:
        g = Chargers()                       # sets latch high, then outputs push-pull
        cfg = g.g.get_feature(hw.REP_GPIO_CFG, 5)
        lat = g.g.get_feature(REP_GPIO_GET, 2)[1]
        ok = (cfg[1] & CE_MASK) == CE_MASK and (lat & CE_MASK) == CE_MASK
        print(f"{serial}: chargers OFF: " + ", ".join(f"ch{ch} (GPIO{g_})" for ch, g_ in sorted(CE_GPIO.items()))
              + ("" if ok else f"  -- VERIFY FAILED (dir=0x{cfg[1]:02X} latch=0x{lat:02X})"))
        rc |= 0 if ok else 1
    except (OSError, RuntimeError) as e:
        print(f"{serial}: ERROR: chargers not switched off ({e})")
        rc |= 1

    try:
        bus = hw.Bus(hw.find_cp2112_bus())
        hw.pwm_leds(bus, hw.FETS_OFF)
        ls = bus.write_read(hw.PWM_ADDR, [hw.AI | hw.LS0], 2)
        ok = list(ls) == [0x00, 0x55]
        print(f"{serial}: loads OFF (LED4..LED7 low)"
              + ("" if ok else f"  -- VERIFY FAILED (LS0/LS1=0x{ls[0]:02X}/0x{ls[1]:02X})"))
        rc |= 0 if ok else 1
    except (OSError, TypeError) as e:
        print(f"{serial}: ERROR: loads not switched off ({e})")
        rc |= 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    hw.add_cycler_arg(ap)
    ap.add_argument("--all", action="store_true", help="all connected cyclers (skips those in use)")
    ap.add_argument("--force", action="store_true", help="switch off even if a script uses the cycler")
    args = ap.parse_args()

    if not args.all:
        hw.select_cycler(args.cycler)
        return switch_off(hw.lock_cycler(force=args.force))
    rc = 0
    for d in hw.list_cp2112():
        hw.select_cycler(d["serial"])
        hw._lock = None                      # one cycler at a time; the previous lock is released
        try:
            serial = hw.lock_cycler(force=args.force)
        except SystemExit as e:
            print(f"{d['serial']}: skipped - {e}")
            rc |= 2
            continue
        rc |= switch_off(serial)
    return rc


if __name__ == "__main__":
    sys.exit(main())
