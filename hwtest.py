#!/usr/bin/env python3
"""Hardware test for LiIon4cycler.

Topology:
  CP2112 (USBI2C01)  -- /dev/i2c-N (kernel hid_cp2112) + /dev/hidrawN for GPIO
   +-- PCA9531 LED driver (I2CPWM01) @ 0x63, open-drain
   |     LED0..3 indicators (anode -> VDD), LED4..7 loads (L = OFF, H = ON)
   +-- TCA9548A mux (I2CHUB02) @ 0x70..0x73 (found by scanning, see find_mux)
         +-- ch0..ch3: BQ34Z100 gauge (LION1CELL01B) @ 0x55

Steps:
  1. blink TX/RX LEDs on USBI2C01 (CP2112 GPIO0/1)
  2. blink indicator LEDs LED0..LED3 on I2CPWM01 (LED4..7 drive FETs, left OFF)
  3. identify every BQ34Z100 gauge (device type, firmware), then write the data flash
     configuration matching its firmware (GAUGE_CONFIGS, only differing values are written)
  4. read all chargers (registers chosen by firmware) and print values + configuration read-back
Exit code is the number of failed checks (0 = all OK).

Usage:
  ./hwtest.py                    the only connected cycler
  ./hwtest.py --list             serial numbers of all connected cyclers (CP2112)
  ./hwtest.py --cycler 9BFD      cycler with CP2112 serial ending ...9BFD (TX/RX LEDs blink)

No external dependencies; uses I2C_RDWR and HIDIOC[GS]FEATURE ioctls directly.
"""
import argparse
import ctypes
import threading
import fcntl
import glob
import os
import sys
import time

CP2112_HID_ID = "000010C4:0000EA90"
PWM_ADDR = 0x63
MUX_ADDRS = (0x70, 0x71, 0x72, 0x73)   # possible I2CHUB02 addresses (A0, A1 jumpers; A2 = GND)
GAUGE_ADDR = 0x55
CHARGER_CHANNELS = (0, 1, 2, 3)

# ---------------------------------------------------------------- I2C bus ---
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001


class i2c_msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class i2c_rdwr_ioctl_data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(i2c_msg)), ("nmsgs", ctypes.c_uint32)]


# ------------------------------------------------- cycler selection (USB serial) ---
# Several cyclers on one computer are told apart by the USB serial number of their CP2112
# (USBI2C01).  Select one with --cycler SERIAL (any script) or the environment variable
# CYCLER=SERIAL; a unique end of the serial number is enough.  With a single CP2112 connected
# nothing has to be selected.
_selected = os.environ.get("CYCLER") or None
_lock = None


def list_cp2112():
    """All connected CP2112: list of {"serial", "i2c": /dev/i2c-N, "hidraw": /dev/hidrawN}."""
    out = []
    for d in sorted(glob.glob("/sys/bus/hid/devices/*")):
        try:
            with open(d + "/uevent") as f:
                ue = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        except OSError:
            continue
        if ue.get("HID_ID", "").upper() != "0003:" + CP2112_HID_ID.upper():
            continue
        i2c = glob.glob(d + "/i2c-*")
        raw = glob.glob(d + "/hidraw/hidraw*")
        out.append({"serial": ue.get("HID_UNIQ", ""),
                    "i2c": "/dev/" + os.path.basename(i2c[0]) if i2c else None,
                    "hidraw": "/dev/" + os.path.basename(raw[0]) if raw else None})
    return out


def select_cycler(serial):
    """Choose the cycler by (the end of) its CP2112 serial number; None = the only one connected."""
    global _selected
    if serial:
        _selected = serial


def cycler():
    """The selected CP2112 (dict from list_cp2112()); exits with a message if none / ambiguous."""
    devs = list_cp2112()
    names = ", ".join(d["serial"] or "?" for d in devs) or "none"
    if _selected:
        m = [d for d in devs if d["serial"].upper().endswith(_selected.upper())]
        if len(m) != 1:
            raise SystemExit(f"cycler '{_selected}' " + ("not found" if not m else "is ambiguous")
                             + f" (connected CP2112: {names})")
        return m[0]
    if not devs:
        raise SystemExit("no CP2112 (USBI2C01) found - connected? hid_cp2112 driver loaded?")
    if len(devs) > 1:
        raise SystemExit(f"{len(devs)} cyclers connected (CP2112 serial: {names}) - "
                         f"select one with --cycler SERIAL or CYCLER=SERIAL")
    return devs[0]


def cycler_serial():
    return cycler()["serial"]


def lock_cycler(force=False):
    """Exclusive use of the selected cycler by this process (flock, released on exit).
    Returns the serial number; exits if another script uses the same cycler (unless force)."""
    global _lock
    serial = cycler_serial()
    if _lock is None:
        f = open(f"/tmp/liion4cycler-{serial or 'noserial'}.lock", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            who = f.read().strip() or "?"
            f.close()
            if not force:
                raise SystemExit(f"cycler {serial} is in use by PID {who} - stop it first")
            print(f"WARNING: cycler {serial} is in use by PID {who} (forced)")
            return serial
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()} {' '.join(os.path.basename(a) for a in sys.argv)}\n")
        f.flush()
        _lock = f
    return serial


def add_cycler_arg(ap):
    ap.add_argument("--cycler", metavar="SERIAL",
                    help="CP2112 serial number (or its unique end) of the cycler to use; "
                         "needed only with several cyclers connected (also env CYCLER)")


def open_cycler(args, lock=True, calibrated=True):
    """Select the cycler from args.cycler, lock it, open its I2C bus and load its calibration
    (bus.cal, see load_calibration; calibrated=False -> none, raw values) -> (Bus, serial)."""
    select_cycler(getattr(args, "cycler", None))
    serial = lock_cycler() if lock else cycler_serial()
    dev = cycler()["i2c"]
    if dev is None:
        raise SystemExit(f"cycler {serial}: CP2112 has no I2C bus (hid_cp2112 driver loaded?)")
    bus = Bus(dev)
    bus.serial = serial
    bus.cal = load_calibration(serial) if calibrated else {}
    if calibrated:
        print(cal_info(bus))
    return bus, serial


# ------------------------------------------------------------- calibration ---
# calibration.csv next to the scripts, one line per cycler channel:
#   cycler,ch,U_gain,U_offset_mV,I_gain,I_offset_mA,date,note     ('#' lines are comments)
# corrected = gauge value * gain + offset.  No line for a channel = gauge values used as they are.
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.csv")
CAL_FIELDS = (("U_gain", 1.0), ("U_offset_mV", 0.0), ("I_gain", 1.0), ("I_offset_mA", 0.0))


def load_calibration(serial, path=CAL_FILE):
    """{ch: {"U_gain", "U_offset_mV", "I_gain", "I_offset_mA"}} of the cycler; {} if none."""
    import csv
    cal = {}
    try:
        with open(path, newline="") as f:
            lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    except FileNotFoundError:
        return cal
    for n, r in enumerate(csv.DictReader(lines), 2):
        try:
            if (r.get("cycler") or "").strip().upper() != (serial or "").upper():
                continue
            cal[int(r["ch"])] = {k: float(r.get(k) or dflt) for k, dflt in CAL_FIELDS}
        except (KeyError, TypeError, ValueError) as e:
            raise SystemExit(f"{path}: bad calibration line {n} ({e}): {r}")
    return cal


def cal_info(bus):
    cal = getattr(bus, "cal", {})
    name = os.path.basename(CAL_FILE)
    if not cal:
        return (f"Calibration: none for cycler {getattr(bus, 'serial', '?')} in {name} "
                f"- gauge values used as they are")
    missing = [ch for ch in CHARGER_CHANNELS if ch not in cal]
    return (f"Calibration: {name}, " + ", ".join(f"ch{ch}" for ch in sorted(cal))
            + (f"  (no calibration for " + ", ".join(f"ch{ch}" for ch in missing) + ")" if missing else ""))


def apply_calibration(d, c):
    """Correct U_mV / I_mA of a read_gauge() dict with calibration c (or None); keeps the raw
    values in U_raw_mV / I_raw_mA and sets d["cal"]."""
    d["U_raw_mV"], d["I_raw_mA"] = d["U_mV"], d["I_mA"]
    if c:
        d["U_mV"] = round(d["U_mV"] * c["U_gain"] + c["U_offset_mV"])
        d["I_mA"] = round(d["I_mA"] * c["I_gain"] + c["I_offset_mA"])
    d["cal"] = bool(c)
    return d


def find_cp2112_bus():
    return cycler()["i2c"]


class Bus:
    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDWR)
        self.lock = threading.Lock()          # one transfer at a time

    def _xfer(self, msgs):
        arr = (i2c_msg * len(msgs))(*msgs)
        with self.lock:
            fcntl.ioctl(self.fd, I2C_RDWR, i2c_rdwr_ioctl_data(arr, len(msgs)))

    @staticmethod
    def _msg(addr, flags, buf):
        return i2c_msg(addr, flags, len(buf), ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)))

    def write(self, addr, data):
        buf = (ctypes.c_uint8 * len(data))(*data)
        self._xfer([self._msg(addr, 0, buf)])

    def read(self, addr, n):
        buf = (ctypes.c_uint8 * n)()
        self._xfer([self._msg(addr, I2C_M_RD, buf)])
        return list(buf)

    def write_read(self, addr, wdata, n):
        wbuf = (ctypes.c_uint8 * len(wdata))(*wdata)
        rbuf = (ctypes.c_uint8 * n)()
        self._xfer([self._msg(addr, 0, wbuf), self._msg(addr, I2C_M_RD, rbuf)])
        return list(rbuf)

    def present(self, addr):
        try:
            self.read(addr, 1)
            return True
        except OSError:
            return False


# ------------------------------------------------------ CP2112 GPIO (HID) ---
REP_GPIO_CFG, REP_GPIO_SET = 0x02, 0x04
CP_LED_MASK = 0x03  # GPIO0 = TX LED, GPIO1 = RX LED


def _hid_ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord('H') << 8) | nr


def find_cp2112_hidraw():
    return cycler()["hidraw"]


class CP2112Gpio:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)

    def get_feature(self, rep, n):
        buf = bytearray([rep] + [0] * (n - 1))
        fcntl.ioctl(self.fd, _hid_ioc(0x07, n), buf)
        return list(buf)

    def set_feature(self, data):
        buf = bytearray(data)
        fcntl.ioctl(self.fd, _hid_ioc(0x06, len(buf)), buf)


def blink_usbi2c():
    path = find_cp2112_hidraw()
    if path is None:
        raise RuntimeError("CP2112 hidraw device not found")
    g = CP2112Gpio(path)
    cfg = g.get_feature(REP_GPIO_CFG, 5)
    # GPIO0/1 push-pull outputs, TX/RX-toggle special functions off
    g.set_feature([REP_GPIO_CFG, cfg[1] | CP_LED_MASK, cfg[2] | CP_LED_MASK, cfg[3] & ~0x06, cfg[4]])
    try:
        for i in range(10):
            g.set_feature([REP_GPIO_SET, (0x01, 0x02)[i % 2], CP_LED_MASK])
            time.sleep(0.3)
        for _ in range(3):
            g.set_feature([REP_GPIO_SET, 0x00, CP_LED_MASK]); time.sleep(0.4)
            g.set_feature([REP_GPIO_SET, 0x03, CP_LED_MASK]); time.sleep(0.4)
    finally:
        g.set_feature([REP_GPIO_CFG] + cfg[1:5])  # restore original config


# ------------------------------------------------------- PCA9531 (I2CPWM) ---
LS0, AI = 0x05, 0x10
FETS_OFF = 0xF0     # LED4..LED7 switch the loads: L = load OFF, H (released) = load ON


def pwm_leds(bus, mask):
    """bit=1 -> output driven low (LSx = 01), bit=0 -> released (Hi-Z, pulled high).
    Keep bits 4..7 set (FETS_OFF = loads OFF) unless a load should be ON."""
    ls0 = sum(1 << (2 * i) for i in range(4) if mask >> i & 1)
    ls1 = sum(1 << (2 * i) for i in range(4) if mask >> (i + 4) & 1)
    bus.write(PWM_ADDR, [AI | LS0, ls0, ls1])


def blink_i2cpwm(bus):
    """Indicators LED0..LED3 only; LED4..LED7 stay LOW (loads OFF)."""
    try:
        for _ in range(3):
            for i in range(4):
                pwm_leds(bus, FETS_OFF | 1 << i)
                time.sleep(0.2)
        for _ in range(3):
            pwm_leds(bus, FETS_OFF | 0x0F); time.sleep(0.4)
            pwm_leds(bus, FETS_OFF); time.sleep(0.4)
    finally:
        try:
            pwm_leds(bus, FETS_OFF)
        except OSError:
            pass


# ---------------------------------------------------- mux + BQ34Z100 gauge ---
def scan_mux(bus):
    """Addresses from MUX_ADDRS that answer on the bus."""
    return [a for a in MUX_ADDRS if bus.present(a)]


def find_mux(bus):
    """Find the I2CHUB02 (TCA9548A) at one of MUX_ADDRS and remember it in bus.mux_addr.
    Raises OSError if none or more than one answers."""
    found = scan_mux(bus)
    if len(found) != 1:
        bus.mux_addr = None
        raise OSError("I2CHUB02 not found at " + ", ".join(f"0x{a:02X}" for a in MUX_ADDRS) if not found
                      else "several I2CHUB02 found (" + ", ".join(f"0x{a:02X}" for a in found)
                      + ") - only one hub per cycler is supported")
    bus.mux_addr = found[0]
    return bus.mux_addr


def mux_select(bus, ch):
    """ch = None -> all channels off; TCA9548A control byte is a channel bitmask.
    The hub address is found on the first call (find_mux)."""
    if getattr(bus, "mux_addr", None) is None:
        find_mux(bus)
    bus.write(bus.mux_addr, [0 if ch is None else (1 << ch)])
    bus.mux_ch = ch


def gauge_word(bus, cmd, signed=False):
    lo, hi = bus.write_read(GAUGE_ADDR, [cmd], 2)
    v = lo | (hi << 8)
    if signed and v & 0x8000:
        v -= 0x10000
    return v


CURRENT_MIN_FW = 0x0016     # Current() 0x10 exists from this FW on; FW 0x0006 reads it as 0
CURRENT_CTRL_FW = (0x0006,)  # old FW: instantaneous current via Control() subcommand CURRENT 0x0018


def gauge_fw(bus):
    """Firmware version of the gauge on the selected mux channel (cached per channel)."""
    cache = bus.__dict__.setdefault("gauge_fw", {})
    ch = getattr(bus, "mux_ch", None)
    if ch not in cache:
        cache[ch] = gauge_ctrl(bus, 0x0002)
    return cache[ch]


def identify_gauge(bus):
    """Device type / firmware / hardware / chemistry of the gauge on the selected channel.
    Refreshes the per-channel firmware cache used by gauge_current()."""
    info = {"dev": gauge_ctrl(bus, 0x0001), "fw": gauge_ctrl(bus, 0x0002),
            "hw": gauge_ctrl(bus, 0x0003), "chem": gauge_ctrl(bus, 0x0008),
            "sealed": bool(gauge_ctrl(bus, 0x0000) & 0x2000)}
    bus.__dict__.setdefault("gauge_fw", {})[getattr(bus, "mux_ch", None)] = info["fw"]
    return info


def gauge_current(bus):
    """(current mA, instantaneous?) - FW 0x0016: Current() 0x10; FW 0x0006: Control() CURRENT
    0x0018 (both 1 s values); unknown FW: the filtered AverageCurrent() 0x0A."""
    fw = gauge_fw(bus)
    if fw >= CURRENT_MIN_FW:
        return gauge_word(bus, 0x10, signed=True), True
    if fw in CURRENT_CTRL_FW:
        v = gauge_ctrl(bus, 0x0018)
        return (v - 0x10000 if v & 0x8000 else v), True
    return gauge_word(bus, 0x0A, signed=True), False


def read_gauge(bus):
    i, inst = gauge_current(bus)
    return {
        "U_mV": gauge_word(bus, 0x08),
        "I_mA": i,
        "I_inst": inst,
        "SoC_%": bus.write_read(GAUGE_ADDR, [0x02], 1)[0],
        "MaxErr_%": bus.write_read(GAUGE_ADDR, [0x03], 1)[0],
        "RemCap_mAh": gauge_word(bus, 0x04),
        "FullCap_mAh": gauge_word(bus, 0x06),
        "T_C": round(gauge_word(bus, 0x0C) / 10.0 - 273.15, 1),
        "DesCap_mAh": gauge_word(bus, 0x3C),
    }


def gauge_ctrl(bus, sub):
    """Control() subcommand; returns the 16-bit response word."""
    bus.write(GAUGE_ADDR, [0x00, sub & 0xFF, sub >> 8])
    time.sleep(0.01)
    lo, hi = bus.write_read(GAUGE_ADDR, [0x00], 2)
    return lo | hi << 8


# ---- BQ34Z100 data flash configuration (offsets from SLUSAU1C, Table 7-8) ---
# Cell: 3500 mAh, 4.2 V full, 2.5 V empty; 7 LEDs on SN74HC164 shift register.
# (subclass, offset, size, value, name[, opt]); data flash values are big-endian.
#   opt = int        bit mask: only these bits are set to `value`, the others are kept
#   opt = ("if", d)  initial value: written only while the field still holds the TI default d
#                    (Qmax is learned by the gauge - never overwrite a learned value)
# Gauge SLEEP (|AverageCurrent| < Sleep Current, at rest) measures only every 20 s -> after the load
# is switched ON the values stay stale for up to 20 s.  Pack Configuration (64/0, same on both FW):
#   [IWAKE RSNS1 RSNS0] = 0 0 1  wake-up comparator ON, 1.25 mV across Rsense (TI default; found 0 0 0
#                                = disabled on most boards) -> a load step wakes the gauge at once
#   [SLEEP] = 0                  FW 0x0006: gauge never sleeps (verified); FW 0x0016 sleeps even with
#                                bit 5 = 0 (and with Sleep Current = 0), so only the wake-up is fixed there
WAKE_ON = (64, 0, 2, 0x0100, "Pack Configuration [IWAKE RSNS1 RSNS0] = 001 (current wake-up)", 0x0700)
NO_SLEEP = (64, 0, 2, 0x0100, "Pack Configuration [SLEEP]=0, [IWAKE RSNS]=001 (no sleep, wake-up)", 0x0720)
QMAX_DEFAULT = 1000               # TI default Qmax Cell 0 [mAh]
GAUGE_CONFIG = [
    (48, 11, 2, 3500, "Design Capacity [mAh]"),
    (48, 13, 2, 12600, "Design Energy [mWh] (3500 mAh * 3.6 V)"),
    (48, 17, 2, 4200, "Cell Charge Voltage T1-T2 [mV]"),
    (48, 19, 2, 4200, "Cell Charge Voltage T2-T3 [mV]"),
    (64, 4, 1, 0x63, "LED_Comm Configuration (7 ext. LEDs, I2C comm)"),
    (64, 7, 1, 1, "Number of series cells"),
    (80, 53, 2, 2500, "Cell Terminate Voltage [mV]"),
    WAKE_ON,
    (82, 0, 2, 3500, "Qmax Cell 0 [mAh] (initial, then learned)", ("if", QMAX_DEFAULT)),
]

# Older BQ34Z100 firmware 0x0006 (datasheet SLUSAU1B, Dec 2012) has a different layout of
# subclasses 48 and 80.  The first four lines repair fields that were overwritten when the
# FW 0x0016 offsets above were applied to such a gauge (TI defaults / 90 % of Design Capacity;
# 80/53 is undocumented, its original value 0x1400 follows from the FW 0x0016 block pattern).
GAUGE_CONFIG_FW0006 = [
    (48, 13, 2, 0, "Manufacture Date (repair, default 0)"),
    (48, 17, 2, 0, "Cycle Count (repair, default 0)"),
    (48, 19, 2, 3150, "CC Threshold [mAh] (repair, 90 % of Design Capacity)"),
    (80, 53, 2, 0x1400, "undocumented 80/53 (repair, original 0x1400)"),
    (48, 21, 2, 3500, "Design Capacity [mAh]"),
    (48, 23, 2, 12600, "Design Energy [mWh] (3500 mAh * 3.6 V)"),
    (48, 28, 2, 4200, "Cell Charge Voltage T1-T2 [mV]"),
    (48, 30, 2, 4200, "Cell Charge Voltage T2-T3 [mV]"),
    (64, 4, 1, 0x63, "LED_Comm Configuration (7 ext. LEDs, I2C comm)"),
    (64, 7, 1, 1, "Number of series cells"),
    (80, 67, 2, 2500, "Cell Terminate Voltage [mV]"),
    NO_SLEEP,
    (82, 0, 2, 3500, "Qmax Cell 0 [mAh] (initial, then learned)", ("if", QMAX_DEFAULT)),
]

GAUGE_CONFIGS = {0x0016: GAUGE_CONFIG, 0x0006: GAUGE_CONFIG_FW0006}   # firmware -> data flash layout
GAUGE_FW_INFO = {
    0x0016: "SLUSAU1C layout, Current() 0x10 instantaneous",
    0x0006: "SLUSAU1B layout (old), instantaneous current via Control() CURRENT 0x0018",
}
KNOWN_FW = ", ".join(f"0x{fw:04X}" for fw in sorted(GAUGE_CONFIGS))

# Learned (Impedance Track) data - same location on FW 0x0006 and 0x0016.  A new cell must not
# inherit the values learned on the previous one: new_cell=True in configure_gauge() writes these
# defaults (Qmax = Design Capacity, Ra tables = TI default for CHEM 0x0107, flags "not updated").
RA_DEFAULT = [105, 100, 113, 143, 98, 97, 108, 89, 86, 85, 87, 90, 110, 647, 1500]
LEARNED_RESET = (
    [(82, 2, 2, 0, "Cycle Count (new cell)"),
     (82, 4, 1, 0x00, "Update Status: clear learned bits (new cell)", 0x03)]   # 05/06 -> 04, 02 -> 00
    + [(88, 0, 2, 0xFF55, "R_a0 flag (new cell: not updated)")]
    + [(88, 2 + 2 * i, 2, v, f"R_a0 {i} (new cell)") for i, v in enumerate(RA_DEFAULT)]
    + [(89, 0, 2, 0xFFFF, "R_a0x flag (new cell: not updated)")]
    + [(89, 2 + 2 * i, 2, v, f"R_a0x {i} (new cell)") for i, v in enumerate(RA_DEFAULT)])


def cfg_items(fw, new_cell=False):
    """GAUGE_CONFIGS[fw] normalised to (sub, off, size, val, name, mask, initial_default).
    new_cell: initial values (Qmax) are forced and the learned data are reset as well."""
    for e in GAUGE_CONFIGS[fw] + (LEARNED_RESET if new_cell else []):
        sub, off, size, val, name = e[:5]
        opt = e[5] if len(e) > 5 else None
        mask = opt if isinstance(opt, int) else (1 << 8 * size) - 1
        init = opt[1] if isinstance(opt, tuple) and not new_cell else None
        yield sub, off, size, val, name, mask, init


def cfg_ok(cur, val, mask, init):
    """True if the data flash value `cur` satisfies the config entry."""
    return (cur & mask) == (val & mask) if init is None else cur != init


UNSEAL_KEY = 0x36720414           # TI default "Sealed to Unsealed"
FLASH_MIN_MV = 2900               # Flash Update OK Cell Volt is 2800 mV by default


def flash_read_block(bus, sub, blk):
    bus.write(GAUGE_ADDR, [0x61, 0x00]); time.sleep(0.01)   # BlockDataControl
    bus.write(GAUGE_ADDR, [0x3E, sub]); time.sleep(0.01)    # DataFlashClass
    bus.write(GAUGE_ADDR, [0x3F, blk]); time.sleep(0.05)    # DataFlashBlock
    return bus.write_read(GAUGE_ADDR, [0x40], 32)


def flash_write_block(bus, sub, blk, data):
    flash_read_block(bus, sub, blk)                          # select block
    bus.write(GAUGE_ADDR, [0x40] + list(data)); time.sleep(0.01)
    bus.write(GAUGE_ADDR, [0x60, 0xFF - (sum(data) & 0xFF)])  # checksum commits to flash
    time.sleep(1.0)                                          # flash write time


def configure_gauge(bus, new_cell=False):
    """Apply the config for the gauge's firmware, writing only differing values.
    new_cell=True also discards the learned values (Qmax, Ra tables, cycle count).
    Returns list of changes."""
    fw = gauge_ctrl(bus, 0x0002)
    if fw not in GAUGE_CONFIGS:
        raise RuntimeError(f"gauge FW 0x{fw:04X} - unknown data flash layout, not configured "
                           f"(known FW: {KNOWN_FW})")
    if gauge_ctrl(bus, 0x0000) & 0x2000:                     # SS = sealed
        gauge_ctrl(bus, UNSEAL_KEY & 0xFFFF)
        gauge_ctrl(bus, UNSEAL_KEY >> 16)
        time.sleep(0.05)
        if gauge_ctrl(bus, 0x0000) & 0x2000:
            raise RuntimeError("gauge is SEALED and default unseal key failed")
    u = gauge_word(bus, 0x08)
    blocks = {}
    for sub, off, size, val, name, mask, init in cfg_items(fw, new_cell):
        blocks.setdefault((sub, off // 32), []).append((off % 32, size, val, name, mask, init))
    changes = []
    for (sub, blk), items in blocks.items():
        data = flash_read_block(bus, sub, blk)
        new = list(data)
        for o, size, val, name, mask, init in items:
            old = int.from_bytes(bytes(new[o:o + size]), "big")
            if init is not None and old != init:
                continue                                     # learned / already set - keep
            want = (old & ~mask) | (val & mask)
            if old != want:
                new[o:o + size] = list(want.to_bytes(size, "big"))
                fmt = "0x{:04X}" if mask != (1 << 8 * size) - 1 else "{}"
                changes.append(f"{name}: {fmt.format(old)} -> {fmt.format(want)}")
        if new != data:
            if u < FLASH_MIN_MV:
                raise RuntimeError(f"cell voltage {u} mV too low for flash write (min {FLASH_MIN_MV} mV)")
            flash_write_block(bus, sub, blk, new)
            if flash_read_block(bus, sub, blk) != new:
                raise RuntimeError(f"flash verify failed for subclass {sub} block {blk}")
    if changes:
        # RESET to apply; write only - no read-back while the gauge reboots
        bus.write(GAUGE_ADDR, [0x00, 0x41, 0x00])
        time.sleep(2.0)
    return changes


def read_gauge_config(bus, fw):
    """Read back the data flash values of GAUGE_CONFIGS[fw]: list of (name, value, expected text, ok)."""
    cache, out = {}, []
    for sub, off, size, val, name, mask, init in cfg_items(fw):
        key = (sub, off // 32)
        if key not in cache:
            cache[key] = flash_read_block(bus, *key)
        o = off % 32
        cur = int.from_bytes(bytes(cache[key][o:o + size]), "big")
        if init is not None:
            exp = f"not {init}"
        elif mask != (1 << 8 * size) - 1:
            exp = f"bits 0x{mask:04X} = 0x{val & mask:04X}"
            cur = f"0x{cur:04X}"
        else:
            exp = str(val)
        out.append((name, cur, exp, cfg_ok(int(cur, 0) if isinstance(cur, str) else cur, val, mask, init)))
    return out


def read_gauge_retry(bus, tries=2):
    """Few tries only: a broken gauge may lock SDA low after repeated NACKs."""
    for _ in range(tries):
        try:
            return read_gauge(bus)
        except OSError as e:
            err = e
            time.sleep(0.05)
    raise err


# ------------------------------------------------------------------- main ---
def main():
    errors = []
    warnings = []

    def fail(msg):
        errors.append(msg)
        print(f"  ERROR: {msg}")

    def warn(msg):
        warnings.append(msg)
        print(f"  WARNING: {msg}")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_cycler_arg(ap)
    ap.add_argument("--list", action="store_true", help="list connected cyclers (CP2112 serial numbers)")
    args = ap.parse_args()
    if args.list:
        for d in list_cp2112():
            print(f"{d['serial'] or '?':<12} {d['i2c']}  {d['hidraw']}")
        return 0
    select_cycler(args.cycler)
    serial = lock_cycler()
    print(f"Cycler {serial}")
    cal = load_calibration(serial)
    print(f"  calibration.csv: " + (", ".join(f"ch{ch}" for ch in sorted(cal)) if cal else "no entry")
          + " - this test prints raw gauge values (use them for calibration)")

    # 1) USBI2C LEDs
    print("[1] USBI2C01 (CP2112): blinking TX/RX LEDs - identifies this cycler ...")
    try:
        blink_usbi2c()
        print("  OK")
    except (OSError, RuntimeError) as e:
        fail(f"USBI2C01 GPIO: {e}")

    dev = find_cp2112_bus()
    if dev is None:
        fail("CP2112 I2C bus not found (is hid_cp2112 loaded?)")
        return len(errors)
    try:
        bus = Bus(dev)
    except OSError as e:
        fail(f"cannot open {dev}: {e}")
        return len(errors)
    try:
        pwm_leds(bus, FETS_OFF)          # first of all: loads OFF (LED4..LED7 low)
    except OSError:
        pass

    # 2) I2CPWM LEDs
    print(f"[2] I2CPWM01 (PCA9531 @ 0x{PWM_ADDR:02X}): blinking indicators LED0..LED3 ...")
    try:
        blink_i2cpwm(bus)
        print("  OK")
    except OSError as e:
        fail(f"I2CPWM01 @ 0x{PWM_ADDR:02X} not responding ({e})")

    # 3) chargers
    print("[3] Chargers via I2CHUB02 (TCA9548A @ " + "/".join(f"0x{a:02X}" for a in MUX_ADDRS) + ") ...")
    try:
        find_mux(bus)
        print(f"  I2CHUB02 found @ 0x{bus.mux_addr:02X}")
        mux_select(bus, None)
    except OSError as e:
        fail(f"I2CHUB02: {e}")
    else:
        fws = {}
        print("  identifying gauges (known FW: " + ", ".join(
            f"0x{fw:04X} = {info}" for fw, info in sorted(GAUGE_FW_INFO.items())) + ") ...")
        for ch in CHARGER_CHANNELS:
            try:
                mux_select(bus, ch)
                g = identify_gauge(bus)
                fws[ch] = g["fw"]
                known = GAUGE_FW_INFO.get(g["fw"], "UNKNOWN firmware")
                print(f"  ch{ch}: DEVICE_TYPE 0x{g['dev']:04X} FW 0x{g['fw']:04X} HW 0x{g['hw']:04X} "
                      f"CHEM 0x{g['chem']:04X} {'SEALED' if g['sealed'] else 'unsealed'} - {known}")
                if g["fw"] not in GAUGE_CONFIGS:
                    warn(f"ch{ch}: unknown gauge FW 0x{g['fw']:04X} (known: {KNOWN_FW}) - data flash layout "
                         f"unknown, NOT configured; current = AverageCurrent() only. Find the datasheet "
                         f"for this FW and add its layout to GAUGE_CONFIGS")
                if g["dev"] != 0x0100:
                    fail(f"ch{ch}: unexpected device type 0x{g['dev']:04X} (BQ34Z100 = 0x0100)")
            except OSError as e:
                fail(f"ch{ch}: gauge @ 0x{GAUGE_ADDR:02X} not responding ({e})")

        print("  configuring gauges (3500 mAh, 4.2 V / 2.5 V, 7 LEDs) ...")
        for ch in fws:
            if fws[ch] not in GAUGE_CONFIGS:
                print(f"  ch{ch} (FW 0x{fws[ch]:04X}): SKIPPED - unknown firmware")
                continue
            try:
                mux_select(bus, ch)
                changes = configure_gauge(bus)
                print(f"  ch{ch} (FW 0x{fws[ch]:04X}): " + ("already configured" if not changes else "updated"))
                for c in changes:
                    print(f"         {c}")
            except (OSError, RuntimeError) as e:
                fail(f"ch{ch}: gauge configuration failed ({e})")

        print()
        print(f"  {'ch':>3} {'FW':>6} {'U[mV]':>6} {'I[mA]':>7} {'SoC':>4} {'Err':>4} "
              f"{'Rem[mAh]':>8} {'Full[mAh]':>9} {'Des[mAh]':>8} {'T[C]':>6}")
        avg_used = False
        for ch in CHARGER_CHANNELS:
            try:
                mux_select(bus, ch)
            except OSError as e:
                fail(f"ch{ch}: mux select failed ({e}) - bus may be stuck, power-cycle modules")
                break
            try:
                d = read_gauge_retry(bus)
                mark = " " if d["I_inst"] else "*"
                avg_used |= not d["I_inst"]
                print(f"  {ch:>3} 0x{gauge_fw(bus):04X} {d['U_mV']:>6} {d['I_mA']:>6}{mark} {d['SoC_%']:>3}% "
                      f"{d['MaxErr_%']:>3}% {d['RemCap_mAh']:>8} {d['FullCap_mAh']:>9} "
                      f"{d['DesCap_mAh']:>8} {d['T_C']:>6}")
            except OSError as e:
                fail(f"ch{ch}: gauge @ 0x{GAUGE_ADDR:02X} not responding ({e})")
        print("  I = instantaneous current (FW 0x0016: Current() 0x10, FW 0x0006: Control() 0x0018)"
              + ("; * = AverageCurrent() 0x0A (unknown FW)" if avg_used else ""))

        print("\n  data flash read-back (layout by firmware):")
        for ch, fw in fws.items():
            if fw not in GAUGE_CONFIGS:
                continue
            try:
                mux_select(bus, ch)
                print(f"  ch{ch} FW 0x{fw:04X}:")
                for name, val, exp, ok in read_gauge_config(bus, fw):
                    print(f"         {name:<58} {val:>6}" + ("" if ok else f"   <-- expected {exp}"))
                    if not ok:
                        fail(f"ch{ch}: {name} = {val}, expected {exp}")
            except OSError as e:
                fail(f"ch{ch}: data flash read-back failed ({e})")
        try:
            mux_select(bus, None)
        except OSError as e:
            fail(f"I2C bus stuck after charger readout ({e}) - power-cycle modules")

    print("\nRESULT: " + ("all OK" if not errors else f"{len(errors)} error(s)")
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    for e in errors:
        print(f"  - ERROR: {e}")
    for w in warnings:
        print(f"  - WARNING: {w}")
    return len(errors)


if __name__ == "__main__":
    sys.exit(main())
