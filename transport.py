#!/usr/bin/env python3
"""Bring all 4 cells to the transport (storage) voltage, default 3.5 V open-circuit.

Each cell independently, repeated steps until the rested voltage (OCV) is within --tol:
  REST       charger and load OFF for --rest minutes, then OCV = cell voltage
  CHARGE     OCV below target: charger ON until the voltage under charge reaches target + offset
  DISCHARGE  OCV above target: load ON until the voltage under load drops to target - offset
  DONE       |OCV - target| <= tol  (or FAIL after MAX_STEPS steps / timeout)
The offset (voltage difference between "under current" and the rested voltage, I*R plus
polarisation) starts at OFFSET0_MV and is re-estimated after every step, so the cells
converge in 2..3 steps.

At the end chargers and loads are OFF.  NOTE: the chargers stay OFF only while the CP2112 is
USB-powered (the /CE pull-down enables them otherwise) - disconnect the cells or the charger
supply before unplugging USB.

Log: transport_SERIAL_YYYYmmdd_HHMMSS.csv (same columns as cycling_*.csv, works with plotlog.py).

Usage:
  ./transport.py                 target 3500 mV, tolerance 20 mV, rest 5 min
  ./transport.py --target 3600 --tol 10 --rest 10
  ./transport.py --cycler 9BFD   several cyclers connected: select by CP2112 serial (end)
"""
import argparse
import csv
import sys
import time
from datetime import datetime

import hwtest as hw
from cycling import CHANNELS, MAX_I2C_ERRORS, MAX_TEMP_C, POLL_S, SHORT, Chargers, Loads, read_cell

TARGET_MV = 3500        # transport open-circuit voltage
TOL_MV = 20             # accepted |OCV - target|
REST_MIN = 5            # rest before each OCV reading [min]
OFFSET0_MV = 100        # initial guess of |U under current - OCV|
OFFSET_MAX_MV = 400
MAX_STEPS = 8           # charge/discharge steps per cell
STEP_TIMEOUT_S = 4 * 3600


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=int, default=TARGET_MV, metavar="MV",
                    help=f"transport open-circuit voltage (default {TARGET_MV})")
    ap.add_argument("--tol", type=int, default=TOL_MV, metavar="MV", help=f"tolerance (default {TOL_MV})")
    ap.add_argument("--rest", type=float, default=REST_MIN, metavar="MIN",
                    help=f"rest before each OCV reading in minutes (default {REST_MIN})")
    hw.add_cycler_arg(ap)
    args = ap.parse_args()
    target, tol, rest_s = args.target, args.tol, args.rest * 60

    bus, serial = hw.open_cycler(args)
    chargers = Chargers()                       # all OFF
    loads = Loads(bus)                          # all OFF

    state = {ch: "REST" for ch in CHANNELS}
    since = {}
    stop_mv = {}                                # voltage at which the running step ends
    u_stop = {}                                 # voltage under current at the end of the last step
    last_step = {}                              # "CHARGE" / "DISCHARGE" of the last step
    offset = {ch: {"CHARGE": OFFSET0_MV, "DISCHARGE": OFFSET0_MV} for ch in CHANNELS}
    steps = {ch: 0 for ch in CHANNELS}
    ocv = {}
    errors = {ch: 0 for ch in CHANNELS}
    last = {}

    logname = datetime.now().strftime(f"transport_{serial}_%Y%m%d_%H%M%S.csv")
    logf = open(logname, "w", newline="")
    log = csv.writer(logf)
    log.writerow(["time", "elapsed_s", "ch", "state", "load", "U_mV", "I_mA", "T_C", "SoC_%",
                  "RemCap_mAh", "R_mOhm"])
    print(f"Cycler {serial}: target OCV {target} +- {tol} mV, rest {rest_s / 60:.0f} min, logging to {logname}")

    def enter(ch, st, now, msg):
        state[ch] = st
        since[ch] = now
        print(f"  ch{ch}: {msg}")

    def stop_step(ch, now, d, why=""):
        st = state[ch]
        chargers.set(ch, False) if st == "CHARGE" else loads.set(ch, False)
        u_stop[ch], last_step[ch] = d["U_mV"], st
        enter(ch, "REST", now, f"{st} step {steps[ch]} stopped at {d['U_mV']} mV{why}, resting")

    t0 = time.time()
    for ch in CHANNELS:
        since[ch] = t0
    try:
        while True:
            now = time.time()
            for ch in CHANNELS:
                if state[ch] in ("DONE", "FAIL"):
                    continue
                try:
                    d = read_cell(bus, ch)
                    errors[ch] = 0
                except OSError as e:
                    errors[ch] += 1
                    print(f"  ch{ch}: read error {e} ({errors[ch]}/{MAX_I2C_ERRORS})")
                    if errors[ch] >= MAX_I2C_ERRORS:
                        raise RuntimeError(f"ch{ch}: gauge not responding")
                    continue
                last[ch] = d
                st = state[ch]
                log.writerow([datetime.now().isoformat(timespec="seconds"), round(now - t0, 1), ch,
                              st, int(st == "DISCHARGE"), d["U_mV"], d["I_mA"], d["T_C"], d["SoC_%"],
                              d["RemCap_mAh"], ""])
                if d["T_C"] > MAX_TEMP_C:
                    raise RuntimeError(f"ch{ch}: temperature {d['T_C']} C > {MAX_TEMP_C} C")
                in_state = now - since[ch]

                if st == "REST":
                    if in_state < rest_s:
                        continue
                    u = ocv[ch] = d["U_mV"]
                    if ch in last_step:                 # learn the offset from the last step
                        k = last_step[ch]
                        offset[ch][k] = min(OFFSET_MAX_MV, max(0, abs(u_stop[ch] - u)))
                    if abs(u - target) <= tol:
                        enter(ch, "DONE", now, f"OCV {u} mV - DONE")
                    elif steps[ch] >= MAX_STEPS:
                        enter(ch, "FAIL", now, f"OCV {u} mV, not within {tol} mV after {MAX_STEPS} steps")
                    elif u < target:
                        steps[ch] += 1
                        stop_mv[ch] = target + offset[ch]["CHARGE"]
                        chargers.set(ch, True)
                        enter(ch, "CHARGE", now, f"OCV {u} mV -> charge until {stop_mv[ch]} mV")
                    else:
                        steps[ch] += 1
                        stop_mv[ch] = target - offset[ch]["DISCHARGE"]
                        loads.set(ch, True)
                        enter(ch, "DISCHARGE", now, f"OCV {u} mV -> discharge until {stop_mv[ch]} mV")

                elif st == "CHARGE":
                    if d["U_mV"] >= stop_mv[ch]:
                        stop_step(ch, now, d)
                    elif in_state > STEP_TIMEOUT_S:
                        stop_step(ch, now, d, " (timeout - charger not charging?)")
                        state[ch] = "FAIL"

                elif st == "DISCHARGE":
                    if d["U_mV"] <= stop_mv[ch]:
                        stop_step(ch, now, d)
                    elif in_state > STEP_TIMEOUT_S:
                        stop_step(ch, now, d, " (timeout)")
                        state[ch] = "FAIL"
            hw.mux_select(bus, None)
            logf.flush()

            print(f"[{(now - t0) / 60:7.1f} min] " + " | ".join(
                f"ch{ch} {SHORT.get(state[ch], state[ch][:4]):<4} {last[ch]['U_mV']:>4}mV "
                f"{last[ch]['I_mA']:>5}mA" if ch in last else f"ch{ch} ----" for ch in CHANNELS))

            if all(state[ch] in ("DONE", "FAIL") for ch in CHANNELS):
                print(f"\nFinished after {(now - t0) / 60:.0f} min:")
                for ch in CHANNELS:
                    print(f"  ch{ch}: {state[ch]:<4}  OCV {ocv.get(ch, '-')} mV  ({steps[ch]} steps)")
                return 0 if all(state[ch] == "DONE" for ch in CHANNELS) else 1

            time.sleep(max(0.0, POLL_S - (time.time() - now)))
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except RuntimeError as e:
        print(f"ABORT: {e}")
        return 1
    finally:
        try:
            loads.all_off()
        except OSError as e:
            print(f"WARNING: could not switch loads OFF ({e}) - LOADS MAY STILL BE ON, power-cycle I2CPWM01!")
        chargers.all_off()
        try:
            hw.mux_select(bus, None)
        except OSError:
            pass
        logf.close()
        print("Loads OFF, chargers OFF (chargers stay OFF only while the CP2112 is USB-powered)")


if __name__ == "__main__":
    sys.exit(main())
