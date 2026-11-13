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
- **Nabíjecí proud** nastavuje R19 na ISET1: I = 1000 × 1 V / (R_SNS × R19), kde R_SNS = R21 = 0,1 Ω (vzorec a konstanty podle datasheetu BQ24103). Původní R19 = 7k5 dává ≈ 1,33 A. Naměřeno bylo ≈ 2000 mAh za 2,2 h včetně fáze konstantního napětí.
- **Proud předběžného nabíjení a konce nabíjení** nastavuje R18 na ISET2 stejným vzorcem s 0,1 V místo 1 V. Původní R18 = 7k5 dává ≈ 133 mA.
- **Změna na 350 mA** (stejný proud jako vybíjení přes 10 Ω): R19 a R18 se mění na 30,1 kΩ (30k1).
  - R19 = 30k1 dává ≈ 332 mA nabíjecího proudu.
  - R18 = 30k1 dává ≈ 33 mA proudu předběžného nabíjení a konce nabíjení. S původními 133 mA by nabíjení při 350 mA skončilo příliš brzy.
  - Zatím je to provedeno pokusně jen na jedné nabíječce.
- **Bezpečnostní časovač nabíječky** nastavuje C3 na TTC: 2,6 min/nF. Původní C3 = 100 nF dává ≈ 4,3 h. Nabití článku 3500 mAh (LG MJ1) proudem 333 mA trvá ≈ 11,5–12 h z úplně vybitého stavu (samotných 3500 mAh / 333 mA je 10,5 h, k tomu dobíjení konstantním napětím), v testu po úvodním vybití na 3,5 V ≈ 9–10 h. S původním C3 by nabíječka skončila po ≈ 1500 mAh.
  - Proto se C3 mění na 470 nF, časovač pak dává ≈ 20,4 h.
  - Hlavní časový limit hlídá `cycling.py` (`CHARGE_TIMEOUT_S` = 16 h), který po vypršení vypne nabíječku daného kanálu přes /CE. Časovač nabíječky je záloha pro případ, kdy skript neběží (bez USB je /CE stažené na L a nabíječka nabíjí sama).
  - Podle datasheetu (SLUS606P) je povolený rozsah C_TTC 10–220 nF a časovač 25–572 min. **470 nF je mimo specifikaci**; v testu nabíjení 10 h prošlo, ale přesnost časovače není zaručená.
- **Časovač předběžného nabíjení je pevný: t_PRECHG = 24–36 min (typ. 30 min)**, nezávisí na C3. Když článek během něj nedosáhne V_LOWV (≈ 71,4 % z 4,2 V, tedy ≈ 3,0 V), nabíječka se vypne s chybou (proud 0). Sestupná hrana /CE (high → low) chybu i časovače resetuje.
  - Po vybití na 2,5 V se článek zotaví jen na ≈ 2,75 V a proud předběžného nabíjení 33 mA (R18 = 30k1) ho za 30 min nedostane přes 3,0 V. V testu 11.–13. 11. 2026 (cycler 00AC4D02) se tak ch0–ch2 v `CHARGE3` zastavily na 2,94–2,99 V, skript čekal na 16h timeout a test skončil až o 16 h později. Jen ch3 přešel přes 3,0 V v 28. minutě.
  - `cycling.py` proto v `CHARGE` i `CHARGE3` hlídá zastavenou nabíječku: když proud zůstane ≤ 10 mA 120 s a článek není nabitý, přepne /CE high → low a nabíječku tím znovu spustí, nejvýš 4× za jednu fázi nabíjení. Potom nabíjení ukončí (`FAILED: charger stopped`, ve `CHARGE3` `OK, final charge stopped`).

## Úpravy modulů oproti standardní výrobě

Změny, kterými se moduly v cycleru liší od standardně vyráběných MLAB modulů:

| Modul | Součástka / zapojení | Standardně | Upraveno | Důvod |
|---|---|---|---|---|
| LION1CELL01B | R19 (ISET1) | 7k5 (≈ 1,33 A) | 30k1 (≈ 332 mA) | nabíjecí proud jako vybíjecí proud |
| LION1CELL01B | R18 (ISET2) | 7k5 (≈ 133 mA) | 30k1 (≈ 33 mA) | proud předběžného nabíjení a konce nabíjení úměrný nižšímu nabíjecímu proudu |
| LION1CELL01B | C3 (TTC) | 100 nF (≈ 4,3 h) | 470 nF (≈ 20,4 h) | bezpečnostní časovač nabíjení delší než nabití 3500 mAh proudem 333 mA |

Výměna R18, R19 a C3 je zatím provedená jen pokusně na jedné nabíječce (viz [TODO.md](TODO.md)).

## Měření kapacity a vnitřního odporu

- **Kapacita** se počítá sčítáním okamžitého proudu z gauge během vybíjení ze stavu plného nabití. Výsledek souhlasil s `RemainingCapacity()` gauge na 2–3 %.
- Vybíjení do 3,5 V pod zátěží nevybije článek celý, zůstává podstatná část kapacity. Pro srovnání s katalogovou kapacitou je potřeba vybíjet níž.
- Proto se od listopadu 2026 vybíjí ve dvou úrovních:
  - Úvodní vybití (definovaný výchozí stav před nabitím) končí na 3,5 V (`--empty-pre`).
  - Vybití pro měření kapacity končí na 2,5 V (`--empty`), což je konečné napětí vybíjení podle datasheetu LG MJ1 (do 10. 11. 2026 to bylo 2,8 V). Výsledky změřené s různým konečným napětím (3,5 V, 2,8 V, 2,5 V) nejsou srovnatelné; konec vybíjení je v `results.csv` ve sloupci `empty_mV`.
- Po vybití na 2,5 V se 10 min zapisuje do logu, jak se napětí bez zátěže zotavuje (stav `REST3`). Do výpočtů se nepoužívá.
- Potom se článek dobije, dokud napětí při nabíjení nedosáhne 3,5 V (stav `CHARGE3`), a test skončí. Článek tak nezůstane hluboko vybitý a má napětí vhodné pro zápis do data flash gauge (nad ≈ 2,9 V) při dalším spuštění. Přesné transportní napětí nastaví `transport.py`.
- **DC vnitřní odpor:** během vybíjení se zátěž každých 5 min odpojí a odpor se spočte jako R = (U_off − U_on) / (I_off − I_on). Započítávají se do něj i kontakty, vodiče a bočník. Hodnoty níže jsou z prvních testů, kdy se zátěž odpojovala jen na ≈ 2 s:

  | Kanál | Odpor |
  |---|---|
  | ch0 | 119–123 mΩ |
  | ch1 | 109–113 mΩ |
  | ch2 | 100–113 mΩ |
  | ch3 | ≈ 144 mΩ |

- **Délka odpojení zátěže je 3 s** (`--rint-off`, zapisuje se do `results.csv` jako `R_pulse_s`). Po odpojení napětí nejdřív skočí o ohmickou složku a pak ještě pomalu roste, takže naměřený odpor závisí na délce pulzu: 1 s dává nejnižší hodnotu, 10 s (HPPC) nejvyšší.
  - Gauge aktualizuje hodnoty jednou za sekundu a proud průměruje přes celou sekundu. Hodnota „po 3 s“ tedy pochází z intervalu 2–3 s po odpojení.
  - Při původních ≈ 2 s kolísala skutečná doba mezi 1 a 2 s, kde se napětí mění nejrychleji. To přidávalo rozptyl a pravděpodobně to způsobilo odlehlé hodnoty 93,9 a 83,5 mΩ na ch2.
  - Napětí bez zátěže se čte v přesný čas nezávisle na 2s cyklu měření.
  - Výsledky naměřené s různou délkou pulzu nejsou srovnatelné.

## Kalibrace měření

Od 13. 11. 2026 se kalibruje stolním multimetrem **Rigol DM3058** (v. č. DM3L202700203) s kalibračním listem **OTK26269-C6331-BAC**. Korekce je uložená v `calibration.csv`. Hodnoty gauge jsou surové (nekalibrované), proud se měřil při ≈ 300–330 mA.

| Cycler | Kanál | Napětí gauge → DM3058 | Proud gauge → DM3058 | Zisk U | Zisk I |
|---|---|---|---|---|---|
| 00AC4D02 | ch0 | 3,182 → 3,1852 V | 304 → 301 mA | 1,001006 | 0,990132 |
| 00AC4D02 | ch1 | 3,126 → 3,1317 V | 305 → 301 mA | 1,001823 | 0,986885 |
| 00AC4D02 | ch2 | 3,132 → 3,1354 V | 301 → 298 mA | 1,001086 | 0,990033 |
| 00AC4D02 | ch3 | 3,403 → 3,4084 V | 324 → 318 mA | 1,001587 | 0,981481 |
| 00A59BFD | ch0 | 3,591 → 3,5946 V | 326 → 316 mA | 1,001003 | 0,969325 |
| 00A59BFD | ch1 | 3,593 → 3,5955 V | 322 → 318 mA | 1,000696 | 0,987578 |
| 00A59BFD | ch2 | 3,586 → 3,5921 V | 316 → 313 mA | 1,001701 | 0,990506 |
| 00A59BFD | ch3 | 3,590 → 3,5944 V | 327 → 320 mA | 1,001226 | 0,978593 |

- Gauge měří napětí o 0,07–0,18 % nižší než DM3058 a proud o 1,0–3,2 % vyšší.
- Kalibrace je jednobodová, offset je 0. Stejný zisk proudu se používá pro nabíjení i vybíjení.
- Rozlišení gauge je 1 mA, takže při ≈ 300 mA je nejistota zisku proudu z jednoho bodu ≈ ±0,3 %.
- Původní kalibrace prototypu 00A59BFD z 8. 11. 2026 (UNI-T UT804 bez kalibračního listu, proud 0,6–1,4 A) je v `calibration.csv` ponechaná jako komentář. Zisk proudu z ní se od nové liší až o 1,2 % (ch1). Výsledky před 13. 11. 2026 jsou tedy kalibrované jinak.

## Otevřené body

Otevřené úkoly jsou v [TODO.md](TODO.md).
