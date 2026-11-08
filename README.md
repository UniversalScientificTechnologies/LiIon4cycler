# LiIon4cycler

Li-ion cell tester for 4 cells at a time. It measures the capacity and the DC internal resistance of each cell and then brings the cells to a transport voltage. It is built from MLAB kit modules and is controlled from a Linux computer over USB.

## Hardware

```
PC (Linux, USB)
 └─ USBI2C01 (CP2112)  USB → I2C + GPIO, serial number = cycler identification
     ├─ I2C ─ I2CPWM01 (PCA9531 @ 0x63)   LED0–3 indicators, LED4–7 switch the discharge loads
     ├─ I2C ─ I2CHUB02 (TCA9548A @ 0x73)  I2C multiplexer, channels 0–3
     │         └─ ch0–ch3: LION1CELL01B   BQ24103 charger + BQ34Z100 gauge @ 0x55
     └─ GPIO3–6 → charger /CE             H = charger off
```

| Cell channel | Charger /CE | Load (PCA9531) | Indicator LED |
|---|---|---|---|
| ch0 | GPIO6 | LED4 | LED0 |
| ch1 | GPIO5 | LED5 | LED1 |
| ch2 | GPIO4 | LED6 | LED2 |
| ch3 | GPIO3 | LED7 | LED3 |

- **Charging:** each cell has its own BQ24103 charger. The scripts switch it on and off through its /CE input. Without control from USB the chargers charge, because /CE has a pull-down.
- **Discharging:** one resistive load per channel, switched statically by a PCA9531 output (L = off, H = on).
- **Measurement:** the BQ34Z100 gauge on each board measures voltage, current and temperature. Gauges with either of the two known firmware versions (0x0016 and 0x0006) are handled automatically.
- Several cyclers on one computer are told apart by the serial number of their CP2112.

Details and findings from bring-up are in [POZNATKY.md](POZNATKY.md) (Czech). Open work is in [TODO.md](TODO.md).

## Requirements

- Linux with the `hid_cp2112` driver, which is part of the standard kernel.
- Python 3; no extra packages are needed, except `matplotlib` for `plotlog.py`.
- Read/write access to the converter's `/dev/i2c-*` and `/dev/hidraw*`, for example through a udev rule for USB ID `10c4:ea90`.

## Scripts

| Script | Purpose |
|---|---|
| `hwtest.py` | Hardware test and shared library. Blinks the LEDs, finds the gauges, identifies their firmware, loads the matching configuration and prints the readings. |
| `alloff.py` | Puts the cycler into a safe state with all chargers and loads off. |
| `chargertest.py` | Checks the charger /CE wiring; with `--loads` it also checks the loads. |
| `monitor.py` | Reads the gauges only, with chargers and loads off. Optional CSV log. |
| `cycling.py` | Cell test: discharge → charge → discharge. Writes capacity and DC resistance to `results.csv`. |
| `transport.py` | Charges or discharges the cells to the 3.5 V open-circuit transport voltage. |
| `plotlog.py` | Live plot of voltage, current and resistance from a running log. |

Every script has `--help`. Only one script can use a cycler at a time; a second one refuses to start.

## Test procedure

1. **Check the hardware** after assembly or after replacing a board. The output should end with `RESULT: all OK`.
   ```
   ./hwtest.py
   ```
2. **Switch everything off** before connecting power supplies and cells:
   ```
   ./alloff.py
   ```
3. **Insert the cells and start the test.** Cell labels are optional:
   ```
   ./cycling.py --cells A1,A2,A3,A4
   ```
   - Each cell is discharged, rests 10 min, is charged, rests 30 min and is discharged again. The capacity (mAh, Wh) comes from the second discharge.
   - The DC internal resistance is measured every 5 min during both discharges.
   - At start, the gauge configuration is checked and the values the gauge learned on the previous cells are discarded. Use `--same-cells` to keep them when you continue with the same cells.
   - More options: `--empty MV` (discharge end voltage, default 3500), `--rest MIN`, `--rest-empty MIN`, `--rint MIN`.
4. **Watch the progress** in a second terminal. Without an argument it opens the newest log:
   ```
   ./plotlog.py
   ```
5. **Results:** `results.csv` gets one row per cell and test. Each row holds:
   - capacity and energy;
   - DC resistance (median, min, max);
   - discharge current;
   - voltage after charge and charged capacity;
   - maximum temperature;
   - the cycler serial and the status (`OK` or `ABORTED in …`);
   - whether a calibration was used (`calibrated`).

   The file is only ever appended to, never overwritten.
6. **Prepare the cells for transport.** This sets each cell to 3.5 V open-circuit:
   ```
   ./transport.py
   ```
   Then disconnect the cells first and USB second. Without USB the chargers switch on by themselves.

For a quick look at the cells without changing anything, use `./cycling.py --status`. For continuous readings, use `./monitor.py`.

## Calibration

Voltage and current readings are corrected per cycler and channel from `calibration.csv` (next to the scripts). There is one line per channel; the cycler is identified by its CP2112 serial number:

```
cycler,ch,U_gain,U_offset_mV,I_gain,I_offset_mA,date,note
00A59BFD,0,1.001184,0,0.963687,0,2026-11-08,"bench meter: 3.715 V -> 3.7194 V, 1.432 A -> 1.380 A"
```

- Corrected value = gauge value × gain + offset (mV, mA). Lines starting with `#` are comments.
- With one calibration point: gain = meter reading / gauge reading, offset = 0. `./hwtest.py` prints the raw gauge values to compare against the meter.
- A channel without a line uses the gauge values as they are. The scripts print at start which channels are calibrated.
- The logs show whether calibration was used: column `cal` (1/0) and the raw gauge values `U_raw_mV` and `I_raw_mA`. `results.csv` has the column `calibrated`.

## Several cyclers on one computer

```
./hwtest.py --list                    # serial numbers of connected CP2112 converters
./hwtest.py --cycler 9BFD             # test one cycler (its TX/RX LEDs blink)
./cycling.py --cycler 9BFD --cells …  # a unique end of the serial number is enough
CYCLER=9BFD ./monitor.py              # same, via environment variable
./alloff.py --all                     # switch off every cycler that is not in use
```

Log names contain the serial number, for example `cycling_00A59BFD_20261108_125405.csv`. All cyclers write to the same `results.csv`; the `cycler` column tells the rows apart.

## Logs

| File | Content |
|---|---|
| `cycling_<serial>_<date>.csv` | Every reading of every cell: state, load, U, I, T, SoC, R. |
| `transport_<serial>_<date>.csv` | Progress of `transport.py`, same columns. |
| `monitor_<serial>_<date>.csv` | Log of `monitor.py --log`. |
| `results.csv` | Summary results of the tests. |
| `calibration.csv` | Calibration per cycler and channel (configuration, kept in git). |
