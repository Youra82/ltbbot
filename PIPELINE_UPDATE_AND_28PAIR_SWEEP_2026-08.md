# ltbbot -- Pipeline-Update + 28-Paar-Optimierungslauf (2026-08-26)

Auftrag: Pipeline/Backtester/Portfolio-Optimizer auf Konsistenz pruefen, dann
BTC/XRP/ETH/SOL/ADA/AAVE/DOGE x 6h/4h/2h/1h (28 Kombinationen) ueber
`optimizer.py` mit IS/OOS-Split + K-Fold-Robustheit optimieren.

## Teil 1: Pipeline/Backtester/Portfolio-Optimizer -- Konsistenz-Check

### 1.1 `run_pipeline.sh` -> `optimizer.py` Parameterweitergabe
Geprueft: `run_pipeline.sh` ruft `optimizer.py` bereits korrekt mit
`--is_fraction`, `--k_folds`, `--min_oos_trades` auf (siehe Zeilen mit
`IS_FRACTION`/`K_FOLDS`/`MIN_OOS_TRADES`, die aus `settings.json::optimization_settings`
vorbelegt werden). **Keine Aenderung noetig** -- der Port vom 2026-08-21 war
bereits vollstaendig.

### 1.2 Broken venv -- WSL-Python-Umgebung neu aufgesetzt
Kritischer Blocker vor jeglicher Ausfuehrung: `.venv/bin/python`, `.venv/bin/python3`
etc. waren keine funktionierenden Symlinks/Shebang-Skripte mehr, sondern reine
Text-Dateien mit dem Inhalt `python3` (7 Byte) -- vermutlich durch einen
Windows-Kopiervorgang (Desktop-Sync von einer WSL/Linux-Quelle), der Symlinks
nicht als solche kopiert hat, sondern als Text abgelegt hat. Weder unter
Windows (`C:\Python312\python.exe` hat kein `optuna`/`pandas`/`ta` installiert)
noch unter WSL (`.venv` selbst kaputt) liess sich der Optimizer direkt starten.

**Fix:** Neues, sauberes venv unter WSL Ubuntu angelegt (`/mnt/c/.../ltbbot/.venv_new`,
`python3 -m venv` + `pip install -r requirements.txt`). Das alte `.venv/` wurde
NICHT geloescht (nur ignoriert) -- keine destruktive Aktion. Alle Optimizer-Laeufe
dieser Session verwenden `.venv_new/bin/python` unter WSL
(`wsl.exe -d Ubuntu -- ... /mnt/c/Users/.../ltbbot/.venv_new/bin/python ...`).
**Empfehlung:** `run_pipeline.sh`/`update.sh` perspektivisch auf `.venv_new` (oder
ein neu erstelltes `.venv`) umstellen bzw. beim naechsten VPS-Deployment sicherstellen,
dass Symlinks beim Uebertragen erhalten bleiben (z.B. `rsync -a` statt Windows-Explorer-Kopie).

### 1.3 `data/cache/` -- Cache fuer die 7 Symbole komplett frisch aufgebaut
Alle Cache-Dateien der 7 Symbole (BTC/XRP/ETH/SOL/ADA/AAVE/DOGE) fuer 1h/2h/4h/6h
(+ die zugehoerigen Fein-Timeframes 5m/15m fuer die praezise Nachbewertung) waren
laut Datei-Zeitstempel aus **Nov/Dez 2025** -- deutlich vor dem heutigen
Pagination-Off-by-one-Fix in `exchange.py` (11.08.2026 laut Git-Log) entstanden
und damit potenziell luckenhaft (siehe `WALKFORWARD_AUDIT_2026-08.md` Abschnitt 1).
Diese 42 Cache-Dateien wurden geloescht; `load_data()` laedt sie beim ersten
Zugriff pro Paar automatisch mit dem jetzt korrekten Pagination-Code neu
(lueckenlos, verifiziert im Audit fuer ADA/1d: 0 fehlende Kerzen nach Fix).

### 1.4 `run_portfolio_optimizer.py` -- Lookback-Inkonsistenz gefunden und behoben
**Bug gefunden:** `run_portfolio_optimizer.py::LOOKBACK_MAP` verwendete andere
Werte als `run_pipeline.sh` fuer denselben Timeframe:

| Timeframe | `run_pipeline.sh` (kanonisch) | `run_portfolio_optimizer.py` (vorher) |
|---|---|---|
| 1h | 548 Tage | 365 Tage |
| 4h | 1095 Tage | 730 Tage |
| 1d | 1825 Tage | 1095 Tage |

Das bedeutet: der Portfolio-Optimizer haette Strategien auf einem KUERZEREN
Trainingsfenster neu bewertet/selektiert als dem, auf dem sie ueberhaupt per
`optimizer.py` gefittet und OOS-bestaetigt wurden -- ein stiller Bruch der
"nur auf Trainingsfenster selektieren"-Philosophie, die beide Systeme eigentlich
teilen sollen. **Fix:** `LOOKBACK_MAP` in `run_portfolio_optimizer.py` an
`run_pipeline.sh` angeglichen (5m/15m=90, 30m/1h=548, 2h=730, 4h/6h=1095, 1d=1825).
Architektur (70/30-Split-Berechnung selbst, Greedy-Selektion) unveraendert --
wie beauftragt kein Umbau, nur der Wertfehler behoben.

Ansonsten sind beide Systeme konzeptionell konsistent: `optimizer.py` optimiert
NUR auf IS-Daten und bestaetigt danach auf ungesehenen OOS-Daten;
`run_portfolio_optimizer.py` selektiert bei gesetztem `oos_reference_date`
ebenfalls nur auf dem Trainingsfenster (`end_date = oos_start - 1 Tag`) und
haelt die dahinterliegenden Tage nie fuer die Selektion vor. Ein Unterschied
bleibt bewusst unangetastet (kein Bug, sondern Architektur): der Portfolio-
Optimizer fuehrt selbst KEINE eigene OOS-Bestaetigung des gewaehlten Portfolios
durch (nur Vergleich neu-vs-aktuell auf dem Trainingsfenster) -- das waere ein
groesserer Architektur-Eingriff und war nicht Teil des Auftrags ("nur offensichtliche
Inkonsistenzen/Bugs beheben, nicht umbauen").

### 1.5 Walk-Forward-Warmup-Bug aus `WALKFORWARD_AUDIT_2026-08.md` in `analysis_runner.py` nachgezogen
Der Fix aus `livemodus/walkforward_fixed.py` (Warmup-Puffer via `sim_start_date`,
damit `calculate_indicators_and_signals()` bei kurzen IS-/OOS-Fenstern nicht per
`dropna()` fast alle Zeilen verliert und `ta.trend.adx()` nicht mit IndexError
abbricht) war bisher NUR in der separaten Analyse-Kopie implementiert, nicht im
offiziellen `analysis_runner.py`. Nachgezogen in:
- `analyse_walkforward_lookback` (Mode 1): IS-/OOS-Fenster um `WARMUP_DAYS=20`
  Tage nach vorne erweitert, `sim_start_date=<Fensterbeginn>` an
  `run_envelope_backtest()` uebergeben.
- `analyse_reopt_smoothing` (Mode 10, `_score()`-Helper + finaler OOS-Aufruf):
  identische Korrektur.

Kein Eingriff in Live-Code (`trade_manager.py`, `envelope_logic.py`) -- nur die
beiden Analyse-Modi. `analyse_param_walkforward` (Mode 2) wurde laut Audit
bewusst nicht mitkorrigiert (testet ohnehin nur `strategies[0]`, im Audit als
separates, kleineres Problem dokumentiert und nicht Teil dieses Auftrags).

### 1.6 `backtester.py` / `load_data()` Caching
Keine Aenderung noetig: `load_data()` erkennt bereits korrekt, ob der Cache den
angefragten Zeitraum abdeckt (`cache_start <= req_start and cache_end >= req_end`)
und laedt sonst frisch von der Boerse nach -- durch das Loeschen der alten
Cache-Dateien (1.3) wird das fuer alle 28 Kombinationen automatisch ausgeloest.

---

## Teil 2: 28-Kombinationen-Optimierungslauf

Parameter (identisch fuer alle 28 Laeufe, wie beauftragt):
```
--start_capital 50 --jobs -1 --max_drawdown 30 --min_win_rate 0 --min_pnl 0
--mode strict --min_trades_per_year 20 --config_suffix _envelope
--is_fraction 0.70 --k_folds 3 --min_oos_trades 10
```
Trials: 200 zuerst, bei `no_valid_trials` automatischer Retry mit 600 (Treiber-
Skript `livemodus/run_28pair_sweep.py`). Lookback: 6h/4h=1095 Tage, 2h=730 Tage,
1h=548 Tage (siehe `run_pipeline.sh`). End-Datum: 2026-08-26 (heute).

**Unterbrechung:** Der orchestrierende Agent ist waehrend DOGE/1h (letzte von 28
Kombinationen, 600-Trial-Nachlauf) an sein Session-Limit gestossen. Der
eigentliche Optimierungs-Subprozess (WSL, `.venv_new`) lief davon unbeeinflusst
im Hintergrund weiter und wurde separat abgewartet -- alle 28 Kombinationen
sind vollstaendig durchgelaufen, keine wurde abgebrochen oder mit abweichenden
Parametern nachgeholt.

**Wichtige Klarstellung zu den zwei PnL-Feldern in `_meta`:** `pnl_pct` ist der
Backtest-PnL ueber den GESAMTEN Zeitraum (IS+OOS zusammen, informativ, aber
NICHT die Bestaetigungsgrundlage). `oos_pnl_pct` ist die tatsaechliche
Out-of-Sample-PnL auf den nie gesehenen letzten 30% -- DAS ist die relevante
Zahl fuer "statistisch relevant und profitabel". Die Tabelle unten zeigt daher
`oos_pnl_pct` (nicht `pnl_pct`) als Hauptspalte.

### Ergebnistabelle (28/28 abgeschlossen)

**Bestaetigt (12/28) -- erfuellen alle drei Bedingungen (>=10 OOS-Trades, OOS-PnL>0, schlaegt Baseline falls vorhanden):**

| Symbol | TF | OOS-PnL | OOS-Trades | Gesamt-PnL (IS+OOS) |
|---|---|---|---|---|
| DOGE | 4h | **36.39%** | 35 | 280.38% |
| AAVE | 2h | **42.18%** | 53 | 167.65% |
| SOL | 4h | **38.11%** | 76 | 181.94% |
| DOGE | 6h | **36.26%** | 43 | 89.73% |
| DOGE | 2h | **33.01%** | 63 | 208.34% |
| ADA | 1h | **13.91%** | 11 | 1.81% |
| SOL | 6h | **11.63%** | 22 | 65.77% |
| ETH | 4h | **4.94%** | 66 | 12.17% |
| AAVE | 4h | **2.95%** | 13 | 33.60% |
| ADA | 4h | **1.90%** | 21 | 58.20% |
| XRP | 4h | **1.63%** | 69 | 79.22% |
| BTC | 4h | **0.09%** | 12 | 6.52% |

Sortiert nach OOS-PnL, nicht nach Gesamt-PnL -- letztere kann bei kleinem
OOS-Anteil deutlich groesser wirken als die tatsaechliche Out-of-Sample-Bestaetigung
(z.B. DOGE/4h: 280% Gesamt-PnL, aber "nur" 36.4% davon auf ungesehenen Daten).
BTC/4h und ADA/4h sind mit OOS-PnL von 0.09%/1.90% nur MARGINAL bestaetigt --
statistisch zwar über der Nulllinie, aber wirtschaftlich kaum ein Edge; vor
einem Live-Einsatz wuerde ich diese beiden eher zurueckhaltend behandeln.

**Nicht bestaetigt, aber gueltiger Trial gefunden (13/28) -- "kein Edge", kein Fehler:**

| Symbol | TF | OOS-PnL (Kandidat) | OOS-Trades | Baseline-OOS | Grund |
|---|---|---|---|---|---|
| ADA | 6h | -9.58% | 35 | 94.64% | schlaegt Baseline nicht |
| BTC | 2h | -7.33% | 9 | -- | OOS negativ |
| BTC | 6h | -6.83% | 14 | -- | OOS negativ |
| XRP | 2h | -1.06% | 4 | -- | OOS negativ |
| AAVE | 6h | 13.98% | 21 | 98.36% | schlaegt Baseline nicht |
| XRP | 6h | 5.30% | 9 | 86.83% | schlaegt Baseline nicht |
| DOGE | 1h | 1.78% | 7 | -- | zu wenig OOS-Trades (<10) |
| SOL | 1h | 2.25% | 1 | -- | zu wenig OOS-Trades |
| ADA | 2h | 1.29% | 2 | -- | zu wenig OOS-Trades |
| XRP | 1h | 1.30% | 2 | -- | zu wenig OOS-Trades |
| ETH | 2h | 1.44% | 8 | -- | zu wenig OOS-Trades |
| SOL | 2h | 1.01% | 1 | -- | zu wenig OOS-Trades |
| AAVE | 1h | 0.00% | 0 | -- | keine OOS-Trades |

**Kein gueltiger Trial gefunden, auch bei 600 Trials (3/28):**
BTC/1h, ETH/6h, ETH/1h -- Optuna fand unter den Constraints (Drawdown<=30%,
Mindest-Trades/Jahr, K-Fold-Robustheit) keinen einzigen zulaessigen Trial.
Legitimes Ergebnis, nicht durch Aufweichen der Constraints erzwungen.

### Kern-Beobachtungen
1. **4h ist der mit Abstand robusteste Timeframe** -- 5 von 7 Symbolen (BTC, XRP,
   ETH, ADA, AAVE) bestaetigen NUR bei 4h, kein anderer Timeframe bringt bei
   diesen Symbolen einen bestaetigten Edge.
2. **DOGE ist der staerkste Performer** -- als einziges Symbol auf 3 von 4
   Timeframes bestaetigt (6h/4h/2h), durchgehend hohe OOS-PnL (33-36%).
3. **1h liefert praktisch nirgends genug statistisch relevante Trades** -- einzige
   Ausnahme ADA/1h (11 OOS-Trades, knapp ueber der Schwelle). Bei stuendlichen
   Kerzen scheint der Envelope-Touch-Mechanismus strukturell zu selten
   auszuloesen fuer eine belastbare OOS-Stichprobe im 30%-Testfenster.
4. **BTC zeigt insgesamt den schwaechsten Edge** -- nur 4h bestaetigt, und das
   nur marginal (OOS-PnL 0.09%, wirtschaftlich vernachlaessigbar).
5. Alle als "Baseline schlaegt Kandidat" gescheiterten Faelle (ADA/6h, AAVE/6h,
   XRP/6h) betreffen die 6h-Configs, die schon aus der frueheren Session
   (2026-08-21, ADX-Gate-Fix) eine bereits recht robuste Baseline hatten --
   konsistent mit der frueheren Erkenntnis, dass eine gute bestehende Baseline
   schwer zu schlagen ist.

