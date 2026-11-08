# LiIon4cycler

Tester Li-ion článků pro 4 články najednou. Změří kapacitu a DC vnitřní odpor článků a připraví je na transportní napětí. Je postavený z modulů stavebnice MLAB a ovládá se z linuxového počítače přes USB.

## Zařízení

```
PC (Linux, USB)
 └─ USBI2C01 (CP2112)  USB → I2C + GPIO, sériové číslo = identifikace cycleru
     ├─ I2C ─ I2CPWM01 (PCA9531 @ 0x63)   LED0–3 indikace, LED4–7 spínání vybíjecích zátěží
     ├─ I2C ─ I2CHUB02 (TCA9548A @ 0x73)  multiplexer I2C, kanály 0–3
     │         └─ ch0–ch3: LION1CELL01B   nabíječka BQ24103 + gauge BQ34Z100 @ 0x55
     └─ GPIO3–6 → /CE nabíječek           H = nabíječka vypnutá
```

| Kanál článku | /CE nabíječky | Zátěž (PCA9531) | Indikační LED |
|---|---|---|---|
| ch0 | GPIO6 | LED4 | LED0 |
| ch1 | GPIO5 | LED5 | LED1 |
| ch2 | GPIO4 | LED6 | LED2 |
| ch3 | GPIO3 | LED7 | LED3 |

- **Nabíjení:** každý článek má vlastní nabíječku BQ24103, kterou skript zapíná a vypíná signálem /CE. Bez řízení z USB (pull-down na /CE) nabíječky nabíjejí.
- **Vybíjení:** odporová zátěž na kanál, spínaná staticky výstupem PCA9531 (L = vypnuto, H = zapnuto).
- **Měření:** gauge BQ34Z100 na každé desce měří napětí, proud a teplotu. Gauge s rozdílnými verzemi firmware (0x0016 a 0x0006) se obsluhují automaticky.
- Více cyclerů na jednom počítači se rozliší podle sériového čísla převodníku CP2112.

Podrobnosti a zjištěné problémy jsou v [POZNATKY.md](POZNATKY.md).

## Požadavky

- Linux s ovladačem `hid_cp2112` (běžná součást jádra), Python 3 bez dalších knihoven.
- `matplotlib`, jen pro `plotlog.py`.
- Práva ke čtení a zápisu `/dev/i2c-*` a `/dev/hidraw*` převodníku, například přes pravidlo udev pro USB ID `10c4:ea90`.

## Skripty

| Skript | Účel |
|---|---|
| `hwtest.py` | test hardwaru a sdílená knihovna: blikne LED, najde gauge, zjistí jejich firmware a nahraje správnou konfiguraci, vypíše hodnoty |
| `alloff.py` | uvede cycler do bezpečného stavu: nabíječky i zátěže vypnuté |
| `chargertest.py` | ověří zapojení /CE nabíječek, s `--loads` i zátěží |
| `monitor.py` | jen čtení gauge, nabíječky a zátěže vypnuté; volitelně CSV log |
| `cycling.py` | test článků: vybití → nabití → vybití, kapacita a DC odpor do `results.csv` |
| `transport.py` | nabije nebo vybije články na transportní napětí 3,5 V naprázdno |
| `plotlog.py` | průběžný graf napětí, proudu a odporu z běžícího logu |

Všechny skripty mají nápovědu `--help`. Na jednom cycleru může běžet vždy jen jeden skript, druhý odmítne začít.

## Postup testu

1. **Kontrola hardwaru** po zapojení nebo výměně desek. Na konci má být `RESULT: all OK`.
   ```
   ./hwtest.py
   ```
2. **Bezpečný stav** před připojením zdrojů a článků:
   ```
   ./alloff.py
   ```
3. **Vložit články a spustit test.** Volitelně s označením článků:
   ```
   ./cycling.py --cells A1,A2,A3,A4
   ```
   - Každý článek se nejdřív vybije, odpočine 10 min, nabije, odpočine 30 min a nakonec se vybije znovu. Z druhého vybíjení se počítá kapacita (mAh, Wh).
   - Během obou vybíjení se každých 5 min měří DC vnitřní odpor.
   - Při startu se zkontroluje konfigurace gauge a smažou se hodnoty naučené na předchozích článcích. Volba `--same-cells` je ponechá, když se pokračuje se stejnými články.
   - Další volby: `--empty MV` (konečné napětí vybíjení, výchozí 3500), `--rest MIN`, `--rest-empty MIN`, `--rint MIN`.
4. **Sledování průběhu** v druhém terminálu (bez argumentu otevře nejnovější log):
   ```
   ./plotlog.py
   ```
5. **Výsledky:** v souboru `results.csv` je jeden řádek na článek a test. Obsahuje kapacitu, energii, DC odpor (medián, min a max), vybíjecí proud, napětí po nabití, nabitou kapacitu, maximální teplotu, cycler a stav (`OK` nebo `ABORTED in …`). Soubor se jen doplňuje, nikdy se nepřepisuje.
6. **Příprava na transport:** články se nastaví na 3,5 V naprázdno.
   ```
   ./transport.py
   ```
   Pak odpoj články, teprve potom USB. Bez USB by se nabíječky samy zapnuly.

Rychlé zobrazení stavu článků bez zásahu: `./cycling.py --status` nebo průběžně `./monitor.py`.

## Více cyclerů na jednom počítači

```
./hwtest.py --list                    # sériová čísla připojených převodníků CP2112
./hwtest.py --cycler 9BFD             # test konkrétního cycleru (blikají jeho LED TX/RX)
./cycling.py --cycler 9BFD --cells …  # stačí jednoznačný konec sériového čísla
CYCLER=9BFD ./monitor.py              # totéž přes proměnnou prostředí
./alloff.py --all                     # vypnout všechny cyclery, které právě nic nepoužívá
```

Logy mají v názvu sériové číslo (`cycling_00A59BFD_20261108_125405.csv`). Všechny cyclery zapisují do společného `results.csv` a řádky rozliší sloupec `cycler`.

## Logy

| Soubor | Obsah |
|---|---|
| `cycling_<serial>_<datum>.csv` | každé měření každého článku: stav, zátěž, U, I, T, SoC, R |
| `transport_<serial>_<datum>.csv` | průběh `transport.py` (stejné sloupce) |
| `monitor_<serial>_<datum>.csv` | log `monitor.py --log` |
| `results.csv` | souhrnné výsledky testů |
