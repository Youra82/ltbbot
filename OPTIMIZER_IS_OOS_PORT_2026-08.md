# ltbbot: IS/OOS-Split + K-Fold-Robustheit im Optimizer (2026-08-21)

Port der stbot-Loesung vom selben Tag (`stbot/src/stbot/analysis/optimizer.py`) nach
`src/ltbbot/analysis/optimizer.py`. Hintergrund: `LIVE_TRADE_FORENSIK_2026-08.md` zeigte, dass
der volle 3-Jahres-Backtest (2023-09 bis 2026-08) das Deaktivieren des ADX-`STRONG_TREND`-Blocks
klar als Verbesserung ausweist (+321pp Summe-PnL ueber alle 6 Configs), aber auf den juengsten
3 Monaten (21.05.-21.08.2026) allein ist dieselbe Aenderung netto schlechter (-11.3pp) --
klassisches In-Sample-Overfitting-Symptom, weil `optimizer.py` bis dahin komplett ohne
Out-of-Sample-Split arbeitete.

## Was am optimizer.py geaendert wurde

Datei: `src/ltbbot/analysis/optimizer.py` (Port von `stbot/src/stbot/analysis/optimizer.py`,
Muster identisch, Parameter-Namen an ltbbots Envelope-Strategie angepasst).

- **Neue Globals** (Zeile ~34-51): `IS_DATA`/`OOS_DATA` (chronologischer Split, `IS_FRACTION`
  Standard 0.70), `MIN_OOS_TRADES` (Standard 10), `K_FOLDS` (Standard 3). `HISTORICAL_DATA` bleibt
  bestehen (voller Datensatz vor dem Split).
- **`objective()`** (Zeile ~57-159):
  - Backtest waehrend der Suche laeuft jetzt auf `IS_DATA` statt `HISTORICAL_DATA` (Zeile 115).
  - `fine_data=None` waehrend jedes Trials (Zeile 115, 154) statt `fine_data=FINE_DATA`
    (LazyFineData) bei jedem Trial -- das war derselbe Performance-Fehler, den stbot hatte:
    LazyFineData holt pro Trial Tag-fuer-Tag einzeln vom Netzwerk (gemessen >100s/Trial), reines
    `fine_data=None` (grobe Naeherung) braucht Bruchteile einer Sekunde. Bei 200-600 Trials x
    6 Symbolen ist das der Unterschied zwischen Stunden und Minuten.
  - Pruning-Kriterien (Drawdown/WinRate/PnL/Mindest-Trades) bleiben auf dem VOLLEN IS-Fenster.
  - Neu: K-Fold-Robustheits-Score (Zeile ~140-159) -- `IS_DATA` wird in `K_FOLDS` (Standard 3)
    aufeinanderfolgende Teilfenster gesplittet, jedes einzeln backtestet, das Optuna-Zielergebnis
    ist das **Minimum** ueber alle Teilfenster statt der Gesamt-IS-PnL. Das bestraft Parameter, die
    nur in einer einzelnen Marktphase zufaellig gut abschneiden, schon waehrend der Suche.
- **`main()`** (Zeile ~163-521):
  - Neue CLI-Args `--is_fraction` (Default 0.70), `--min_oos_trades` (Default 10), `--k_folds`
    (Default 3) -- alle mit Defaults, `run_pipeline.sh`/der Scheduler brechen dadurch nicht.
  - Chronologischer Split `HISTORICAL_DATA` -> `IS_DATA`/`OOS_DATA` direkt nach dem Laden
    (Zeile ~244-255), inkl. Log-Zeile mit Split-Datum.
  - `MIN_TRADES_FOR_VALID` wird jetzt auf Basis der IS-Fensterlaenge skaliert (vorher: volle
    Historie) -- konsistent mit `objective()`, das nur IS_DATA sieht.
  - Praezise Nachbewertung des besten Trials (Zeile ~355-375): EIN Bulk-Fetch der feinen Zeitreihe
    (`load_data(symbol, fine_tf, start_date, end_date)`) statt `LazyFineData` -- LazyFineData waere
    fuer eine durchgehende Nachbewertung ueber Jahre extrem langsam (stbot-Messung: >12 Min fuer ein
    3-Jahres-Fenster). Mit dieser Fein-Aufloesung werden `best_is`/`best_oos` UND (falls vorhanden)
    `baseline_is`/`baseline_oos` (bestehende Config, auf denselben Daten) berechnet.
  - **Neue Speicherlogik** (Zeile ~432-497): Bestaetigung (`confirmed`) erfordert
    `OOS-Trades >= MIN_OOS_TRADES` UND `OOS-PnL > 0` UND (falls Baseline vorhanden)
    `OOS-PnL(neu) > OOS-PnL(Baseline)` -- alle drei auf `OOS_DATA`, die nie in die Suche eingeflossen
    ist. Config wird **nur bei Bestaetigung ueberschrieben** (bewusst NICHT das stbot-Verhalten
    "immer speichern, nur markieren" -- der User wollte explizit die alte "nur speichern wenn
    besser"-Semantik von ltbbot beibehalten, nur jetzt OOS- statt Gesamt-Backtest-basiert).
  - `_meta` um `oos_pnl_pct`, `oos_trades`, `is_oos_split_date`, `is_fraction`, `k_folds`,
    `confirmed`, `optimized_at` erweitert; `pnl_pct` bleibt erhalten (jetzt IS-PnL statt
    Voll-Historie-PnL) fuer Rueckwaertskompatibilitaet mit Code, der dieses Feld liest.
  - `bool(...)`-Wrapper um den `confirmed`-Ausdruck (numpy.bool_ aus pandas-basierten Backtest-
    Ergebnissen ist sonst nicht JSON-serialisierbar -- identischer Bug/Fix wie in stbot 2026-08-21).

Zusaetzlich angepasst, um die neuen Args durchzureichen:
- `run_pipeline.sh` -- neue Prompts fuer `IS_FRACTION`/`K_FOLDS`/`MIN_OOS_TRADES` (Defaults aus
  `settings.json`, fallback 0.70/3/10), an den Optimizer-Aufruf angehaengt.
- `settings.json` -- `optimization_settings.is_fraction`/`k_folds`/`min_oos_trades` als
  dokumentierte Defaults ergaenzt (nur Metadaten, kein Verhalten geaendert -- `auto_optimizer_scheduler.py`
  ruft `optimizer.py` nicht direkt auf, nur `show_results.py --mode 3`, ist also unbetroffen).

Getestet: `pytest tests/` -- 2 passed, 3 skipped, 1 bekannter/unabhaengiger Fehlschlag
(`test_place_entry_orders_on_bitget`, dokumentiert in `LIVE_TRADE_FORENSIK_2026-08.md`, betrifft
Live-Order-Platzierung und nicht den Optimizer).

## Trial-Budget: warum 200 nicht reichte und 600 verwendet wurde

Erster Lauf mit `--trials 200` (Vorgabe aus der Aufgabenstellung/`settings.json`-Standard) auf
allen 6 Symbolen: **kein einziger Config wurde aktualisiert.** Drei Symbole (ADA, ETH, LTC) fanden
in 200 Trials NICHT EINEN gueltigen (nicht gepruneten) Trial -- der IS-only + K-Fold-Suchraum ist
mit den `strict`-Constraints (MaxDD<=30%, PnL>=0, Mindest-Trades proportional zur IS-Fensterlaenge)
straffer als die alte Voll-Historie-Suche. Die anderen drei (AAVE, BNB, XRP) fanden Kandidaten, aber
keiner bestand die OOS-Bestaetigung.

Da die Laufzeit pro Symbol unkritisch war (Download + 200 Trials + Praezisions-Nachbewertung ca.
4 Minuten im Schnitt, dominiert von Netzwerk-Downloads nicht von der Trial-Rechenzeit selbst --
~1.5-2 Trials/Sek. bei `--jobs -1` auf 16 Kernen, jeder Trial macht 4 Backtests: 1x IS-Vollfenster +
3x K-Fold-Teilfenster), wurde ein zweiter Lauf mit **`--trials 600`** gestartet (3x Budget, geschaetzt
~10-15 Min Mehraufwand/Symbol, gesamt gut vertretbar).

## Ergebnisse pro Symbol

Alle Zahlen aus dem finalen `--trials 600`-Lauf (2023-09-01..2026-08-21, `--start_capital 50`,
`--is_fraction 0.70 --k_folds 3 --min_oos_trades 10`, `--jobs -1`). IS-Fenster: 2023-09-01 bis
2025-09-29 (~3035 Kerzen), OOS-Fenster: 2025-09-29 bis 2026-08-21 (~1302 Kerzen), fuer alle
6 Symbole identisch. "Baseline" = bestehende Config (Fit vom 2026-06-29, siehe Abschnitt oben),
auf denselben IS/OOS-Daten neu ausgewertet.

Laufzeit: ~13-15 Min je Symbol bei 600 Trials (~1-1.5 Trials/Sek. effektiv bei `--jobs -1` auf
16 Kernen, jeder Trial macht bis zu 4 Backtests: IS-Vollfenster + 3 K-Fold-Teilfenster; ein
Zwischenlauf mit 200 Trials brauchte ~4 Min/Symbol, siehe Abschnitt "Trial-Budget" oben).

### AAVE/USDT:USDT (6h) — nicht bestaetigt, Config unveraendert

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 248 | 73 | 68 | 16 |
| WinRate | 35.1% | 52.0% | 39.7% | 62.5% |
| PnL % | +303.5% | +105.4% | +99.0% | +32.0% |
| MaxDD % | 20.8% | 5.2% | 13.5% | 2.9% |

Grund: OOS-PnL (+32.0%) positiv und genug Trades (16 >= 10), aber schlechter als Baseline-OOS
(+99.0%) -> Bedingung "besser als Baseline" nicht erfuellt.

### ADA/USDT:USDT (6h) — nicht bestaetigt, Config unveraendert

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 221 | 108 | 83 | 37 |
| WinRate | 47.5% | 49.1% | 42.2% | 54.0% |
| PnL % | +343.5% | +190.3% | +82.6% | +56.1% |
| MaxDD % | 5.3% | 4.1% | 7.5% | 4.6% |

Grund: OOS-PnL (+56.1%) positiv, aber schlechter als Baseline-OOS (+82.6%).

### BNB/USDT:USDT (6h) — **BESTAETIGT, Config aktualisiert**

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 110 | 49 | 62 | 27 |
| WinRate | 20.9% | 55.1% | 12.9% | 44.4% |
| PnL % | -8.2% | +50.9% | -14.8% | +16.3% |
| MaxDD % | 10.5% | 2.6% | 15.8% | 4.6% |

Neue Parameter (Auszug): `average_type=WMA, average_period=6, envelopes=[0.0245, 0.0324, 0.0389],
leverage=6, risk_per_entry_pct=0.47, sl_to_env1_ratio=0.1048, disable_strong_trend_block=False,
strong_trend_adx_threshold=28.31`. Interessant: Optuna waehlte fuer BNB ein ENGERES ADX-Gate
(28.31 statt 30, Block NICHT deaktiviert) statt es zu lockern -- deckt sich exakt mit dem
`LIVE_TRADE_FORENSIK_2026-08.md`-Befund, dass BNB die einzige Config ist, die von einem
strengeren statt lockereren Regime-Gate profitiert.

### ETH/USDT:USDT (6h) — nicht bestaetigt, Config unveraendert

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 138 | 50 | 53 | 16 |
| WinRate | 49.3% | 42.0% | 39.6% | 31.2% |
| PnL % | +192.1% | +29.4% | +29.0% | +2.4% |
| MaxDD % | 14.7% | 5.4% | 11.9% | 6.0% |

Grund: OOS-PnL knapp positiv (+2.4%), aber deutlich schlechter als Baseline-OOS (+29.0%).

### LTC/USDT:USDT (6h) — nicht bestaetigt, Config unveraendert

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 96 | 71 | 31 | 25 |
| WinRate | 46.9% | 45.1% | 58.1% | 48.0% |
| PnL % | +105.8% | +17.0% | +70.9% | +9.2% |
| MaxDD % | 8.9% | 2.4% | 5.0% | 1.7% |

Grund: OOS-PnL positiv (+9.2%), aber deutlich schlechter als Baseline-OOS (+70.9%).

### XRP/USDT:USDT (6h) — **BESTAETIGT, Config aktualisiert**

| Metrik | Baseline IS | Best IS | Baseline OOS | Best OOS |
|---|---|---|---|---|
| Trades | 154 | 182 | 48 | 58 |
| WinRate | 57.1% | 52.8% | 45.8% | 50.0% |
| PnL % | +302.8% | +281.5% | +73.2% | +82.2% |
| MaxDD % | 9.6% | 8.2% | 10.1% | 10.4% |

Neue Parameter (Auszug): `average_type=WMA, average_period=5, envelopes=[0.0220, 0.0277, 0.1323],
leverage=15, risk_per_entry_pct=0.97, sl_to_env1_ratio=0.1067, disable_strong_trend_block=True`.
OOS-PnL (+82.2%) schlaegt die Baseline-OOS (+73.2%) leicht, bei etwas hoeherem MaxDD (10.4% vs.
10.1%, beide innerhalb des 30%-Limits) -- eine echte, wenn auch moderate Verbesserung.

### Zusammenfassung

| Symbol | Bestaetigt? | Baseline OOS-PnL | Neu OOS-PnL | Config geaendert? |
|---|---|---|---|---|
| AAVE | Nein | +99.0% | +32.0% | Nein |
| ADA | Nein | +82.6% | +56.1% | Nein |
| BNB | **Ja** | -14.8% | **+16.3%** | **Ja** |
| ETH | Nein | +29.0% | +2.4% | Nein |
| LTC | Nein | +70.9% | +9.2% | Nein |
| XRP | **Ja** | +73.2% | **+82.2%** | **Ja** |

2 von 6 Configs aktualisiert (BNB, XRP), 4 unveraendert (AAVE, ADA, ETH, LTC) -- jeweils mit
dokumentierter OOS-Begruendung. Bemerkenswert: bei 4 der 6 Symbole ist die Baseline-OOS-PnL trotz
kleinerem, IS-only gefitteten neuen Suchraum weiterhin klar besser -- konsistent mit der
Nachpruefung im Abschnitt "Wichtiger methodischer Punkt" oben (die Baselines generalisieren
offenbar echt gut, kein Leakage-Artefakt).

## Wichtiger methodischer Punkt: Warum "nicht bestaetigt" bei starken Baselines kein Fehlschlag der neuen Methodik ist

**Der Verdacht (zu Recht aufgeworfen):** Die BESTEHENDEN Baseline-Configs wurden mit dem ALTEN
Optimizer (vor diesem Port, ohne eingebauten IS/OOS-Split) gefittet. Falls dieser alte Fit den
kompletten Zeitraum inkl. des Bereichs gesehen hat, den der NEUE Optimizer jetzt als OOS reserviert,
waere der Vergleich strukturell unfair zugunsten der alten Baseline (Data Leakage) -- das wuerde
erklaeren, warum z.B. AAVE (starke Baseline) nicht bestaetigt wurde (99.0% vs. 44.3-32.0% OOS beim
neuen Kandidaten), waehrend ein schwacher Baseline wie BNB trotz dieses Nachteils fast geschlagen
wurde.

**Nachpruefung via Git-Historie (`git log`/`git show` auf die Config-Dateien):**

Alle 6 aktuell aktiven Baseline-Configs stammen aus zwei Commits vom 2026-06-29
(`1fc41f6` 20:25 fuer AAVE, `25bb0c1` 22:42 fuer ADA/BNB/ETH/LTC/XRP). `run_pipeline.sh` hatte zu
diesem Zeitpunkt bereits sein eigenes 70/30-Walk-Forward-Feature (seit Commit `548bc9e feat: 70/30
OOS-Split wie zerobot`, deutlich aelter). `settings.json` zeigt `oos_reference_date` in BEIDEN
Commits aktiv auf das jeweilige Lauf-Datum selbst gesetzt (im AAVE-Commit sogar explizit von
`"2026-06-28"` auf `"2026-06-29"` aktualisiert) -- das Feld wird vom Skript nur dann NICHT auf
`null` zurueckgesetzt, wenn am interaktiven Prompt tatsaechlich ein Datum eingegeben wurde. Das ist
ein starkes (wenn auch nicht 100% wasserdichtes, da der exakte interaktive Prompt-Input selbst nicht
geloggt ist) Indiz, dass der Walk-Forward-Split beim Erzeugen dieser Configs AKTIV war.

Mit `oos_reference_date=2026-06-29` und dem 6h-Standard-Lookback (1095 Tage) ergibt SICH
rechnerisch (`run_pipeline.sh`-Formel, 30% von 1095 Tagen = 328 Tage OOS):

| | Altes Pipeline-Training (falls Walk-Forward aktiv) | Neuer IS/OOS-Split (dieser Port) |
|---|---|---|
| Trainings-/IS-Fenster | 2023-06-30 .. **2025-08-04** | 2023-09-01 .. **2025-09-29** |
| OOS-/reserviertes Fenster | 2025-08-05 .. 2026-06-29 (328d, NIE gefittet, aber auch nie automatisch als Gate geprueft -- die alte `optimizer.py` hatte schlicht keine Bestaetigungslogik) | **2025-09-29 .. 2026-08-21** (1302 Kerzen) |

**Befund: Es besteht KEINE direkte Datenueberlappung.** Das alte Trainingsfenster endet
~2025-08-04, das neue OOS-Fenster beginnt erst ~2025-09-29 -- eine Luecke von **56 Tagen**. Die
alten Baseline-Parameter wurden also nachweislich NICHT auf denselben Kerzen gefittet, die jetzt als
OOS getestet werden. Klassisches "gleiche Kerzen in Training und Test"-Leakage scheidet damit als
Erklaerung aus (unter der Annahme, dass der Walk-Forward-Split beim Original-Lauf tatsaechlich aktiv
war -- was die Settings-Evidenz nahelegt, aber nicht zweifelsfrei beweist, da der genaue CLI-Aufruf
selbst nicht geloggt wurde).

**Was die Daten trotzdem zeigen:** Das alte "reservierte" Fenster (2025-08-05..2026-06-29) UEBERLAPPT
zu 273 von 327 Tagen mit dem NEUEN OOS-Fenster (2025-09-29..2026-06-29 liegt in beiden). D.h. selbst
wenn niemand die alten Parameter je gegen dieses reservierte Fenster geprueft hat (die alte
`optimizer.py` hatte dafuer schlicht keinen automatischen Mechanismus -- genau die Luecke, die dieser
Port jetzt schliesst), deckt sich ein grosser Teil des heutigen OOS-Zeitraums mit einem Zeitraum, der
UNMITTELBAR auf das alte Trainingsende folgt und mit ihm stark autokorreliert sein kann (Marktregime-
Persistenz ueber die Trainings-/Testgrenze hinweg ist ein bekanntes, subtileres Verwandtes von echtem
Leakage, aber nicht dasselbe Phaenomen).

**Einordnung fuer die beobachteten Skip-Faelle (AAVE/XRP, starke Baseline-OOS-PnL):** Die belastbarste
Lesart ist NICHT klassisches Leakage, sondern: (a) diese Parameter generalisieren tatsaechlich gut
ueber mehrere Marktphasen (das ist genau das, was ein guter Fit auf 2023-06..2025-08 leisten sollte),
und/oder (b) das angrenzende Marktregime 2025-08..2026-06 aehnelt statistisch dem Trainingsfenster
genug, dass die Generalisierung nicht ueberrascht. Fuer BNB (schwache Baseline, ohne den
`disable_strong_trend_block`-Fix) ist das Bild konsistent mit der urspruenglichen Forensik: die
Baseline war strukturell schwach (nicht profitabel), und der neue IS-only-Kandidat verbessert sie
im finalen 600-Trial-Lauf sogar ueber die Profitabilitaetsschwelle (-14.8% -> **+16.3%** OOS,
siehe "BNB im Detail" unten) -- hier hat die neue Methodik tatsaechlich eine reale Verbesserung
gefunden, kein Leakage-Artefakt der Baseline im Weg gestanden.

**Praktische Konsequenz (unveraendert von der urspruenglichen Aufgabenstellung):** Das Problem
betrifft ausschliesslich den EINMALIGEN Uebergang von Legacy-Configs (ohne eingebauten Split gefittet)
zu erstmals IS/OOS-bestaetigten Configs -- und selbst dieser Uebergang ist nach der Nachpruefung
wahrscheinlich NICHT durch Leakage verzerrt, sondern zeigt echte (wenn auch teils zufaellig gute)
Generalisierung der alten Fits. Sobald eine Config einmal ehrlich durch DIESEN Port bestaetigt und
uebernommen wurde, sind alle kuenftigen Re-Optimierungslaeufe symmetrisch fair: Baseline UND
Kandidat werden beide ausschliesslich mit `IS_DATA` gefittet bzw. auf denselben `IS_DATA`/`OOS_DATA`
verglichen (Zeile ~416-430 in `optimizer.py`). Die Bestaetigungslogik selbst wurde dafuer NICHT
veraendert (wie vom User gewuenscht) -- diese Analyse ist rein dokumentarisch.

## BNB im Detail

BNB war laut Aufgabenstellung die mit Abstand schwaechste Config: `_meta.pnl_pct` nur 7.86 (Fit
vom 2026-06-29, nie aktualisiert -- die einzige der 6 Configs, die von den 5 manuellen
`disable_strong_trend_block`-Fixes aus der Forensik-Session ausgenommen war), WinRate in den
letzten 3 Monaten nur 7.4%.

**Auf denselben IS/OOS-Daten neu ausgewertet zeigt die alte Baseline:** IS PnL -8.2% (WR 20.9%,
110 Trades), OOS PnL -14.8% (WR 12.9%, 62 Trades) -- durchgehend unprofitabel, IS und OOS
konsistent. Das bestaetigt: BNB war strukturell schwach, nicht nur "zufaellig schlecht in den
letzten 3 Monaten".

**Der neue, IS/OOS-bestaetigte Kandidat:** IS PnL +50.9% (WR 55.1%, 49 Trades), OOS PnL +16.3%
(WR 44.4%, 27 Trades) -- klar profitabel auf BEIDEN Fenstern, davon OOS nie in die Suche
eingeflossen. 27 OOS-Trades sind komfortabel ueber der `--min_oos_trades 10`-Schwelle, also eine
statistisch einigermassen belastbare Bestaetigung (wenn auch bei Weitem nicht so viele Trades wie
z.B. ADA/XRP).

**Wichtige Erkenntnis zu den gewaehlten Parametern:** Optuna waehlte fuer BNB
`disable_strong_trend_block=False` mit `strong_trend_adx_threshold=28.31` -- also ein ETWAS
ENGERES Regime-Gate als der alte Default (30.0), nicht ein deaktiviertes. Das ist die exakte
Bestaetigung des `LIVE_TRADE_FORENSIK_2026-08.md`-Befunds "BNB ist die einzige Config, die von
einem strengeren statt lockereren Gate profitiert" -- diesmal nicht aus einem groben 4-Punkte-
Sweep (ADX>25/30/35/40), sondern aus einer vollen, IS/OOS-bestaetigten Parameter-Suche. Zusaetzlich
faellt `leverage` von einem unbekannten alten Wert auf 6 (vs. 13-15 bei den anderen bestaetigten/
Baseline-Configs) und `risk_per_entry_pct` auf 0.47% -- insgesamt eine deutlich defensivere
Parametrisierung, passend zu einem Symbol, das bisher strukturell verlustreich war.

**Fazit BNB:** Ja, die neue Config ist eine echte, OOS-bestaetigte Verbesserung -- von
durchgehend unprofitabel (IS -8.2% / OOS -14.8%) zu durchgehend profitabel (IS +50.9% / OOS
+16.3%), mit genug OOS-Trades (27) fuer eine einigermassen belastbare Aussage. Empfehlung:
`disable_strong_trend_block` bei BNB bewusst NICHT manuell auf `true` setzen (wie es bei den
anderen 5 Symbolen in der vorherigen Forensik-Session geschah) -- die IS/OOS-bestaetigte Suche hat
hier bereits die richtige (gegenteilige) Antwort automatisch gefunden.

## Fazit

1. **Der IS/OOS + K-Fold-Port funktioniert wie vorgesehen.** Alle 6 Symbole wurden gegen echte,
   nie gesehene Out-of-Sample-Daten geprueft; 2 von 6 (BNB, XRP) verbesserten sich dabei
   nachweisbar, 4 von 6 (AAVE, ADA, ETH, LTC) wurden zu Recht NICHT ueberschrieben, weil ihre
   bestehenden Baseline-Configs auf denselben OOS-Daten weiterhin klar besser abschneiden.
2. **Das behebt die im Auftrag beschriebene 3-Monats-Schwaeche NUR indirekt, nicht direkt.** Die
   urspruengliche Beobachtung war: "ADX-Gate global deaktivieren sieht im 3-Jahres-Backtest gut
   aus, ist aber in den juengsten 3 Monaten schlechter." Dieser Port loest das strukturelle
   Problem (kein OOS-Check vorhanden) fuer ALLE kuenftigen Parameter-Entscheidungen, nicht nur fuer
   das ADX-Gate -- aber er aendert nicht rueckwirkend die 5 Configs, die in der Forensik-Session
   bereits manuell auf `disable_strong_trend_block=true` gesetzt wurden (AAVE, ADA, ETH, LTC, XRP).
   Bei 4 dieser 5 (AAVE, ADA, ETH, LTC) bestaetigt die neue OOS-Pruefung indirekt, dass die
   bestehenden (Gate-AUS-)Parameter weiterhin die bessere Wahl sind -- inklusive auf dem echten
   OOS-Fenster. Nur bei XRP wurde die komplette Config (inkl. weiterhin Gate AUS) durch eine
   OOS-bessere ersetzt. BNB, das NICHT manuell auf Gate-AUS gesetzt wurde, bekam durch diesen Lauf
   jetzt eine echte, eigenstaendig gefundene (und diesmal Gate-ENGER-statt-AUS) Config.
3. **Ab jetzt ist jede kuenftige Re-Optimierung symmetrisch fair.** Der einmalige Uebergang von
   Legacy-Configs (ohne eingebauten Split gefittet) war laut Git-Rekonstruktion aller
   Wahrscheinlichkeit nach NICHT durch Daten-Leckage verzerrt (siehe methodischer Abschnitt oben) --
   die 4 unveraenderten Baselines gewinnen also mutmasslich durch echte Generalisierung, nicht
   durch einen unfairen Datenvorteil. Jeder naechste `run_pipeline.sh`-Lauf vergleicht ab jetzt
   IS-only-Kandidat gegen IS-only-Baseline auf identischen IS/OOS-Fenstern.
4. **Trial-Budget:** `--trials 200` (urspruengliche Vorgabe) reichte in der Praxis nicht --
   3 von 6 Symbolen fanden dabei ueberhaupt keinen gueltigen Trial. `--trials 600` behob das fuer
   alle 6. Fuer den naechsten reinen Scheduler-/Automatik-Lauf (`settings.json.optimization_settings.num_trials`,
   aktuell 500) empfiehlt sich daher mindestens 500-600 Trials, nicht 200, wenn `is_fraction=0.70`
   aktiv bleibt (kleineres IS-Fenster als die alte Voll-Historie-Suche macht den Suchraum pro
   gegebenem Trial-Budget effektiv "schwerer").
5. **Operative Notiz:** Waehrend dieses Laufs wurde ein Hintergrundprozess unerwartet beendet
   (vermutlich durch eine Session-/Sandbox-Nebenwirkung meines eigenen langen blockierenden
   Wartebefehls, nicht durch einen Fehler im Optimizer selbst) -- betraf nur LTC/XRP im zweiten
   Durchlauf, wurde durch einen gezielten Nachlauf fuer genau diese 2 Symbole vollstaendig
   nachgeholt, ohne die bereits berechneten Ergebnisse fuer AAVE/ADA/BNB/ETH zu verwerfen oder
   erneut zu rechnen.
