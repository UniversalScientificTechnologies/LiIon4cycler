# Zjištěné poznatky

Souhrn toho, co se ukázalo při oživování a prvních testech LiIon4cycleru (listopad 2026).
Popis zařízení a návod k použití skriptů je v [README.md](README.md).

## USB převodník CP2112 (USBI2C01)

- Linuxový ovladač `hid_cp2112` vytvoří I2C sběrnici `/dev/i2c-N`. Skripty ji používají přímo přes ioctl `I2C_RDWR`, žádné další knihovny nejsou potřeba.
- GPIO se ovládají přes `/dev/hidrawN` pomocí HID feature reportů:

  | Report | Funkce |
  |---|---|
  | `0x02` | konfigurace GPIO (směr, push-pull) |
  | `0x03` | čtení úrovní GPIO |
  | `0x04` | nastavení výstupů GPIO |

- Stav výstupů GPIO zůstává nastavený, dokud má převodník napájení z USB. Po odpojení USB nebo po resetu převodníku jsou piny vstupy.
- **Signál /CE nabíječek má pull-down (R11), takže nabíječky ve výchozím stavu nabíjejí.** Po připojení USB nebo po pádu skriptu bez úklidu se mohou samy rozběhnout. Před připojením článků nebo zdrojů proto spusť `alloff.py`. Na konci práce nejdřív odpoj články nebo napájení nabíječek, teprve potom USB.
- Každý převodník má v USB sériové číslo (v sysfs `HID_UNIQ`, např. `00A59BFD`). Podle něj se rozlišuje více cyclerů na jednom počítači.
- Dva programy na jedné sběrnici si navzájem přepínají multiplexer a čtou nesmyslné hodnoty, jednou kvůli tomu na pozadí běžel starý `cycling.py`. Skripty proto drží zámek `/tmp/liion4cycler-<serial>.lock` a na jednom cycleru může běžet jen jeden z nich.

## Spínání zátěží (PCA9531, I2CPWM01)

- Výstupy jsou open-drain. Kombinace `LSx = 01` stáhne výstup do L, `00` ho uvolní (Hi-Z) a výstup je pak v H díky pull-upu z propojek J6–J13.
- LED0–LED3 jsou indikační LED (svítí při L), LED4–LED7 spínají zátěže (**L = vypnuto, H = zapnuto**). Bezpečný stav je maska `FETS_OFF = 0xF0` (LS0/LS1 = `0x00`/`0x55`).
- Zátěže se spínají staticky, střídavé (AC-vázané) ovládání se nepoužívá kvůli chybějícímu modulu. **Nic je tedy nevypne, když program spadne.** PCA9531 si drží poslední stav, dokud se znovu nenastaví nebo dokud se modul nevypne.
- Každý kanál má jinou zátěž. Naměřené proudy:

  | Kanál | Proud zátěže |
  |---|---|
  | ch0 | ≈ 1,54 A |
  | ch3 | ≈ 1,03 A |
  | ch1, ch2 | ≈ 0,68–0,77 A |

  Kapacity naměřené na různých kanálech proto nejsou přímo srovnatelné. Kanály je potřeba sjednotit, nebo počítat s rozdílným vybíjecím proudem (sloupec `I_load_mA` v `results.csv`).

## Gauge BQ34Z100 – dvě verze firmware

Na deskách LION1CELL01B jsou dvě verze firmware se **zcela odlišným rozložením data flash**:

| | FW 0x0016 (ch0–ch2) | FW 0x0006 (ch3) |
|---|---|---|
| Datasheet | SLUSAU1C (2021) | SLUSAU1B (prosinec 2012), [kopie](https://datasheet.octopart.com/BQ34Z100PW-Texas-Instruments-datasheet-48268044.pdf) |
| Okamžitý proud | registr `Current()` 0x10 | podpříkaz `Control()` CURRENT 0x0018 (registr 0x10 vrací 0) |
| Design Capacity / Design Energy | 48/11, 48/13 | 48/21, 48/23 |
| Cell Charge Voltage | 48/17, 48/19 | 48/28, 48/30 |
| Cell Terminate Voltage | 80/53 | 80/67 |
| Qmax, Cycle Count, Update Status | 82/0, 82/2, 82/4 | stejné |
| LED_Comm, počet článků, Pack Configuration | 64/4, 64/7, 64/0 | stejné |
| Ra tabulky | 88, 89 | stejné |

- Verzi firmware vrací `Control()` 0x0002. `hwtest.py` a `cycling.py` ji vždy nejdřív zjistí a podle ní zvolí konfiguraci (`GAUGE_CONFIGS`) i registry. U neznámé verze vypíšou varování a gauge nekonfigurují.
- **Zápis konfigurace pro FW 0x0016 do gauge s FW 0x0006 poškodil data flash.** Hodnoty se zapsaly do jiných polí, mimo jiné Manufacture Date, Cycle Count, CC Threshold a nezdokumentované pole 80/53. Konfigurace `GAUGE_CONFIG_FW0006` je opravuje: obnoví výchozí hodnoty od TI a CC Threshold na 90 % Design Capacity. Původní hodnotu 0x1400 pole 80/53 jsem odvodil z uspořádání bloku.
- Data flash je trvalá paměť, takže konfigurace přežije vyjmutí článku i odpojení napájení. Ztratí se jen průběžné hodnoty v RAM.
- Zápis do flash vyžaduje napětí článku nad ≈ 2,9 V (výchozí Flash Update OK Cell Volt je 2800 mV). Po změně se gauge resetuje podpříkazem `Control()` 0x0041. Během restartu se z gauge nečte.
- `AverageCurrent()` (0x0A) je filtrovaný proud s časovou konstantou τ ≈ 15 s. Pro měření odporu se nehodí, je potřeba okamžitý proud.
- Hodnoty v data flash jsou big-endian. Pozor na bitové masky u dvoubajtových polí: bity RSNS v horním bajtu Pack Configuration mají masku `0x0700`, ne `0x0007`.

## Usínání gauge (SLEEP)

- Když průměrný proud klesne pod Sleep Current (výchozí 10 mA), gauge usne a měří jen jednou za 20 s. Po zapnutí zátěže pak hodnoty až 20 s neodpovídají skutečnosti.
- Gauge má probouzet komparátor proudu (Pack Configuration `[IWAKE RSNS1 RSNS0]`). **Na ch0, ch2 a ch3 byl vypnutý (RSNS = 00)**, výchozí stav od TI je 001 a ten měl jen ch1. Po nastavení 001 (práh 1,25 mV na bočníku) se všechny gauge probudí do ≈ 1,5 s po zapnutí zátěže.
- U FW 0x0006 funguje bit `[SLEEP]` v Pack Configuration a gauge po jeho vynulování neusíná vůbec.
- U FW 0x0016 gauge usíná i s nulovým bitem 5 a i se Sleep Current = 0, protože klidový proud je přesně 0 mA (deadband). Zůstává proto jen rychlé probouzení komparátorem.
- Sleep Current se má nechat na 10 mA. Hodnota 0 by vypnula automatickou kalibraci nuly měření proudu (CC offset).

## Naučené hodnoty (Impedance Track)

- Gauge si během provozu ukládá do flash naučená data: Qmax 82/0, Cycle Count 82/2, Update Status 82/4 a Ra tabulky v subclass 88/89 (příznak 0xFF55 nebo 0xFFFF znamená „neaktualizováno“). Patří ke konkrétnímu článku, takže je `cycling.py` při startu nastaví zpět na výchozí. Volba `--same-cells` je ponechá.
- Výchozí Ra tabulka (CHEM 0x0107): `105 100 113 143 98 97 108 89 86 85 87 90 110 647 1500`.
- **Všechny gauge mají Update Status 0x00, Impedance Track je tedy vypnutý a nic se neučí.** SoC proto vychází jen z přednastaveného Qmax. Zapnout učení by šlo podpříkazem `Control()` IT_ENABLE 0x0021. Vlastní měření kapacity a odporu na tom nezávisí.

## Nabíječka BQ24103

- Nabíječka nezačne znovu nabíjet, pokud je článek nad prahem pro dobíjení. Jednou to vedlo k falešnému stavu FULL při 4108 mV, kdy článek ve skutečnosti nebyl nabitý. Sekvence vybití → nabití → vybití tomu předchází, protože se vždy nabíjí z vybitého článku.
- ch3 v jednom běhu nabíjel jen proudem 350–430 mA, v dřívějším logu 1383 mA. Příčina (teplota, pokles napájení, hardware) zatím není ověřená.

## Měření kapacity a vnitřního odporu

- **Kapacita** se počítá sčítáním okamžitého proudu z gauge během vybíjení ze stavu plného nabití. Výsledek souhlasil s `RemainingCapacity()` gauge na 2–3 %.
- Vybíjení do 3,5 V pod zátěží nevybije článek celý, zůstává podstatná část kapacity. Pro srovnání s katalogovou kapacitou je potřeba vybíjet na 2,5–3,0 V (`--empty`).
- **DC vnitřní odpor:** během vybíjení se zátěž každých 5 min na ≈ 2 s odpojí a odpor se spočte jako R = (U_off − U_on) / (I_off − I_on). Jde o krátkodobý odpor (DCIR za 1–2 s), do kterého se započítávají i kontakty, vodiče a bočník. Naměřené hodnoty:

  | Kanál | Odpor |
  |---|---|
  | ch0 | 119–123 mΩ |
  | ch1 | 109–113 mΩ |
  | ch2 | 100–113 mΩ |
  | ch3 | ≈ 144 mΩ |

## Otevřené body

- Ověřit nízký nabíjecí proud na ch3.
- Sjednotit zátěže na všech kanálech, nebo se rozhodnout, jakým proudem se má kapacita měřit.
- Rozhodnout o konečném napětí vybíjení (výchozí 3500 mV pod zátěží).
- Rozhodnout, jestli zapnout Impedance Track v gauge.
- Čtení GPIO reportem `0x03` v `alloff.py` je ověřené jen na jednom převodníku.
