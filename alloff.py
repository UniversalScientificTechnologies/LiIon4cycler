#!/usr/bin/env python3
"""Put the hardware into a defined safe state and exit:
  - all chargers OFF  (CP2112 GPIO -> /CE high; the CP2112 keeps this as long as it is USB-powered)
  - all loads OFF     (PCA9531 LED4..LED7 low), indicators LED0..LED3 off

Run it before connecting power supplies or cells.  Refuses to run while cycling.py / chargertest.py is running (--force overrides).
"""
import glob
import os
import sys

import hwtest as hw
from cycling import CE_GPIO, CE_MASK, Chargers


REP_GPIO_GET = 0x03     # CP2112 "Get GPIO Values" -> [0x03, pin levels]


def cycling_running():
    """PIDs of other running cycling.py / chargertest.py processes."""
    pids = []
    for f in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            cmd = open(f, "rb").read().split(b"\0")
        except OSError:
            continue
        if any(os.path.basename(a) in (b"cycling.py", b"chargertest.py") for a in cmd[1:3]):
            pids.append(f.split("/")[2])
    return pids


def main():
    pids = cycling_running()
    if pids and "--force" not in sys.argv:
        print(f"cycling.py/chargertest.py is running (PID {', '.join(pids)}) - stop it first, "
              f"or use --force to switch everything off anyway")
        return 2
    rc = 0
    try:
        g = Chargers()                       # sets latch high, then outputs push-pull
        cfg = g.g.get_feature(hw.REP_GPIO_CFG, 5)
        lat = g.g.get_feature(REP_GPIO_GET, 2)[1]
        ok = (cfg[1] & CE_MASK) == CE_MASK and (lat & CE_MASK) == CE_MASK
        print("chargers OFF: " + ", ".join(f"ch{ch} (GPIO{g_})" for ch, g_ in sorted(CE_GPIO.items()))
              + ("" if ok else f"  -- VERIFY FAILED (dir=0x{cfg[1]:02X} latch=0x{lat:02X})"))
        rc |= 0 if ok else 1
    except (OSError, RuntimeError) as e:
        print(f"ERROR: chargers not switched off ({e})")
        rc |= 1

    try:
        bus = hw.Bus(hw.find_cp2112_bus())
        hw.pwm_leds(bus, hw.FETS_OFF)
        ls = bus.write_read(hw.PWM_ADDR, [hw.AI | hw.LS0], 2)
        ok = list(ls) == [0x00, 0x55]
        print("loads OFF (LED4..LED7 low)" + ("" if ok else f"  -- VERIFY FAILED (LS0/LS1=0x{ls[0]:02X}/0x{ls[1]:02X})"))
        rc |= 0 if ok else 1
    except (OSError, TypeError) as e:
        print(f"ERROR: loads not switched off ({e})")
        rc |= 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
