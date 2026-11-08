#!/usr/bin/env python3
"""Verify wiring of charger enable (/CE via CP2112 GPIO) and discharge loads (PCA9531).

Charger test:
  1. all chargers OFF -> baseline currents (no channel should charge)
  2. for each channel: enable ONLY its charger for HOLD_S seconds, record max current
     of every channel -> only the enabled channel should show charge current
  (instantaneous Current() 0x10, or a terminal voltage rise)
  A full cell may take little or no current; then the result is INCONCLUSIVE
  (discharge the cell a bit and repeat).

Load test (--loads):
  switch each load output LED4..LED7 H (indicator LED0..LED3 ON) for LOAD_S seconds, chargers OFF,
  report current of its channel (negative = discharging) - also check visually.

At the end all loads are OFF and chargers are left ON (= hardware default, /CE low).
"""
import argparse
import sys
import time

import hwtest as hw
from cycling import CHANNELS, CE_GPIO, IND_LED, FET_LED, FULL_MV, Chargers, Loads

CHARGE_MA = 50      # instantaneous current above this = charging
CHARGE_DU_MV = 15   # or terminal voltage rise above this = charger active
LOAD_MA = -20       # current below this = load is drawing
HOLD_S = 15
SETTLE_S = 5
LOAD_S = 6


def read_ch(bus, ch):
    """Instantaneous Current() (0x10, AverageCurrent() on old FW) and Voltage()."""
    hw.mux_select(bus, ch)
    return hw.gauge_current(bus)[0], hw.gauge_word(bus, 0x08)


def sample(bus, seconds):
    """Per channel: (mean current over 2nd half of window, last voltage)."""
    hist = {ch: [] for ch in CHANNELS}
    t0 = time.time()
    while True:
        for ch in CHANNELS:
            try:
                hist[ch].append((time.time() - t0,) + read_ch(bus, ch))
            except OSError as e:
                print(f"    ch{ch}: read error {e}")
        hw.mux_select(bus, None)
        if time.time() - t0 >= seconds:
            break
        time.sleep(1.0)
    res = {}
    for ch, h in hist.items():
        late = [i for t, i, u in h if t >= seconds / 2] or [i for t, i, u in h] or [0]
        res[ch] = (round(sum(late) / len(late)), h[-1][2] if h else 0)
    return res


def row(r, u0=None):
    out = []
    for ch in CHANNELS:
        i, u = r[ch]
        du = f" ({u - u0[ch]:+d})" if u0 else ""
        out.append(f"ch{ch}: {i:>5} mA {u:>4} mV{du}")
    return "  ".join(out)


def charger_test(bus, chargers):
    results = {}
    print(f"[1] all chargers OFF, settling {SETTLE_S + 5} s ...")
    chargers.all_off()
    time.sleep(SETTLE_S + 5)
    r = sample(bus, 4)
    print("    " + row(r))
    stuck_on = [ch for ch in CHANNELS if r[ch][0] > CHARGE_MA]
    for ch in stuck_on:
        print(f"    ch{ch}: charges although its /CE (GPIO{CE_GPIO[ch]}) is HIGH "
              f"-> /CE not connected or wired to another charger")

    print(f"[2] enabling one charger at a time for {HOLD_S} s ...")
    for ch in CHANNELS:
        chargers.all_off()
        time.sleep(SETTLE_S)
        u0 = {c: v[1] for c, v in sample(bus, 1).items()}
        chargers.set(ch, True)
        r = sample(bus, HOLD_S)
        chargers.set(ch, False)
        active = [c for c in CHANNELS
                  if r[c][0] > CHARGE_MA or r[c][1] - u0[c] >= CHARGE_DU_MV]
        print(f"    GPIO{CE_GPIO[ch]} -> ch{ch} enabled:  " + row(r, u0))
        others = [c for c in active if c != ch and c not in stuck_on]
        if ch in active and not others:
            res = "PASS"
        elif ch in active:
            res = "FAIL: other channels respond too: " + ", ".join(f"ch{c}" for c in others)
        elif others:
            res = f"FAIL: GPIO{CE_GPIO[ch]} enables charger on " + ", ".join(f"ch{c}" for c in others)
        elif r[ch][1] >= FULL_MV:
            res = f"INCONCLUSIVE (cell full, {r[ch][1]} mV, charger does not start)"
        else:
            res = "FAIL: no response (/CE wiring, charger power?)"
        results[ch] = res
        print(f"      -> {res}")
    for ch in stuck_on:
        results[ch] = "FAIL: charges with /CE high"
    return results


def load_test(bus, loads, chargers):
    results = {}
    chargers.all_off()
    print(f"[3] loads: each output ON for {LOAD_S} s (chargers OFF) ...")
    for ch in CHANNELS:
        loads.all_off()
        time.sleep(3)
        loads.set(ch, True)
        print(f"    LED{FET_LED[ch]} H, indicator LED{IND_LED[ch]} ON (load for ch{ch}) ...")
        r = sample(bus, LOAD_S)
        loads.set(ch, False)
        drawing = [c for c in CHANNELS if r[c][0] < LOAD_MA]
        print("    " + row(r))
        if drawing == [ch]:
            results[ch] = "PASS"
        elif not drawing:
            results[ch] = "NO CURRENT (load not connected yet? check LED visually)"
        else:
            results[ch] = "FAIL: current on " + ", ".join(f"ch{c}" for c in drawing)
        print(f"      -> {results[ch]}")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loads", action="store_true", help="also test discharge loads (LED4..LED7, H = ON)")
    args = ap.parse_args()

    bus = hw.Bus(hw.find_cp2112_bus())
    chargers = Chargers()            # starts with all chargers OFF
    loads = Loads(bus)               # starts with all loads OFF
    try:
        res = charger_test(bus, chargers)
        lres = load_test(bus, loads, chargers) if args.loads else {}
    finally:
        try:
            loads.all_off()
        except OSError as e:
            print(f"WARNING: could not reset PCA9531 ({e})")
        for ch in CHANNELS:
            chargers.set(ch, True)
        print("\nloads OFF, chargers ON (default)")

    print("\nSUMMARY")
    for ch in CHANNELS:
        print(f"  ch{ch}  charger GPIO{CE_GPIO[ch]}: {res.get(ch)}")
        if lres:
            print(f"       load LED{FET_LED[ch]}:     {lres.get(ch)}")
    bad = [r for r in list(res.values()) + list(lres.values()) if r.startswith("FAIL")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
