#!/usr/bin/env python3
"""LiIon4cycler: charge all 4 cells, then discharge them down to 3.5 V.

Hardware (see hwtest.py):
  - BQ34Z100 gauges on I2CHUB02 channels 0..3 (voltage, current, temperature)
  - BQ24103 charger enable /CE driven by CP2112 GPIO (high = charger OFF)
  - discharge loads: static PCA9531 (I2CPWM01) outputs LED4..LED7 (L = load OFF,
    H = load ON, released open-drain pulled high), LED0..LED3 are indicators

Start: every gauge is identified and its data flash configuration checked (hwtest.configure_gauge,
  writes only differing values - normally nothing; the data flash is non-volatile).

Sequence (each cell independently, runs once):
  CHARGE     charger ON; FULL when U >= FULL_MV and |I| <= FULL_MA for FULL_HOLD_S
  REST       charger OFF, wait --rest minutes (default 30) for the voltage to settle
  DISCHARGE  load ON until U <= EMPTY_MV (EMPTY_COUNT samples in a row);
             every RINT_PERIOD_S the load is switched OFF for one poll (~2 s) to measure
             the internal resistance R = (U_off - U_on) / (I_off - I_on)  (1-2 s DCIR,
             includes contacts/wiring/sense resistor, the gauge measures on the PCB;
             gauge FW without any instantaneous current: settled AverageCurrent, I_off = 0)
  DONE       load OFF
  The script ends when all cells are DONE (chargers OFF, loads OFF).

Usage:
  ./cycling.py            run the cycle, log to cycling_YYYYmmdd_HHMMSS.csv
  ./cycling.py --rest 60  rest 60 min between charge and discharge
  ./cycling.py --rint 0   no internal resistance measurement (default every 5 min)
  ./cycling.py --status   read-only: print one readout of all cells and exit
"""
import argparse
import csv
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
EMPTY_MV = 3500         # discharge finished (voltage under load)
EMPTY_COUNT = 3         # consecutive samples below EMPTY_MV
MAX_TEMP_C = 55.0       # any cell above -> abort (everything off)
CHARGE_TIMEOUT_S = 8 * 3600
DISCHARGE_TIMEOUT_S = 8 * 3600
REST_MIN = 30           # default rest between charge and discharge [min]
POLL_S = 2.0            # readout + terminal output period
RINT_MIN = 5            # internal resistance measurement period during discharge [min]
RINT_OFF_S = 1.5        # load OFF at least this long before the "off" reading (gauge updates 1/s)
RINT_MIN_DI_MA = 100    # current step needed for a valid measurement
MAX_I2C_ERRORS = 5      # consecutive failed readouts of one cell -> abort

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
    hw.mux_select(bus, ch)
    return hw.read_gauge_retry(bus)


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


# -------------------------------------------------------------------- main ---
def setup_gauges(bus):
    """Identify all gauges and make sure their data flash configuration is right."""
    for ch in CHANNELS:
        try:
            hw.mux_select(bus, ch)
            g = hw.identify_gauge(bus)
            tag = f"ch{ch}: gauge FW 0x{g['fw']:04X}"
            if g["fw"] not in hw.GAUGE_CONFIGS:
                print(f"{tag}  WARNING: unknown firmware (known: {hw.KNOWN_FW}) - not configured, "
                      f"current = AverageCurrent() only")
                continue
            changes = hw.configure_gauge(bus)
            print(f"{tag}  config " + ("OK" if not changes else "UPDATED: " + "; ".join(changes)))
        except (OSError, RuntimeError) as e:
            print(f"ch{ch}: WARNING: gauge configuration not checked ({e})")
    hw.mux_select(bus, None)


def run(bus, rest_s, rint_s):
    chargers = Chargers()
    loads = Loads(bus)
    setup_gauges(bus)
    state = {ch: "CHARGE" for ch in CHANNELS}   # CHARGE -> REST -> DISCHARGE -> DONE
    since = {}                                  # start time of the current state
    full_since = {ch: None for ch in CHANNELS}
    low_count = {ch: 0 for ch in CHANNELS}
    errors = {ch: 0 for ch in CHANNELS}
    result = {}
    last = {}
    rint_next = {}                              # time of next R measurement
    pulse = {}                                  # ch -> (t_off, U_on, I_on) while load is OFF for R

    logname = datetime.now().strftime("cycling_%Y%m%d_%H%M%S.csv")
    logf = open(logname, "w", newline="")
    log = csv.writer(logf)
    log.writerow(["time", "elapsed_s", "ch", "state", "load", "U_mV", "I_mA", "T_C", "SoC_%",
                  "RemCap_mAh", "R_mOhm"])
    print(f"Logging to {logname}, rest {rest_s / 60:.0f} min, discharge to {EMPTY_MV} mV, "
          + (f"R every {rint_s / 60:.0f} min" if rint_s else "no R measurement"))

    def enter(ch, st, now, msg):
        state[ch] = st
        since[ch] = now
        print(f"  ch{ch}: {msg}")

    # start charging; CE high->low transition also resets BQ24103 timers/faults
    time.sleep(1.0)
    t0 = time.time()
    for ch in CHANNELS:
        chargers.set(ch, True)
        since[ch] = t0
    print("All chargers ON")

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
                load_on = state[ch] == "DISCHARGE" and ch not in pulse
                r_mohm = ""
                if ch in pulse and time.time() - pulse[ch][0] >= RINT_OFF_S:
                    _, u_on, i_on = pulse.pop(ch)
                    loads.set(ch, True)                     # load back ON
                    # unknown FW: only filtered AverageCurrent (tau ~15 s) - the 2 s "off" value is
                    # meaningless, but with charger and load OFF the current is exactly 0;
                    # i_on is settled (load ON for RINT_MIN)
                    i_off = d["I_mA"] if d["I_inst"] else 0
                    di = i_off - i_on
                    if abs(di) >= RINT_MIN_DI_MA:
                        r_mohm = round((d["U_mV"] - u_on) / di * 1000, 1)
                        print(f"  ch{ch}: R = {r_mohm} mOhm  (U {u_on} -> {d['U_mV']} mV, "
                              f"I {i_on} -> {i_off} mA{'' if d['I_inst'] else ' assumed'}, "
                              f"SoC {d['SoC_%']}%)")
                    else:
                        print(f"  ch{ch}: R not measured, current step only {di} mA")
                log.writerow([datetime.now().isoformat(timespec="seconds"), round(now - t0, 1), ch,
                              state[ch], int(load_on), d["U_mV"], d["I_mA"], d["T_C"], d["SoC_%"],
                              d["RemCap_mAh"], r_mohm])

                if d["T_C"] > MAX_TEMP_C:
                    raise RuntimeError(f"ch{ch}: temperature {d['T_C']} C > {MAX_TEMP_C} C")
                in_state = now - since[ch]

                if state[ch] == "CHARGE":
                    if d["U_mV"] >= FULL_MV and abs(d["I_mA"]) <= FULL_MA:
                        full_since[ch] = full_since[ch] or now
                        if now - full_since[ch] >= FULL_HOLD_S:
                            chargers.set(ch, False)
                            enter(ch, "REST", now, f"FULL at {d['U_mV']} mV after "
                                  f"{in_state / 3600:.2f} h, charger OFF, resting")
                    else:
                        full_since[ch] = None
                    if state[ch] == "CHARGE" and in_state > CHARGE_TIMEOUT_S:
                        raise RuntimeError(f"ch{ch}: charge timeout")

                elif state[ch] == "REST":
                    if in_state >= rest_s:
                        loads.set(ch, True)
                        rint_next[ch] = now + rint_s
                        result[ch] = {"rest_end_mV": d["U_mV"], "rem_start": d["RemCap_mAh"], "R": []}
                        enter(ch, "DISCHARGE", now, f"rest over at {d['U_mV']} mV, load ON")

                elif state[ch] == "DISCHARGE":
                    if r_mohm != "":
                        result[ch]["R"].append(r_mohm)
                    if ch in pulse or not load_on:
                        continue                            # load OFF for R: voltage not under load
                    low_count[ch] = low_count[ch] + 1 if d["U_mV"] <= EMPTY_MV else 0
                    if low_count[ch] >= EMPTY_COUNT:
                        loads.set(ch, False)
                        result[ch].update(t_h=in_state / 3600, rem_end=d["RemCap_mAh"])
                        enter(ch, "DONE", now, f"EMPTY at {d['U_mV']} mV after "
                              f"{in_state / 3600:.2f} h, load OFF")
                    elif in_state > DISCHARGE_TIMEOUT_S:
                        raise RuntimeError(f"ch{ch}: discharge timeout")
                    elif rint_s and now >= rint_next[ch]:
                        pulse[ch] = (time.time(), d["U_mV"], d["I_mA"])   # values under load
                        loads.set(ch, False)
                        rint_next[ch] = now + rint_s
            hw.mux_select(bus, None)
            logf.flush()

            print(f"[{(now - t0) / 60:7.1f} min] " + " | ".join(
                f"ch{ch} {state[ch][:4]:<4} {last[ch]['U_mV']:>4}mV {last[ch]['I_mA']:>5}mA "
                f"{last[ch]['T_C']:>4.1f}C" if ch in last else f"ch{ch} ----"
                for ch in CHANNELS))

            if all(state[ch] == "DONE" for ch in CHANNELS):
                print(f"\nAll cells discharged to {EMPTY_MV} mV, total {(now - t0) / 3600:.2f} h")
                for ch in CHANNELS:
                    r = result[ch]
                    print(f"  ch{ch}: discharge {r['t_h']:.2f} h, gauge RemCap "
                          f"{r['rem_start']} -> {r['rem_end']} mAh "
                          f"(used {r['rem_start'] - r['rem_end']} mAh), rest end {r['rest_end_mV']} mV"
                          + (f", R {min(r['R'])}..{max(r['R'])} mOhm (n={len(r['R'])})" if r["R"] else ""))
                return 0

            time.sleep(max(0.0, POLL_S - (time.time() - now)))
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="read-only readout of all cells")
    ap.add_argument("--rest", type=float, default=REST_MIN, metavar="MIN",
                    help=f"rest between charge and discharge in minutes (default {REST_MIN})")
    ap.add_argument("--rint", type=float, default=RINT_MIN, metavar="MIN",
                    help=f"internal resistance measurement period in minutes, 0 = off (default {RINT_MIN})")
    args = ap.parse_args()

    dev = hw.find_cp2112_bus()
    if dev is None:
        sys.exit("CP2112 I2C bus not found")
    bus = hw.Bus(dev)
    if args.status:
        status(bus)
        return 0
    try:
        return run(bus, args.rest * 60, args.rint * 60)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except RuntimeError as e:
        print(f"ABORT: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
