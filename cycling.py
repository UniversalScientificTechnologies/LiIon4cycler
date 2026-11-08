#!/usr/bin/env python3
"""LiIon4cycler: capacity and DC internal resistance test of 4 cells.

Hardware (see hwtest.py):
  - BQ34Z100 gauges on I2CHUB02 channels 0..3 (voltage, current, temperature)
  - BQ24103 charger enable /CE driven by CP2112 GPIO (high = charger OFF)
  - discharge loads: static PCA9531 (I2CPWM01) outputs LED4..LED7 (L = load OFF,
    H = load ON, released open-drain pulled high), LED0..LED3 are indicators

Start: every gauge is identified and its data flash configuration checked (hwtest.configure_gauge,
  writes only differing values).  New cells are assumed: the values the gauge learned on the
  previous cell (Qmax, Ra tables, cycle count) are discarded; --same-cells keeps them.

Sequence (each cell independently, runs once):
  DISCHARGE1  load ON until U <= --empty (EMPTY_COUNT samples in a row) - defined empty start
  REST1       load OFF, wait --rest-empty minutes (default 10)
  CHARGE      charger ON; FULL when U >= FULL_MV and |I| <= FULL_MA for FULL_HOLD_S
  REST2       charger OFF, wait --rest minutes (default 30) for the voltage to settle (OCV full)
  DISCHARGE2  load ON until U <= --empty -> measured capacity (integrated current) and energy
  DONE        load OFF
  During both discharges the load is switched OFF for --rint-off seconds (default 3) every --rint
  minutes to measure the DC internal resistance R = (U_off - U_on) / (I_off - I_on): U_on/I_on read
  just before switching OFF, U_off/I_off read exactly --rint-off s later (the gauge updates once
  per second, so its values come from the last second before the reading).  Includes contacts,
  wiring and sense resistor; the result is the median of the DISCHARGE2 values.
  The script ends when all cells are DONE (chargers OFF, loads OFF).

Logs:
  cycling_SERIAL_YYYYmmdd_HHMMSS.csv  every sample of every cell (SERIAL = CP2112 serial = cycler)
  calibration.csv              voltage/current calibration per cycler (CP2112 serial) and channel;
                               without an entry the gauge values are used as they are.  Log and
                               display use calibrated values; log column cal = 1 if calibrated
  results.csv                  one line per cell and run (appended, never overwritten; shared by all
                               cyclers, column "cycler"): capacity, energy, DC resistance,
                               charged capacity, ... - also written for an aborted run (status column)

Usage:
  ./cycling.py                       new cells, log to cycling_YYYYmmdd_HHMMSS.csv
  ./cycling.py --cells A1,A2,A3,A4   cell labels for results.csv (ch0..ch3, empty = no cell id)
  ./cycling.py --same-cells          continue with the same cells (keep learned gauge values)
  ./cycling.py --skip-predischarge   start with CHARGE (no DISCHARGE1 / REST1)
  ./cycling.py --empty 3000          discharge end voltage under load [mV] (default 3500)
  ./cycling.py --rest 60             rest 60 min between charge and discharge
  ./cycling.py --rint 0              no internal resistance measurement (default every 5 min)
  ./cycling.py --rint-off 5          load OFF for 5 s per R measurement (default 3 s)
  ./cycling.py --status              read-only: print one readout of all cells and exit
  ./cycling.py --cycler 9BFD         several cyclers connected: use the one whose CP2112 serial
                                     ends with 9BFD (./hwtest.py --list shows them)
Afterwards ./transport.py brings the cells to the transport voltage.
"""
import argparse
import csv
import os
import statistics
import sys
import time
from datetime import datetime

import hwtest as hw

# ------------------------------------------------------------ configuration ---
CHANNELS = (0, 1, 2, 3)                   # mux channels with chargers
CE_GPIO = {3: 3, 2: 4, 1: 5, 0: 6}        # mux channel -> CP2112 GPIO driving /CE
IND_LED = {0: 0, 1: 1, 2: 2, 3: 3}        # mux channel -> PCA9531 indicator LED (static, low = ON)
FET_LED = {0: 4, 1: 5, 2: 6, 3: 7}        # mux channel -> PCA9531 output switching the load (H = ON)

FULL_MV = 4100          # charge finished: voltage at least ...
FULL_MA = 50            # ... and charger current dropped to (BQ24103 terminates ~130 mA)
FULL_HOLD_S = 30        # ... for this long
EMPTY_MV = 3500         # default discharge end (voltage under load)
EMPTY_COUNT = 3         # consecutive samples below the empty voltage
MAX_TEMP_C = 55.0       # any cell above -> abort (everything off)
CHARGE_TIMEOUT_S = 8 * 3600
DISCHARGE_TIMEOUT_S = 8 * 3600
REST_MIN = 30           # default rest between charge and discharge [min]
REST_EMPTY_MIN = 10     # default rest between the first discharge and charge [min]
POLL_S = 2.0            # readout + terminal output period
RINT_MIN = 5            # internal resistance measurement period during discharge [min]
RINT_OFF_S = 3.0        # default load OFF time before the "off" reading for R [s]
RINT_OFF_MIN_S = 1.5    # shorter: the 1 s gauge values would still include the load-ON period
RINT_MIN_DI_MA = 100    # current step needed for a valid measurement
MAX_I2C_ERRORS = 5      # consecutive failed readouts of one cell -> abort
RESULTS_FILE = "results.csv"

DISCHARGES = ("DISCHARGE1", "DISCHARGE2")
SHORT = {"DISCHARGE1": "DIS1", "REST1": "RST1", "CHARGE": "CHRG", "REST2": "RST2",
         "DISCHARGE2": "DIS2", "DONE": "DONE"}

# ------------------------------------------------------------- CP2112 GPIO ---
CE_MASK = sum(1 << g for g in CE_GPIO.values())


class Chargers:
    """BQ24103 /CE via CP2112 GPIO: GPIO high = charger disabled."""

    def __init__(self):
        path = hw.find_cp2112_hidraw()
        if path is None:
            raise RuntimeError("CP2112 hidraw device not found")
        self.g = hw.CP2112Gpio(path)
        self.latch = CE_MASK                      # start with all chargers OFF
        cfg = self.g.get_feature(hw.REP_GPIO_CFG, 5)
        self._write()                             # set latch before enabling outputs
        self.g.set_feature([hw.REP_GPIO_CFG, cfg[1] | CE_MASK, cfg[2] | CE_MASK, cfg[3], cfg[4]])
        self._write()

    def _write(self):
        self.g.set_feature([hw.REP_GPIO_SET, self.latch, CE_MASK])

    def set(self, ch, on):
        bit = 1 << CE_GPIO[ch]
        self.latch = (self.latch & ~bit) if on else (self.latch | bit)
        self._write()

    def all_off(self):
        self.latch = CE_MASK
        self._write()


# ---------------------------------------------------------- PCA9531 loads ---
class Loads:
    """Discharge loads on PCA9531 LED4..LED7, static control:
    L (output driven low) = load OFF, H (output released, pulled high) = load ON.
    LED0..LED3 are indicators, driven low (LED on) while the load is ON.
    Note: the PCA9531 keeps its last state if the program or the bus dies.
    """

    def __init__(self, bus):
        self.bus = bus
        self.active = set()
        self.all_off()

    def _mask(self):
        m = hw.FETS_OFF                         # all load outputs low
        for ch in self.active:
            m |= 1 << IND_LED[ch]               # indicator on
            m &= ~(1 << FET_LED[ch])            # release -> H -> load ON
        return m

    def set(self, ch, on):
        (self.active.add if on else self.active.discard)(ch)
        hw.pwm_leds(self.bus, self._mask())

    def all_off(self):
        self.active.clear()
        hw.pwm_leds(self.bus, hw.FETS_OFF)


# ------------------------------------------------------------------ gauges ---
def read_cell(bus, ch):
    """Gauge readout with the cycler calibration applied (d["cal"] = calibration used)."""
    hw.mux_select(bus, ch)
    return hw.apply_calibration(hw.read_gauge_retry(bus), getattr(bus, "cal", {}).get(ch))


def fmt_cell(ch, d, state):
    return (f"ch{ch} {state:<10} {d['U_mV']:>5} mV {d['I_mA']:>6} mA "
            f"{d['T_C']:>5.1f} C SoC {d['SoC_%']:>3}%")


def status(bus):
    for ch in CHANNELS:
        try:
            print(fmt_cell(ch, read_cell(bus, ch), "-"))
        except OSError as e:
            print(f"ch{ch} ERROR {e}")
    hw.mux_select(bus, None)


def setup_gauges(bus, new_cell):
    """Identify all gauges, check their data flash configuration and (new cells) discard the
    learned values.  Returns {ch: firmware}."""
    fws = {}
    for ch in CHANNELS:
        try:
            hw.mux_select(bus, ch)
            g = hw.identify_gauge(bus)
            fws[ch] = g["fw"]
            tag = f"ch{ch}: gauge FW 0x{g['fw']:04X}"
            if g["fw"] not in hw.GAUGE_CONFIGS:
                print(f"{tag}  WARNING: unknown firmware (known: {hw.KNOWN_FW}) - not configured, "
                      f"learned values not reset, current = AverageCurrent() only")
                continue
            changes = hw.configure_gauge(bus, new_cell=new_cell)
            what = "config + learned values reset (new cell)" if new_cell else "config"
            print(f"{tag}  {what} " + ("OK" if not changes else f"UPDATED ({len(changes)} values)"))
            for c in changes:
                print(f"         {c}")
        except (OSError, RuntimeError) as e:
            print(f"ch{ch}: WARNING: gauge configuration not checked ({e})")
    hw.mux_select(bus, None)
    return fws


# ----------------------------------------------------------------- results ---
RESULT_COLUMNS = ["start", "end", "log", "cycler", "ch", "cell_id", "status",
                  "capacity_mAh", "energy_Wh", "R_dc_mOhm", "R_min_mOhm", "R_max_mOhm", "R_n", "R_pulse_s",
                  "I_load_mA", "t_discharge_h", "U_ocv_full_mV", "charge_mAh", "t_charge_h",
                  "predischarge_mAh", "gauge_used_mAh", "T_max_C", "empty_mV", "gauge_fw", "calibrated"]


def write_results(res, path=RESULTS_FILE):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    cols = RESULT_COLUMNS
    if not new:                                 # keep the columns of an existing (older) file
        with open(path, newline="") as f:
            cols = next(csv.reader(f), RESULT_COLUMNS)
        missing = [c for c in RESULT_COLUMNS if c not in cols]
        if missing:
            print(f"WARNING: {path} has an older header, not written: {', '.join(missing)}")
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for ch in CHANNELS:
            w.writerow(res[ch])


# -------------------------------------------------------------------- main ---
def run(bus, args):
    rest_s, rest_empty_s, rint_s, empty_mv = args.rest * 60, args.rest_empty * 60, args.rint * 60, args.empty
    rint_off_s = args.rint_off
    ids = (args.cells.split(",") + [""] * 4)[:4] if args.cells else [""] * 4
    chargers = Chargers()
    loads = Loads(bus)
    fws = setup_gauges(bus, new_cell=not args.same_cells)

    state = {ch: "DISCHARGE1" for ch in CHANNELS}
    since = {}                                  # start time of the current state
    full_since = {ch: None for ch in CHANNELS}
    low_count = {ch: 0 for ch in CHANNELS}
    errors = {ch: 0 for ch in CHANNELS}
    last = {}
    t_prev = {}                                 # time of the previous sample (integration)
    rint_next = {}                              # time of next R measurement
    pulse = {}                                  # ch -> (t_off, U_on, I_on, pos) while load is OFF for R;
                                                # pos = t_off - start of its poll (channel position)
    acc = {ch: {"mAh": 0.0, "mWh": 0.0, "R": [], "rem_start": None} for ch in CHANNELS}

    stamp = datetime.now()
    logname = stamp.strftime(f"cycling_{args.serial}_%Y%m%d_%H%M%S.csv")
    res = {ch: {"start": stamp.isoformat(timespec="seconds"), "log": logname, "cycler": args.serial, "ch": ch,
                "cell_id": ids[ch], "status": "ABORTED in DISCHARGE1", "empty_mV": empty_mv,
                "R_pulse_s": rint_off_s if rint_s else "",
                "calibrated": "yes" if ch in getattr(bus, "cal", {}) else "no",
                "gauge_fw": f"0x{fws[ch]:04X}" if ch in fws else "", "T_max_C": None}
           for ch in CHANNELS}
    logf = open(logname, "w", newline="")
    log = csv.writer(logf)
    log.writerow(["time", "elapsed_s", "ch", "state", "load", "U_mV", "I_mA", "T_C", "SoC_%",
                  "RemCap_mAh", "R_mOhm", "cal"])
    print(f"Cycler {args.serial}: logging to {logname}, results to {RESULTS_FILE}; discharge to {empty_mv} mV, "
          f"rest {rest_empty_s / 60:.0f} min (empty) / {rest_s / 60:.0f} min (full), "
          + (f"R every {rint_s / 60:.0f} min (load OFF {rint_off_s:g} s)" if rint_s else "no R measurement"))

    def enter(ch, st, now, msg):
        state[ch] = st
        since[ch] = now
        t_prev[ch] = now
        res[ch]["status"] = f"ABORTED in {st}"
        acc[ch].update(mAh=0.0, mWh=0.0, R=[], rem_start=None)
        print(f"  ch{ch}: {msg}")

    def start_discharge(ch, now, st, msg):
        loads.set(ch, True)
        rint_next[ch] = now + rint_s
        low_count[ch] = 0
        enter(ch, st, now, msg)

    def finish_discharge(ch, now, d):
        a, in_state = acc[ch], now - since[ch]
        loads.set(ch, False)
        if state[ch] == "DISCHARGE1":
            res[ch]["predischarge_mAh"] = round(a["mAh"])
            enter(ch, "REST1", now, f"EMPTY at {d['U_mV']} mV after {in_state / 3600:.2f} h "
                  f"({a['mAh']:.0f} mAh), load OFF, resting")
        else:
            r = a["R"]
            res[ch].update(capacity_mAh=round(a["mAh"]), energy_Wh=round(a["mWh"] / 1000, 2),
                           t_discharge_h=round(in_state / 3600, 2),
                           I_load_mA=round(a["mAh"] / (in_state / 3600)) if in_state else "",
                           gauge_used_mAh=a["rem_start"] - d["RemCap_mAh"],
                           R_dc_mOhm=round(statistics.median(r), 1) if r else "",
                           R_min_mOhm=min(r) if r else "", R_max_mOhm=max(r) if r else "", R_n=len(r))
            enter(ch, "DONE", now, f"EMPTY at {d['U_mV']} mV after {in_state / 3600:.2f} h, load OFF: "
                  f"capacity {a['mAh']:.0f} mAh, {a['mWh'] / 1000:.2f} Wh"
                  + (f", R {res[ch]['R_dc_mOhm']} mOhm" if r else ""))
            res[ch]["status"] = "OK"

    time.sleep(1.0)
    t0 = time.time()
    for ch in CHANNELS:
        if args.skip_predischarge:              # CE high->low also resets BQ24103 timers/faults
            chargers.set(ch, True)
            enter(ch, "CHARGE", t0, "initial discharge skipped, charger ON")
        else:
            start_discharge(ch, t0, "DISCHARGE1", "load ON (initial discharge)")

    try:
        while True:
            now = time.time()
            for ch in CHANNELS:
                if state[ch] == "DONE":
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
                a = acc[ch]
                t_s = time.time()
                load_on = st in DISCHARGES and ch not in pulse

                # charge / discharge integration (rectangle, sample current x time since last sample)
                dt_h = (t_s - t_prev.get(ch, t_s)) / 3600
                t_prev[ch] = t_s
                sign = -1 if st in DISCHARGES else 1 if st == "CHARGE" else 0
                if sign:
                    a["mAh"] += max(0, sign * d["I_mA"]) * dt_h
                    a["mWh"] += max(0, sign * d["I_mA"]) * d["U_mV"] / 1000 * dt_h
                    if a["rem_start"] is None:
                        a["rem_start"] = d["RemCap_mAh"]
                tmax = res[ch]["T_max_C"]
                res[ch]["T_max_C"] = d["T_C"] if tmax is None else max(tmax, d["T_C"])

                r_mohm = ""
                if ch in pulse and t_s - pulse[ch][0] >= rint_off_s - 0.1:
                    t_off, u_on, i_on, _ = pulse.pop(ch)
                    loads.set(ch, True)                     # load back ON
                    # unknown FW: only filtered AverageCurrent (tau ~15 s) - the 2 s "off" value is
                    # meaningless, but with charger and load OFF the current is exactly 0
                    i_off = d["I_mA"] if d["I_inst"] else 0
                    di = i_off - i_on
                    if abs(di) >= RINT_MIN_DI_MA:
                        r_mohm = round((d["U_mV"] - u_on) / di * 1000, 1)
                        a["R"].append(r_mohm)
                        print(f"  ch{ch}: R = {r_mohm} mOhm  (U {u_on} -> {d['U_mV']} mV, "
                              f"I {i_on} -> {i_off} mA{'' if d['I_inst'] else ' assumed'}, "
                              f"SoC {d['SoC_%']}%)")
                    else:
                        print(f"  ch{ch}: R not measured, current step only {di} mA")
                log.writerow([datetime.now().isoformat(timespec="seconds"), round(now - t0, 1), ch,
                              st, int(load_on), d["U_mV"], d["I_mA"], d["T_C"], d["SoC_%"],
                              d["RemCap_mAh"], r_mohm, int(d["cal"])])

                if d["T_C"] > MAX_TEMP_C:
                    raise RuntimeError(f"ch{ch}: temperature {d['T_C']} C > {MAX_TEMP_C} C")
                in_state = now - since[ch]

                if st in DISCHARGES:
                    if ch in pulse or not load_on:
                        continue                            # load OFF for R: voltage not under load
                    low_count[ch] = low_count[ch] + 1 if d["U_mV"] <= empty_mv else 0
                    if low_count[ch] >= EMPTY_COUNT:
                        finish_discharge(ch, now, d)
                    elif in_state > DISCHARGE_TIMEOUT_S:
                        raise RuntimeError(f"ch{ch}: {st} timeout")
                    elif rint_s and now >= rint_next[ch]:
                        t_off = time.time()                 # U_on/I_on = values under load
                        pulse[ch] = (t_off, d["U_mV"], d["I_mA"], t_off - now)
                        loads.set(ch, False)
                        rint_next[ch] = now + rint_s

                elif st == "REST1":
                    if in_state >= rest_empty_s:
                        # CE high->low transition also resets BQ24103 timers/faults
                        chargers.set(ch, True)
                        full_since[ch] = None
                        enter(ch, "CHARGE", now, f"rest over at {d['U_mV']} mV, charger ON")

                elif st == "CHARGE":
                    if d["U_mV"] >= FULL_MV and abs(d["I_mA"]) <= FULL_MA:
                        full_since[ch] = full_since[ch] or now
                        if now - full_since[ch] >= FULL_HOLD_S:
                            chargers.set(ch, False)
                            res[ch].update(charge_mAh=round(a["mAh"]), t_charge_h=round(in_state / 3600, 2))
                            enter(ch, "REST2", now, f"FULL at {d['U_mV']} mV after {in_state / 3600:.2f} h "
                                  f"({a['mAh']:.0f} mAh charged), charger OFF, resting")
                    else:
                        full_since[ch] = None
                    if state[ch] == "CHARGE" and in_state > CHARGE_TIMEOUT_S:
                        raise RuntimeError(f"ch{ch}: charge timeout")

                elif st == "REST2":
                    if in_state >= rest_s:
                        res[ch]["U_ocv_full_mV"] = d["U_mV"]
                        start_discharge(ch, now, "DISCHARGE2", f"rest over at {d['U_mV']} mV, load ON "
                                        f"(capacity discharge)")
            hw.mux_select(bus, None)
            logf.flush()

            print(f"[{(now - t0) / 60:7.1f} min] " + " | ".join(
                f"ch{ch} {SHORT[state[ch]]} {last[ch]['U_mV']:>4}mV {last[ch]['I_mA']:>5}mA "
                f"{last[ch]['T_C']:>4.1f}C" if ch in last else f"ch{ch} ----"
                for ch in CHANNELS))

            if all(state[ch] == "DONE" for ch in CHANNELS):
                print(f"\nAll cells done, total {(now - t0) / 3600:.2f} h")
                return 0

            # next poll after POLL_S, or earlier so that a pulsed channel is read exactly
            # rint_off_s after its load was switched OFF
            t_next = min([now + POLL_S] + [t + rint_off_s - pos for t, _, _, pos in pulse.values()])
            time.sleep(max(0.0, t_next - time.time()))
    finally:
        # safe state: loads OFF first, then chargers OFF
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
        print("Loads OFF, chargers OFF")
        end = datetime.now().isoformat(timespec="seconds")
        for ch in CHANNELS:
            res[ch]["end"] = end
        write_results(res)
        print(f"\nResults (appended to {RESULTS_FILE}):")
        for ch in CHANNELS:
            r = res[ch]
            print(f"  ch{ch} {r['cell_id'] or '-':<8} {r['status']:<20} "
                  f"capacity {r.get('capacity_mAh', '-')} mAh  {r.get('energy_Wh', '-')} Wh  "
                  f"R_dc {r.get('R_dc_mOhm') or '-'} mOhm  (charged {r.get('charge_mAh', '-')} mAh, "
                  f"OCV full {r.get('U_ocv_full_mV', '-')} mV, load {r.get('I_load_mA', '-')} mA)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="read-only readout of all cells")
    ap.add_argument("--same-cells", action="store_true",
                    help="continue with the same cells: keep the values learned by the gauges")
    ap.add_argument("--cells", metavar="ID0,ID1,ID2,ID3", help="cell labels for results.csv")
    ap.add_argument("--skip-predischarge", action="store_true",
                    help="skip the initial discharge (and its rest), start with charging")
    ap.add_argument("--empty", type=int, default=EMPTY_MV, metavar="MV",
                    help=f"discharge end voltage under load (default {EMPTY_MV})")
    ap.add_argument("--rest", type=float, default=REST_MIN, metavar="MIN",
                    help=f"rest between charge and the capacity discharge in minutes (default {REST_MIN})")
    ap.add_argument("--rest-empty", type=float, default=REST_EMPTY_MIN, metavar="MIN",
                    help=f"rest between the initial discharge and charge in minutes (default {REST_EMPTY_MIN})")
    ap.add_argument("--rint", type=float, default=RINT_MIN, metavar="MIN",
                    help=f"internal resistance measurement period in minutes, 0 = off (default {RINT_MIN})")
    ap.add_argument("--rint-off", type=float, default=RINT_OFF_S, metavar="S",
                    help=f"load OFF time per internal resistance measurement in seconds (default {RINT_OFF_S:g}, "
                         f"min {RINT_OFF_MIN_S:g})")
    hw.add_cycler_arg(ap)
    args = ap.parse_args()

    if args.rint_off < RINT_OFF_MIN_S:
        ap.error(f"--rint-off must be at least {RINT_OFF_MIN_S:g} s")
    bus, args.serial = hw.open_cycler(args)
    if args.status:
        status(bus)
        return 0
    try:
        return run(bus, args)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except RuntimeError as e:
        print(f"ABORT: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
