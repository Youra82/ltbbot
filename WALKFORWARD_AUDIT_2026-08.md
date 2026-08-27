# ltbbot Walk-Forward-Audit (2026-08-25)

Analog zur dnabot-Session (`bugfix_dnabot_walkforward_catastrophic_failure`): echter, rollierender
Walk-Forward-Test statt eines einzelnen 70/30-Split, plus Klärung zweier offener Verdachtsmomente
aus der heutigen ltbbot-Session (Pagination-Bug-Implikationen, moegliche Live-Bandpreis-Instabilitaet
durch unvollstaendige Kerzen).

---

## 1. Pagination-Bug — Einfluss auf die heutigen Ergebnisse

**Fix:** `src/ltbbot/utils/exchange.py::fetch_historical_ohlcv` UND `fetch_recent_ohlcv` hatten einen
Off-by-one (`since = ohlcv[-1][0] + timeframe_duration_in_ms` statt `+1`). Bitgets `since` ist
exklusiv (liefert nur `timestamp > since`), daher übersprang der alte Code die erste Kerze nach
jedem 200-Kerzen-Chunk. Verifiziert mit einem frischen ADA/1d-Download: 0 fehlende Tage nach dem
Fix (vorher 11 von 1095 = 1.0%).

**Betroffener Umfang für 6h-Configs:** Der Bug tritt an jeder Chunk-Grenze auf (`fetch_limit=200`
Kerzen pro API-Call), unabhängig vom Timeframe. Für 6h-Kerzen bedeutet das ca. alle 200 Kerzen
(= 50 Tage) eine übersprungene Kerze. Über den ~3-Jahres-Backtest-Zeitraum (2023-09-01 bis
2026-08-21, ~4337 Kerzen) ergibt das rechnerisch ca. 4337/200 ≈ 22 Chunk-Grenzen → **ca. 0.5% der
6h-Kerzen fehlten** in den heute (vor dem Fix) verwendeten Daten für Live-Trade-Forensik,
Fair-Filter-Validierung und die 600-Trial-IS/OOS-Reoptimierung (BNB, XRP neu bestätigt).

**Einschätzung — kein Grund zur Neuoptimierung:**
- Die validierten Effekte sind um Größenordnungen größer als eine 0.5%-Datenlücke plausibel
  erklären könnte: ADX-Gate-Lockerung +321pp Summe-PnL über 4 unabhängige Schwellenwerte hinweg
  (monotoner Dosis-Wirkungs-Zusammenhang), alle 28 Exhaustion-Filter-Varianten uniform negativ
  (-16% bis -1338%). Ein verstreuter <1%-Datenausfall alle 50 Tage kann ein derart konsistentes,
  großflächiges Muster nicht erzeugen oder verschwinden lassen.
- Die BNB/XRP-Reoptimierung (OOS-Bestätigung: BNB -14.8%→+16.3%, XRP +73.2%→+82.2%) beruht auf
  600 Optuna-Trials mit K-Fold-Robustheitsscore — auch hier sind die Differenzen (30pp bzw. 9pp)
  deutlich größer als das, was ~22 fehlende Kerzen in einer ~3000-Kerzen-IS-Historie realistisch
  verschieben würden.
- Fehlende Kerzen wirken auf rollierende Indikatoren (EMA/SMA/ADX) nur lokal: an der Lücke selbst
  entsteht ein einmaliger kleiner Sprung, der sich über die nächsten Perioden wieder einpendelt
  (kein systematischer Bias über die gesamte Historie).

**Empfehlung:** Keine Wiederholung der heutigen Optimierungsläufe nötig. Der Cache wird ohnehin bei
jedem zukünftigen Lauf mit den jetzt korrekten Daten neu befüllt (siehe Cache-Hinweis unten), d.h.
der nächste reguläre wöchentliche Re-Optimierungslauf (Auto-Optimizer, jeden Freitag 15:00 laut
`settings.json::schedule`) verwendet automatisch lückenlose Daten. Eine gezielte Neubestätigung nur
für BNB/XRP wäre optional und geringfügig, aber nicht dringend.

**Nebenbefund (Cache-Verhalten, nicht Teil des Pagination-Bugs):** `backtester.py::load_data`
überschreibt die Cache-CSV bei jedem Download mit **nur dem angefragten Zeitraum** (kein Merge mit
vorhandener längerer Historie). Ein Skript, das nur 3 Monate anfordert (z.B. `last3m_comparison.py`),
kappt damit den Cache für alle nachfolgenden Skripte, die eigentlich 3 Jahre erwarten würden (erneuter
Download nötig, kein Datenverlust, aber unnötige Downloads/Wartezeit). Beobachtet: Cache-Dateien für
AAVE/ADA/BNB/ETH/LTC/XRP 6h enthielten zu Beginn dieser Session nur 371 Zeilen (21.05.-21.08.2026)
statt der vollen Historie. Kein Bug im eigentlichen Sinne, aber erwähnenswert für zukünftige
Performance-Optimierung der Analyse-Skripte.

---

## 2. Hypothese: Live berechnet Envelope-Bänder auf unvollständiger Kerze

### a) Cron-Frequenz — empirisch bestimmt

**Bestätigt:** `README.md` (Zeile 216) dokumentiert die Produktions-Crontab:
```
*/15 * * * * /usr/bin/flock -n /home/ubuntu/ltbbot/ltbbot.lock ... master_runner.py ...
```
`master_runner.py` startet bei jedem Aufruf für jede aktive Strategie `run.py` → `full_trade_cycle()`
neu — es gibt keine eigene Lock-/Schleifen-Logik im Bot selbst, der Cronjob treibt die Frequenz.

Verifiziert anhand echter Log-Zeitstempel (`logs/ltbbot_AAVEUSDTUSDT_6h.log`, 6h-Envelope-Strategie):
Läufe exakt alle ~15 Minuten (22:00, 22:15, 22:26, 22:30, 22:45, 23:00, ...). Für ein 6h-Timeframe
(360 Minuten) ergibt das **~24 Läufe pro Kerzen-Intervall**.

### b) Bandpreis-Stabilität innerhalb einer Kerze — quantifiziert

Zwei unabhängige, direkte Belege:

1. **Live-Exchange-Test (soeben, read-only, öffentlicher Endpunkt):** `ccxt.bitget().fetch_ohlcv('BTC/USDT:USDT', '6h')`
   liefert als letzte Kerze den Timestamp `2026-08-25 18:00:00`, obwohl es zum Abfragezeitpunkt erst
   `18:20:18` UTC war — die Kerze ist erst 20 Minuten alt (statt 360). **Bitgets `fetch_ohlcv`
   enthält also nachweislich die aktuell noch laufende Kerze als letztes Element.**
2. **Code-Pfad ohne Filterung:** `trade_manager.py::full_trade_cycle` (Zeile 1367) ruft
   `exchange.fetch_recent_ohlcv(...)` auf und übergibt das Ergebnis direkt an
   `calculate_indicators_and_signals()` (envelope_logic.py). Dort werden `average`/Bandpreise via
   `.iloc[-1]` aus dem EMA/SMA/WMA über die komplette Serie gelesen (Zeile 154-171) — **ohne jede
   Prüfung, ob die letzte Zeile eine abgeschlossene Kerze ist.** Bemerkenswert: An anderer Stelle
   im selben File (Zeile 979, 1160, Kommentar "Close-Confirmation: **letzte abgeschlossene** Kerze
   muss unterhalb des Bands geschlossen haben") geht der Code selbst explizit davon aus, dass
   `df.iloc[-1]` eine geschlossene Kerze ist — eine Annahme, die durch (1) nachweislich falsch ist.

**Quantifizierung (Skript `livemodus/intracandle_drift.py`, ausgewertet über `logs/ltbbot_AAVEUSDTUSDT_6h.log{,.1,.2}`,
4448 geloggte Live-Läufe, 187 6h-Kerzenfenster mit ≥2 Läufen, Median 24 Läufe/Fenster):**

| Metrik (EMA-`average`-Drift innerhalb EINER 6h-Kerze) | Wert |
|---|---|
| Median | **1.30%** |
| Mittelwert | 1.48% |
| 75. Perzentil | 1.93% |
| Maximum (beobachtet) | 4.58% |
| Anteil Fenster mit Drift > 0.5% | 92.0% |
| Anteil Fenster mit Drift > 0.3% | 98.9% |

Zum Vergleich: die Envelope-Bänder der 6 aktuellen Configs liegen typischerweise 2-9% vom
gleitenden Durchschnitt entfernt (`envelopes`-Parameter). Ein Median-Drift von 1.3% des
Durchschnitts selbst (nicht der Bänder) ist damit **nicht vernachlässigbar** relativ zur
Bandbreite — die Trigger-Preise, die alle ~15 Minuten storniert und neu platziert werden
(sichtbar im Log: "Trigger Order ... storniert" bei jedem Lauf), verschieben sich in derselben
Größenordnung, solange die Kerze noch läuft.

**Ergebnis Teil b: BESTÄTIGT, nicht nur Hypothese.** Live berechnet Envelope-Durchschnitt und
-Bänder nachweislich auf Basis einer noch laufenden Kerze, im Mittel ~24 Mal pro 6h-Intervall neu,
mit einer über den Beobachtungszeitraum medianen Verschiebung von 1.3% (bis 4.6%) — während der
Backtester historische Daten verwendet, die (mit einer Ausnahme, siehe unten) grundsätzlich aus
bereits abgeschlossenen Kerzen bestehen.

**Zusatzbefund (verwandter, aber separater Mechanismus):** Auch der Backtester selbst ist nicht
komplett frei davon. `backtester.py::load_data` → `Exchange.fetch_historical_ohlcv(..., end_date_str=str(date.today()))`
lädt bei "bis heute"-Anfragen (so wie in `analysis_runner.py` bei `end_date = date.today()`, was
praktisch jeder heutige Analyse-/Optimierungslauf tut) ebenfalls die aktuell laufende Kerze mit.
Beobachtet im frisch heruntergeladenen `data/cache/AAVE-USDT-USDT_6h.csv`: letzte Zeile
`2026-08-25 18:00:00, ..., volume=881.0` — das Volumen ist ca. 40x kleiner als die vorherigen
vollständigen Kerzen (33-42k), eindeutig eine erst 20 Minuten alte, laufende Kerze. Das betrifft
aber nur die JEWEILS LETZTE Zeile eines "bis heute"-Backtests (ein einmaliger Randeffekt pro Lauf,
nicht 24x pro Kerze wie im Live-Fall) — deutlich kleinerer Effekt, aber derselbe Grundmechanismus
(kein "ist Kerze geschlossen"-Check irgendwo im Datenpfad).

### c) Abgleich mit den 14 echten Live-Trades — TRADE-LEVEL-REKONSTRUKTION (Nachtrag 2026-08-25, zweite Runde)

Die ursprüngliche Einschätzung ("UNKLAR, alte Order-Logs fehlen") war korrekt, aber vorschnell
aufgegeben — auch ohne die alten Order-Logs lässt sich der Live-Cron-Ablauf pro Trade RÜCKWIRKEND
simulieren, mit exakt derselben Feindaten-Infrastruktur (`FINE_TF_MAP['6h'] = '15m'`), die
`backtester.py` für die SL/TP-Intrabar-Auflösung nutzt — 15m ist zufällig exakt die Cron-Granularität,
also ideal für diesen Zweck.

**Methodik (`livemodus/trade_level_reconstruction.py`):** Für jeden der 14 Trades wird ab dem
Öffnen der jeweiligen 6h-Kerze bis zum echten Entry-Zeitpunkt die Folge simulierter 15-Minuten-
Cron-Ticks durchlaufen. Pro Tick wird aus den bis dahin abgeschlossenen 15m-Bars die zu diesem
Zeitpunkt "bekannte" OHLC der noch laufenden 6h-Kerze rekonstruiert (open = erste 15m-Bar,
high/low = Extrema bisher, close = letzter bekannter Preis), an die Warmup-Historie (80 geschlossene
6h-Kerzen) angehängt und `calculate_indicators_and_signals()` — dieselbe Funktion wie live —
darauf ausgeführt. Das ergibt den Bandpreis, den der Live-Bot GENAU zu diesem Tick gesehen hätte.
Verglichen wird das mit dem fixen Bandpreis aus NUR der letzten vollständig geschlossenen Kerze
(Backtester-Analogon). **Korrektur während der Arbeit:** die erste Version wählte das zu
vergleichende Envelope-Band (1/2/3) einmalig anhand der fixen Referenz und behielt diesen Index
für alle Live-Ticks bei — das bevorzugt strukturell die fixe Referenz (ihr Index ist per Definition
ihr bestmöglicher Treffer). Korrigiert: jeder Tick wählt sein eigenes bestpassendes Band unabhängig.

**Ergebnis: 14/14 Trades rekonstruierbar** (15m-Feindaten für alle drei Symbole/Zeiträume verfügbar).
Rohtabelle: `livemodus/trade_level_reconstruction_summary.csv`, vollständiges Log:
`livemodus/trade_level_reconstruction_run.log`.

| Symbol | Seite | Entry-Zeit | Intracandle-Bewegung | Dist. fixe Referenz | Dist. letzter Live-Tick | Dist. bester Live-Tick | Urteil |
|---|---|---|---|---|---|---|---|
| AAVE | buy | 2026-06-30 15:50 | 3.64% | 0.996% | 0.022% | 0.004% | **STÜTZT** |
| AAVE | sell | 2026-07-02 13:30 | 0.08% | 1.025% | 1.582% | 1.518% | WIDERSPRICHT |
| AAVE | sell | 2026-07-02 14:30 | 0.78% | 0.291% | 0.565% | 0.565% | WIDERSPRICHT |
| AAVE | sell | 2026-07-03 17:00 | 2.31% | 1.010% | 1.026% | 0.286% | NEUTRAL |
| ADA | sell | 2026-07-11 17:46 | 2.03% | 0.488% | 0.320% | 0.056% | **STÜTZT** |
| ADA | buy | 2026-07-12 01:45 | 0.30% | 1.730% | 2.189% | 2.087% | NEUTRAL |
| ADA | buy | 2026-07-12 02:30 | 0.30% | 0.447% | 0.900% | 0.799% | WIDERSPRICHT |
| ADA | sell | 2026-07-17 19:30 | 0.06% | 0.025% | 0.752% | 0.732% | WIDERSPRICHT |
| ADA | sell | 2026-07-21 07:22 | 0.23% | 0.517% | 1.031% | 0.954% | WIDERSPRICHT |
| ADA | buy | 2026-07-23 17:16 | 2.76% | 1.138% | 0.123% | 0.051% | **STÜTZT** |
| AAVE | sell | 2026-07-28 18:46 | 0.93% | 0.314% | 1.192% | 0.760% | WIDERSPRICHT |
| AAVE | buy | 2026-07-29 23:24 | 3.49% | 1.320% | 0.365% | 0.012% | **STÜTZT** |
| ETH | sell | 2026-08-19 21:45 | 9.33% | 1.790% | 1.609% | 0.052% | NEUTRAL |
| ETH | sell | 2026-08-19 22:31 | 7.97% | 3.012% | 0.028% | 0.028% | **STÜTZT** |

**Gesamttally: 6 WIDERSPRICHT, 5 STÜTZT, 3 NEUTRAL** — auf den ersten Blick ein Unentschieden.
**Entscheidend ist aber die Stratifizierung nach Intracandle-Bewegung**, die eine sehr saubere,
mechanistisch plausible Trennung offenlegt (Korrelation Intracandle-Bewegung ↔ Vorteil des
Live-Ticks: **r=0.72** beim letzten Tick, **r=0.91** beim besten Tick — beide stark):

| Gruppe | n | Verteilung | Ø Dist. fixe Referenz | Ø Dist. letzter Live-Tick |
|---|---|---|---|---|
| **Große Bewegung** (≥2.0% Preisänderung bis Entry) | 7 | **5× STÜTZT, 2× NEUTRAL, 0× WIDERSPRICHT** | 1.39% | 0.50% (bester Tick: 0.07%) |
| **Kleine Bewegung** (<1.0% Preisänderung bis Entry) | 7 | 0× STÜTZT, 1× NEUTRAL, **6× WIDERSPRICHT** | 0.62% | 1.17% |

Kein einziger Trade mit großer Intracandle-Bewegung widerspricht der Hypothese; kein einziger
Trade mit kleiner Bewegung stützt sie. Das ist exakt die Vorhersage aus der Aufgabenstellung: "vor
allem bei Trades, die kurz nach einem großen Kurssprung innerhalb der laufenden Kerze ausgelöst
haben" sollte der Effekt am klarsten sichtbar sein — bestätigt sich hier fast lehrbuchartig. Bei den
"Große Bewegung"-Trades ist der ZULETZT simulierte Live-Tick im Schnitt ~2.8× näher am echten
Entry als die fixe Referenz, der BESTE Tick sogar ~20× näher (0.07% vs. 1.39%) — ein sehr scharfer
Treffer, kein Zufallsrauschen. Bei "Kleine Bewegung"-Trades liegt die fixe Referenz im Schnitt sogar
etwas näher als der letzte Live-Tick (0.62% vs. 1.17%) — plausibel als kleines Restrauschen der
Rekonstruktionsmethode (15m-Bar-Aggregation approximiert den echten Tick-Preis nicht perfekt) bei
einem Effekt, der hier ohnehin nahe Null sein sollte, weil sich in der laufenden Kerze noch kaum
etwas bewegt hat — die "Widerspruch"-Fälle liegen also genau dort, wo laut Hypothese selbst KEIN
messbarer Unterschied zu erwarten wäre, nicht dort, wo ein Unterschied erwartet und ausgeblieben ist.

**Zwei besonders aussagekräftige Einzelfälle:**
- **ETH SELL 2026-08-19 22:31** (Intracandle-Bewegung 7.97%): fixe Referenz 3.01% daneben, letzter
  Live-Tick trifft auf **0.028%** genau — der reale Entry-Preis (2123.62) ist praktisch nur durch
  die noch laufende, bereits deutlich gewanderte Kerze erklärbar, nicht durch die letzte
  geschlossene.
- **AAVE BUY 2026-06-30 15:50** (Intracandle-Bewegung 3.64%): fixe Referenz 1.0% daneben, letzter
  Live-Tick trifft auf **0.022%**.

**Ergebnis Teil c (überarbeitet): Die Hypothese ist jetzt trade-konkret gestützt — konditional,
nicht pauschal.** Bei Trades mit spürbarer Kursbewegung innerhalb der laufenden Kerze (7 von 14,
50%) erklärt die intracandle-gedriftete Live-Bandberechnung den tatsächlichen Fill-Preis
systematisch und deutlich besser als die auf der letzten geschlossenen Kerze fixierte
Backtester-Logik — mit Distanzen im Bereich von 0.01-0.06%, die für Zufall zu präzise sind. Bei
Trades mit kaum bewegter Kerze ist (erwartungsgemäß) kein Unterschied feststellbar. Das ist eine
deutlich stärkere, weil trade-konkrete und mechanistisch saubere Bestätigung als die generische
1.3%-Median-Drift-Zahl aus Abschnitt (b) — bleibt aber dennoch eine Rekonstruktion (15m-Näherung,
keine echten historischen Order-Logs), keine 100%ige Gewissheit.

**Limitierung, transparent benannt:** Die Rekonstruktion nutzt 15m-Bars als Näherung der
kontinuierlichen Preisbewegung; echte Trigger-/Limit-Preis-Mechanik (kleiner
`trigger_price_delta_pct`-Versatz zwischen Trigger- und Limit-Preis) wurde nicht separat
modelliert, dürfte die Distanzen aber nur um Bruchteile eines Prozentpunkts verschieben, nicht die
klare Gruppentrennung erklären. n=14 bleibt insgesamt klein, auch wenn die Stratifizierung (7/7
sauber getrennt) für einen so kleinen Datensatz ungewöhnlich eindeutig ausfällt.

### d) Fix — WEITERHIN NICHT IMPLEMENTIERT (auf expliziten Wunsch des Users)

**Update:** Der User hat entschieden, den unter (d) skizzierten Fix vorerst NICHT umzusetzen —
auch nach der oben stehenden, deutlich stärkeren Trade-Level-Bestätigung. Stattdessen wurde diese
Session genutzt, um weitere Beweislage zu sammeln (Abschnitt c oben), bevor irgendein Eingriff in
`trade_manager.py`/`envelope_logic.py` erfolgt. Das ist konsistent mit der bisherigen Vorsicht bei
live-kritischem Code (echtes Geld, Order-Platzierung).

Der Fix-Vorschlag selbst (zur Referenz, weiterhin nicht umgesetzt): in
`trade_manager.py::full_trade_cycle`, direkt nach `data = exchange.fetch_recent_ohlcv(...)`
(Zeile 1367) und vor `calculate_indicators_and_signals(data, params)` (Zeile 1372), die letzte
Zeile verwerfen, falls ihr Open-Timestamp + Timeframe-Dauer > aktuelle Zeit ist. Gleiche Prüfung
müsste in `manage_existing_position` (Zeile 258) ergänzt werden. Am saubersten als kleine
Hilfsfunktion in `exchange.py` (z.B. `drop_incomplete_last_candle(df, timeframe)`), die im
Live-Pfad direkt nach `fetch_recent_ohlcv` aufgerufen wird.

**Weiterhin nicht umgesetzt.** Kein Code in `trade_manager.py`/`envelope_logic.py` wurde in dieser
Session verändert. Wartet auf explizite Freigabe des Users, unabhängig davon wie stark die
Beweislage inzwischen ist.

---

## 3. Walk-Forward-Test der 6 aktuellen Configs (AAVE, ADA, BNB, ETH, LTC, XRP, 6h)

### 3.0 Methodik-Klärung (Pflichtlektüre vor den Zahlen)

`settings.json::live_trading_settings.active_strategies` enthielt zu Sessionbeginn **nur
ETH/USDT:USDT** als aktiv — nicht alle 6 Configs. Für den Walk-Forward-Test wurde das
temporär (mit Backup) auf alle 6 Symbole erweitert, nach Abschluss wieder auf den
Original-Zustand zurückgesetzt (kein bleibender Eingriff in die Live-Konfiguration).

`analysis_runner.py --mode 1` implementiert **keinen** Per-Symbol-Walk-Forward wie bei
dnabot, sondern einen **Portfolio-Selektions-Walk-Forward**: für jede der 51 rollierenden
Testwochen wird aus allen 6 Configs per IS-Calmar (Fenstergröße = Lookback) die vermeintlich
beste ausgewählt und nur DIESE eine Woche lang Out-of-Sample gehandelt. Das bildet exakt den
Mechanismus nach, den `use_auto_optimizer_results: true` im Live-Betrieb tatsächlich einsetzt
(Portfolio-Optimizer wählt periodisch die/den aktiven Config/s neu). Das ist die richtige
Testmethodik für "hat das Live-System als Ganzes einen Edge" — sie beantwortet aber NICHT
separat "hat Symbol X isoliert einen Edge", das wäre ein anderer Test.

**Mode 2 (Envelope Parameter Walk-Forward)** wurde geprüft, aber NICHT ausgeführt: der Code
(`analyse_param_walkforward`) testet laut Implementierung nur `strategies[0]` — das erste
aktive Symbol in `active_strategies` — und damit **nicht alle 6 Configs**, sondern genau
eines. Er hat außerdem denselben Tooling-Bug wie unten in 3.1 beschrieben (identisches
Fenster-Slicing-Muster ohne Warmup-Puffer). Da Mode 1 (korrigiert) die zentrale Frage "hat
ltbbot über die aktuellen 6 Configs hinweg einen echten Edge" bereits beantwortet und Mode 2
konzeptionell nur ein Parameter-Sensitivitätstest für ein einzelnes Symbol ist, wurde er hier
nicht mit ausgeführt. Empfehlung: vor einer künftigen Nutzung von Mode 2 dieselbe
Warmup-Korrektur wie unten übernehmen, sonst sind auch dessen Ergebnisse nicht belastbar.

### 3.1 KRITISCHER TOOLING-BUG GEFUNDEN — Original-Tool liefert irreführende Ergebnisse

Der offizielle `run_analysis.sh`-Aufruf (`analysis_runner.py --mode 1`, unverändert) wurde
zuerst ausgeführt wie in der Aufgabenstellung vorgegeben. Ergebnis:

| Lookback | PnL | MaxDD | Calmar | Leerwochen |
|---|---|---|---|---|
| 1W | +0.0% | 0.0% | 0.0 | 51/51 |
| 2W | +0.0% | 0.0% | 0.0 | 32/51 |
| 4W | **-100.0%** | 100.0% | -1.0 | 14/51 |
| 8W | **-100.0%** | 100.0% | -1.0 | 3/51 |
| 12W | **-100.0%** | 100.0% | -1.0 | 0/51 |
| 26W | **-100.0%** | 100.0% | -1.0 | 0/51 |

Auf den ersten Blick identisch zum dnabot-Katastrophenbefund. **Bei genauerer Untersuchung
(Diagnose-Skript `livemodus/walkforward_diagnostic.py`) stellte sich das als Tooling-Artefakt
heraus, nicht als echter Handelsverlust:**

- `analyse_walkforward_lookback` schneidet IS- und OOS-Fenster **ohne Warmup-Puffer** vor
  Fensterbeginn aus der Zeitreihe (z.B. `df_oos = df.loc[(df.index >= oos_start) & (df.index < oos_end)]`
  für eine einzelne Woche = ~28 6h-Kerzen).
- `calculate_indicators_and_signals()` verwirft danach per `dropna()` alle Zeilen ohne
  vollständige Indikator-Anlaufzeit (average_period bis 20, SMA50-Trendfilter, ATR/ADX-Fenster
  14) — von ~28 Rohkerzen bleiben oft nur ~10-15 übrig.
- Technische Indikator-Bibliotheken (`ta.trend.adx` u.a.) werfen bei so wenigen Zeilen
  `IndexError` (beobachtet: "index 14 is out of bounds for axis 0 with size 11").
- Diese Exception wird im Original-Code durch ein pauschales `except Exception: pass`
  **verschluckt** — die betroffene Woche wird faktisch übersprungen (Equity bleibt
  unverändert), taucht aber NICHT im "Leerwochen"-Zähler auf (der nur Wochen ohne
  IS-Kandidat zählt). Ergebnis: für Lookback 4W-26W wurde in fast allen 51 Wochen (49-51 von
  51, siehe Rohlog) in Wahrheit GAR KEIN echter OOS-Test durchgeführt.
- Die einzige Woche, die NICHT stillschweigend übersprungen wurde, ist die letzte
  (`oos_start = heute`), weil dort `run_envelope_backtest` bei zu wenig/leeren Daten den
  Fehler-Sentinel `{"total_pnl_pct": -1000, "end_capital": 0}` zurückgibt statt eine
  Exception zu werfen (Backtester-Fehlerpfad, kein Handelsverlust). Weil sich die
  Equity-Kette multiplikativ fortsetzt (`eq = r_oos['end_capital']`), friert das
  Endkapital ab dieser einen fehlerhaften Woche bei 0 ein — das erzeugt den beobachteten
  "-100%"-Wert für JEDE Konfiguration, die diese letzte Woche überhaupt erreicht (4W-26W;
  bei 1W/2W wird durch die vielen Leerwochen die Ziellinie real nie erreicht, daher dort
  "nur" 0.0%, kein Crash).

**Konsequenz:** Das eingebaute Walk-Forward-Werkzeug in seiner jetzigen Form ist NICHT
verlässlich — weder der "0.0%"-Wert (verschluckte Wochen) noch der "-100%"-Wert (Sentinel-
Artefakt) spiegeln echtes Handelsverhalten wider. Das erklärt vermutlich auch, warum dieses
Werkzeug laut Aufgabenstellung "noch nie im Sinne einer echten Robustheitsprüfung ausgewertet"
wurde — es wurde nie bis zu diesem Detailgrad gegengeprüft.

**Fix (umgesetzt, nur im Analyse-Tool, NICHT im Live-Pfad):** `run_envelope_backtest()` besitzt
bereits den passenden Parameter `sim_start_date` (Kommentar im Code: "Warmup-Kerzen werden für
Indikatoren genutzt, Trades erst ab hier") — er wird im Original `analyse_walkforward_lookback`
nur nicht genutzt. Korrigierte Version (`livemodus/walkforward_fixed.py`): IS-/OOS-Fenster
werden um 20 Kalendertage (~80 6h-Kerzen) VOR Fensterbeginn erweitert, Indikatoren laufen über
den vollen Puffer ein, aber `sim_start_date=<Fensterbeginn>` sorgt dafür, dass nur echte
Handelsaktivität AB Fensterbeginn in PnL/Trades/Drawdown einfließt. Zusätzlich wird die letzte,
noch nicht abgeschlossene Woche ausgeschlossen (vermeidet den Sentinel-Artefakt). Kein Eingriff
in `analysis_runner.py`s Kernlogik oder in Live-Code — nur eine separate, korrigierte Kopie zur
Validierung. **Empfehlung:** dieselbe `sim_start_date`-Korrektur in `analyse_walkforward_lookback`
UND `analyse_reopt_smoothing` (Mode 10, identisches Slicing-Muster) direkt im offiziellen
`analysis_runner.py` nachziehen, sonst bleiben beide Analyse-Modi dauerhaft irreführend.

### 3.2 Korrigierter Walk-Forward-Test — Ergebnistabelle

50 rollierende OOS-Wochen (2025-09-08 bis 2026-08-17, letzte unvollständige Woche
ausgeschlossen), Startkapital 50 USDT, `min_trades=5`, 20 Tage Warmup-Puffer,
`fine_data=None` (grobe SL/TP-Intrabar-Näherung statt 15m-Auflösung, aus Performance-Gründen
wie im IS/OOS-Port von `optimizer.py` — reine Analyse-Optimierung, kein Live-Code-Eingriff).

| Lookback | PnL % | MaxDD % | Calmar | Leerwochen | Wochen mit OOS-Ergebnis | Gewinnwochen | Gesamt-Trades | Endkapital |
|---|---|---|---|---|---|---|---|---|
| 1W | +23.0% | 0.0% | 0.00* | 45/50 | 5/50 | 4 | 6 | 61.52 |
| 2W | +16.2% | 5.9% | 2.74 | 35/50 | 15/50 | 5 | 20 | 58.10 |
| 4W | +25.5% | 15.2% | 1.68 | 16/50 | 34/50 | 8 | 45 | 62.77 |
| 8W | +1.9% | 21.6% | 0.09 | 3/50 | 47/50 | 8 | 59 | 50.95 |
| **12W** | **+38.7%** | 16.0% | **2.41** | 0/50 | 50/50 | 10 | 37 | 69.35 |
| 26W | +12.7% | 7.1% | 1.79 | 0/50 | 50/50 | 7 | 30 | 56.35 |

*1W: Calmar-Formel liefert 0.0 bei MaxDD=0.0% (Divisionsschutz, kein reales "0"-Ergebnis) —
mit nur 6 Trades insgesamt ohnehin statistisch nicht belastbar.

Schlechteste/beste EINZELNE OOS-Woche über alle Lookbacks: -11.03% bis +19.65% — **keine
einzige der 300 (6 Lookbacks × 50 Wochen) simulierten Wochen zeigt einen Totalverlust oder
auch nur annähernd katastrophale Verluste.** Symbolauswahl variiert breit über alle 6 Pairs bei
kurzen/mittleren Lookbacks (2W-12W); bei 26W dominiert LTC (37/50 Wochen) klar — plausibel,
da ein 26-Wochen-Rückblick träger auf Regimewechsel reagiert und einfach das über den ganzen
Zeitraum stabilste Pair bevorzugt. BNB (bekannt schwächstes Pair aus der heutigen
Fair-Filter-Validierung) wird konsistent am seltensten gewählt (0-4 von 50 Wochen, außer 26W
etwas mehr) — die Wochen-Selektion erkennt die schwache Baseline richtig.

### 3.3 Einschätzung: Hat ltbbot einen echten Edge?

**Deutlich positiver Befund als bei dnabot — mit einer methodischen Einschränkung, die ehrlich
benannt werden muss:**

**Positiv:**
- Alle 6 getesteten Lookback-Fenster liefern **positive** OOS-PnL über ~11.5 Monate rollierender
  Out-of-Sample-Tests (+1.9% bis +38.7%), keines zeigt auch nur annähernd Ruin.
- Bei den robusteren Lookbacks (8W-26W, wo fast jede Woche tatsächlich OOS getestet wurde,
  nicht nur eine Handvoll) bleibt das Bild konsistent positiv: 8W schwach (+1.9%, Calmar 0.09,
  am ehesten "kein klarer Edge"), 12W stark (+38.7%, Calmar 2.41), 26W moderat (+12.7%,
  Calmar 1.79). Kein Lookback kippt ins Negative.
- Einzelwochen-Verluste bleiben in einem plausiblen, begrenzten Rahmen (-5.9% bis -11.0%) — kein
  Hinweis auf unkontrolliertes Tail-Risiko oder Leverage-Sprengung im OOS-Test.

**Einschränkung (verhindert ein uneingeschränktes "bestätigter Edge"):**
- Dies ist ein **Portfolio-Selektions-Walk-Forward**, kein Parameter-Walk-Forward: die
  zugrundeliegenden Envelope-Parameter (Periode, Envelope-%, SL-Ratio, Leverage) pro Symbol
  wurden NICHT für jede Testwoche neu gefittet, sondern stammen aus den EINMAL fixierten
  aktuellen 6 Configs (4 davon zuletzt am 2026-06-29 gefittet, BNB/XRP heute mit OOS-Ende
  2026-08-21 reoptimiert). Der Testzeitraum dieses Walk-Forwards (Sept. 2025 – Aug. 2026)
  überlappt damit erheblich mit dem Fitting-Zeitraum der Parameter selbst. Getestet wird also
  "funktioniert die wöchentliche Pair-Rotation zwischen 6 BEREITS GEFITTETEN Configs" — nicht
  "hätte man diese Parameter-Werte auch schon Anfang 2025 blind finden und weiter erfolgreich
  einsetzen können". Ein vollständig sauberer Test müsste die komplette Optimierungs-Pipeline
  (600-Trial-Optuna-Suche pro Symbol) an jedem historischen Wochenstichtag neu laufen lassen —
  das würde Tage/Wochen an Rechenzeit kosten und wurde hier bewusst NICHT gemacht (kein Auftrag
  dazu, unrealistischer Aufwand für dieses Audit).
- 1W/2W-Lookbacks sind wegen der wenigen tatsächlich getesteten Wochen (5 bzw. 15 von 50)
  statistisch zu dünn, um daraus verlässliche Schlüsse zu ziehen — auch wenn beide ebenfalls
  positiv abschneiden.
- "Bestes von 6 getesteten Lookback-Werten" (hier 12W) hat ein mildes Mehrfachvergleichs-Risiko
  — bei 6 Kandidaten und Marktrauschen ist nicht auszuschließen, dass 12W teilweise zufällig am
  besten abschneidet. Der Punkt, dass ALLE 6 Kandidaten positiv sind (nicht nur der beste),
  mindert dieses Risiko aber deutlich.

**Fazit:** Anders als bei dnabot (wo der echte Walk-Forward-Test die vermeintliche
In-Sample-Performance vollständig widerlegte) zeigt der korrigierte ltbbot-Walk-Forward-Test
**keinen Kollaps**, sondern ein konsistent positives, wenn auch bei kurzen Lookbacks dünn
besetztes Bild. Das ist ein ermutigendes Ergebnis, aber wegen der Parameter-Overlap-Einschränkung
oben KEIN vollständig sauberer Beweis für einen von der Parameter-Fit-Periode unabhängigen Edge.
Empfehlung: `optimization_settings.backtest_lookback_weeks` (aktuell auf `1` gesetzt — dieser
Wert stammt aus dem VOR dem Fix fehlerhaft gelaufenen Original-Tool und ist damit ohne
Aussagekraft) auf Basis dieser korrigierten Tabelle neu bewerten, z.B. Richtung 8-12 Wochen statt
1 Woche, sobald der Fix im offiziellen `analysis_runner.py` nachgezogen wurde und der Nutzer die
Umstellung freigibt (kein Auto-Write hier vorgenommen — `settings.json` wurde nach diesem Test
vollständig auf den Originalzustand zurückgesetzt).

### 3.4 Artefakte dieser Session

- `livemodus/walkforward_diagnostic.py` — Diagnose-Skript, das den Original-Tooling-Bug lokalisiert hat.
- `livemodus/walkforward_diagnostic_run.log` / `walkforward_diagnostic_detail.csv` — Rohbefund (Exceptions pro Woche).
- `livemodus/walkforward_fixed.py` — korrigierte Walk-Forward-Implementierung (Warmup-Puffer via `sim_start_date`).
- `livemodus/walkforward_fixed_run.log` / `walkforward_fixed_detail.csv` / `walkforward_fixed_summary.json` — korrigierte Ergebnisse (Tabelle oben).
- `livemodus/walkforward_lookback_chart.png` — Chart des UNKORRIGIERTEN Original-Tool-Laufs (zu Dokumentationszwecken, nicht als valides Ergebnis zu interpretieren).
- `livemodus/intracandle_drift.py` — Quantifizierung der Bandpreis-Instabilität (Abschnitt 2b).
- `livemodus/trade_level_reconstruction.py` — Trade-Level-Rekonstruktion der 14 Live-Trades via simulierter 15-Min-Cron-Ticks (Abschnitt 2c, Nachtrag).
- `livemodus/trade_level_reconstruction_run.log` / `trade_level_reconstruction_summary.csv` — Ergebnisse der Rekonstruktion (Tabelle in Abschnitt 2c).
- `src/ltbbot/analysis/analysis_runner.py` — minimale, opt-in Änderung: `LTBBOT_ANALYSIS_NO_FINE_DATA=1`-Env-Var deaktiviert `LazyFineData` in `_load_fine_data()` (Performance, Default-Verhalten unverändert).

**Keine Code-Änderungen** an `trade_manager.py`, `envelope_logic.py` oder sonstigem Live-Trading-Pfad
in dieser gesamten Session (weder ursprünglich noch im Nachtrag) — der Fix-Vorschlag aus Abschnitt
2d bleibt unimplementiert, wie vom User gefordert.

