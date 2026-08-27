# ltbbot Live-Trade-Forensik & Faire Filter-Validierung (2026-08-21)

Analog zur stbot-Session vom 2026-08-20 (siehe `research_stbot_energy_zscore_filter`), aber
angepasst an ltbbots Struktur: ltbbot ist ein **Envelope-Mean-Reversion-Bot** (Entry bei
Band-Touch, TP = Rückkehr zum gleitenden Durchschnitt), kein SR-Breakout-Bot. Getestet wurden
deshalb **Erschöpfungs-Signale** statt Fortsetzungs-Momentum, plus eine gezielte Prüfung des
eingebauten ADX-Regime-Gates.

**Datengrundlage:**
- 14 echte abgeschlossene Bitget-Live-Trades, 30.06.–19.08.2026, Symbole AAVE (5), ADA (6), ETH (2).
  Quelle: `livemodus/Exported USDT-M Futures position history 7770605553-2026-08-22 00_00_44.620.xls`
- Fairer Backtest-Pool: alle 6 aktuellen 6h-Configs (AAVE, ADA, BNB, ETH, LTC, XRP),
  2023-09-01 bis 2026-08-21 (4337 Kerzen/Symbol), inkl. 15m-Feindaten für SL/TP-Intrabar-Auflösung.

**⚠️ n=14 ist statistisch NICHT belastbar.** Alle Aussagen aus Teil 1 sind Tendenzen, keine
Beweise. Teil 2 (Backtest-Fair-Test) ist der eigentliche Validierungs-Maßstab, unabhängig von
den 14 Live-Trades.

---

## Teil 1 — Live-Trade-Forensik (n=14, diagnostisch)

### Alle 14 Trades

| Symbol | Side | Entry | Exit | PnL (USDT) | ADX@Entry | Regime | Band | Exit-Klass. | MAE% | MFE% | Dauer |
|---|---|---|---|---|---|---|---|---|---|---|---|
| AAVE | buy | 2026-06-30 15:50 | 16:01 | **+0.738** | 43.17 | STRONG_TREND | 2 | TP (dyn. MA) | 1.45 | 0.26 | 11 min |
| AAVE | sell | 2026-07-02 13:30 | 13:30 | -0.114 | 26.13 | TREND | 1 | SL (Match) | 0.45 | 1.49 | 0.3 min |
| AAVE | sell | 2026-07-02 14:30 | 16:19 | **+0.714** | 26.13 | TREND | 1 | TP (dyn. MA) | -0.29 | 2.91 | 109 min |
| AAVE | sell | 2026-07-03 17:00 | 17:48 | **+0.900** | 20.95 | UNCERTAIN | 2 | TP (dyn. MA) | -1.76 | 4.12 | 48 min |
| ADA | sell | 2026-07-11 17:46 | 20:21 | -0.069 | 21.16 | UNCERTAIN | 1 | SL (Match) | 0.70 | 0.99 | 155 min |
| ADA | buy | 2026-07-12 01:45 | 01:46 | -0.071 | 20.04 | UNCERTAIN | 1 | SL (Match) | 1.92 | -0.66 | 1.1 min |
| ADA | buy | 2026-07-12 02:30 | 02:30 | -0.108 | 20.04 | UNCERTAIN | 1 | SL (Match) | 0.06 | 1.22 | 0.2 min |
| ADA | sell | 2026-07-17 19:30 | 20:31 | -0.143 | 14.25 | RANGE | 1 | SL (Match) | 0.60 | 0.54 | 62 min |
| ADA | sell | 2026-07-21 07:22 | 07:23 | -0.090 | 13.50 | UNCERTAIN | 1 | SL (Match) | 1.31 | 0.17 | 1.2 min |
| ADA | buy | 2026-07-23 17:16 | 19:58 | -0.117 | 22.63 | UNCERTAIN | 1 | SL (Match) | 0.30 | 1.13 | 162 min |
| AAVE | sell | 2026-07-28 18:46 | 18:53 | -0.087 | 25.87 | TREND | 1 | SL (Match) | 0.63 | 1.25 | 6.8 min |
| AAVE | buy | 2026-07-29 23:24 | 00:23 | **+0.775** | 24.03 | UNCERTAIN | 1 | TP (dyn. MA) | 0.96 | 1.99 | 60 min |
| ETH | sell | 2026-08-19 21:45 | 21:56 | -0.055 | 16.58 | UNCERTAIN | 3 | SL (Match) | **11.39** | -5.58 | 11 min |
| ETH | sell | 2026-08-19 22:31 | 22:49 | -0.049 | 16.58 | UNCERTAIN | 3 | SL (Match) | **8.69** | -5.51 | 19 min |

4 Gewinner, 10 Verlierer (WR 28.6% — kleine Stichprobe, siehe Backtest-WR pro Config in Teil 2
für ein belastbareres Bild: 36–56%). "Band" = Envelope-Band mit dem geringsten Abstand zum
tatsächlichen Entry-Preis (1 = engstes Band). "Exit-Klass." = Abgleich mit der SL-Formel aus
`trade_manager.py` (`sl_to_env1_ratio`, ggf. ×1.5 im TREND-Regime); alles andere wird als
dynamischer MA-Take-Profit gewertet, da ltbbot **kein Trailing-Stop nutzt** (keine der 6
Live-Configs hat `trailing_callback_rate_pct` gesetzt).

Reproduzierbar via `livemodus/trade_forensics.py` (Skript + `trade_forensics_summary.csv`).

### MAE/MFE-Befund

- **MAE Gewinner: Ø 0.09%** vs. **MAE Verlierer: Ø 2.61%** — Gewinner liefen kaum ins Minus,
  bevor sie den TP erreichten.
- **MFE Gewinner: Ø 2.32%** vs. **MFE Verlierer: Ø -0.50%** — Verlierer kamen im Schnitt nie
  wirklich ins Plus, bevor sie den SL trafen.
- Die beiden ETH-Trades (19.08., Band 3 = weitestes Envelope, ADX 16.6, price_distance_pct 5.9%)
  zeigen extreme Ausreißer (MAE 11.4% / 8.7%) — ein einzelner heftiger Preisschub lief weit gegen
  beide Positionen, bevor der SL griff. Das ist der dominante Treiber des hohen Verlierer-MAE-Mittelwerts;
  ohne diese zwei Trades wäre der Rest der Verlierer deutlich moderater (0.06–1.92%).

**Einordnung:** Das Muster (Gewinner laufen kaum ins Minus, Verlierer kommen kaum ins Plus) ist
qualitativ plausibel für eine funktionierende Mean-Reversion-Strategie, aber bei n=14 (und einem
Ausreißerpaar, das 2 von 10 Verlierern stellt) nicht statistisch verwertbar.

### ADX-Rückrechnung: Gewinner vs. Verlierer

- ADX Gewinner (n=4): **Ø 28.57**, Median 25.08 — Werte: 43.17, 26.13, 20.95, 24.03
- ADX Verlierer (n=10): **Ø 19.68**, Median 20.04
- Mann-Whitney-U-Test: U=33.5, **p=0.065**

**Wichtig — Richtung ist gegenteilig zur ADX-Skepsis-Prior:** Die Gewinner hatten im Schnitt
**höheren** ADX (mehr Trend) als die Verlierer, nicht niedrigeren. Der größte Einzelgewinn
(+0.738 USDT) trat sogar bei ADX=43.17 auf — exakt im Bereich, den das alte STRONG_TREND-Gate
(ADX>30) komplett gesperrt hätte. Das deckt sich mit der Cross-Bot-Erfahrung "ADX-Filter ist oft
wirkungslos oder kontraproduktiv" (siehe Memory `feedback_adx_filter_generally_useless`) und
liefert hier sogar ein Tendenz-Signal in Richtung "Gate schadet mehr als es hilft" — bei p=0.065
und n=14 aber nur ein Hinweis, kein Beweis. Die eigentliche Validierung folgt in Teil 2.

**Methodischer Hinweis:** Da ltbbot Entry-Trigger-Orders vorab platziert und diese erst bei
Preisberührung füllen, kann der ADX-Wert zum *Fill*-Zeitpunkt (hier verwendet) vom ADX zum
*Order-Platzierungs*-Zeitpunkt abweichen. Der erste Trade (ADX=43, STRONG_TREND, Gewinner) ist
dafür ein Beispiel — unter der Annahme, dass der damalige (ungefixte) Live-Code Trigger bei
STRONG_TREND stornieren sollte, ist nicht abschließend rekonstruierbar, ob die Order kurz vor der
Regime-Verschlechterung platziert wurde oder ob es eine Lücke in der Cancel/Replace-Logik gibt.
Ändert nichts an der Kernaussage von Teil 2.

---

## Teil 2 — Faire Filter-Validierung im Backtest (Config-Pool, unabhängig von den 14 Live-Trades)

Methodik identisch zu stbot: **ein fixer Schwellenwert gleichzeitig über alle 6 Configs**,
Summe-PnL mit/ohne Filter verglichen, keine Cherry-Picks von "bestem Wert pro Config".
Backtest-Fenster: 2023-09-01 bis 2026-08-21 (identisch für alle Varianten). Skript:
`livemodus/fair_filter_validation.py`, Rohergebnisse: `livemodus/fair_filter_validation_results.json`.

**Baseline (aktuelle 6 Configs, unverändert):** Summe-PnL **1194.0%**, 924 Trades.

| Symbol | PnL % | Trades | WR % |
|---|---|---|---|
| AAVE | 359.0 | 240 | 36.7 |
| ADA | 315.6 | 193 | 46.6 |
| BNB | -15.1 | 148 | 19.6 |
| ETH | 136.1 | 127 | 44.1 |
| LTC | 119.9 | 89 | 47.2 |
| XRP | 278.4 | 127 | 55.9 |

### A) ADX-Regime-Gate-Varianten — VALIDIERT

| Variante | Summe PnL | Δ zur Basis | Trades | Verbessert |
|---|---|---|---|---|
| **Gate AUS (STRONG_TREND-Block komplett deaktiviert)** | **1515.0%** | **+321.0%** | 1367 | **5/6** |
| Gate-Schwelle ADX>25 (enger) | 1009.6% | -184.4% | 642 | 1/6 |
| Gate-Schwelle ADX>35 (lockerer) | 1301.6% | +107.6% | 1102 | 5/6 |
| Gate-Schwelle ADX>40 (sehr locker) | 1393.0% | +199.0% | 1233 | 5/6 |

Klarer, monotoner Dosis-Wirkungs-Zusammenhang: **je lockerer das Gate, desto besser die
Aggregat-PnL**, über 4 unabhängig getestete Varianten hinweg. Die komplette Deaktivierung des
STRONG_TREND-Blocks ist die beste Variante (+321% Summe-PnL, 5 von 6 Configs verbessert). Deckt
sich mit dem Tendenz-Signal aus Teil 1 (Gewinner hatten *höheren* ADX als Verlierer).

**Eine Ausnahme: BNB.** BNB ist der einzige Config, der von einem **strengeren** statt lockereren
Gate profitiert (ADX>25: -15.1% → +8.8%; Gate AUS: -15.1% → -45.2%, klar schlechter). BNB ist
schon in der Baseline der einzige unprofitable Config (WR nur 19.6%, wenige Trades) — ein
einzelner globaler Schwellenwert für alle Symbole wäre hier also falsch.

**Entscheidung:** Gate validiert als **per-Symbol Optuna-Suchparameter** (nicht als hartcodierter
globaler Wert) — genau wegen der BNB-Ausnahme. Details siehe "Was implementiert wurde" unten.

### B) Exhaustion-Filter-Kandidaten — ALLE VERWORFEN

Mean-Reversion-Exhaustion statt Momentum-Fortsetzung (bewusst anders als stbots
`energy_zscore`/`avalanche_percentile`, die dort für einen Breakout-Bot "hohe Energie = gut"
bedeuteten — hier wurde die Gegenrichtung mitgetestet).

| Variante | Summe PnL | Δ zur Basis | Trades | Verbessert |
|---|---|---|---|---|
| RSI-Extrem ≤20 / ≥80 | -18.5% | -1212.5% | 11 | 1/6 |
| RSI-Extrem ≤25 / ≥75 | -49.2% | -1243.2% | 53 | 1/6 |
| RSI-Extrem ≤30 / ≥70 | -143.7% | -1337.7% | 162 | 1/6 |
| Overshoot ≥0.1% | 1178.1% | -15.9% | 915 | 0/6 |
| Overshoot ≥0.3% | 964.7% | -229.3% | 856 | 0/6 |
| Overshoot ≥0.5% | 763.8% | -430.2% | 795 | 0/6 |
| Volumen-Klimax ≥1.3x | 265.3% | -928.7% | 365 | 1/6 |
| Volumen-Klimax ≥1.5x | 191.1% | -1002.9% | 274 | 1/6 |
| Volumen-Klimax ≥2.0x | 111.0% | -1083.0% | 145 | 1/6 |
| Volumen-Erschöpfung ≤0.7x | 450.2% | -743.8% | 186 | 1/6 |
| Volumen-Erschöpfung ≤0.5x | 196.3% | -997.7% | 82 | 1/6 |
| Wick-Rejection ≥0.3 | 457.9% | -736.1% | 506 | 1/6 |
| Wick-Rejection ≥0.5 | 183.7% | -1010.3% | 264 | 1/6 |
| Wick-Rejection ≥0.7 | 33.6% | -1160.4% | 66 | 1/6 |
| ATR-Perzentil Kontraktion ≤20 | 156.5% | -1037.5% | 135 | 1/6 |
| ATR-Perzentil Kontraktion ≤30 | 267.1% | -926.9% | 202 | 1/6 |
| ATR-Perzentil Expansion ≥70 | 504.5% | -689.5% | 431 | 1/6 |
| ATR-Perzentil Expansion ≥80 | 336.7% | -857.3% | 322 | 0/6 |
| Avalanche hoch ≥60 | 829.9% | -364.1% | 458 | 1/6 |
| Avalanche hoch ≥70 | 673.5% | -520.5% | 353 | 1/6 |
| Avalanche niedrig ≤30 | 105.8% | -1088.2% | 224 | 1/6 |
| Avalanche niedrig ≤40 | 191.1% | -1002.9% | 314 | 1/6 |
| Energy hoch ≥0.0 (stbot-Analog) | 551.4% | -642.6% | 268 | 1/6 |
| Energy hoch ≥0.3 | 368.4% | -825.6% | 186 | 1/6 |
| Energy niedrig ≤0.0 (Exhaustion) | 644.2% | -549.8% | 656 | 1/6 |
| Energy niedrig ≤-0.3 (Exhaustion) | 365.9% | -828.1% | 441 | 1/6 |
| Zeit seit Touch ≥3 Kerzen | 790.5% | -403.5% | 572 | 1/6 |
| Zeit seit Touch ≥5 Kerzen | 788.6% | -405.4% | 539 | 1/6 |
| Zeit seit Touch ≥10 Kerzen | 676.0% | -518.0% | 486 | 1/6 |

**Alle 28 getesteten Varianten sind negativ** (Δ zwischen -16% und -1338%), fast alle verbessern
höchstens 1 von 6 Configs (typischerweise BNB — der einzige unprofitable Config, wo praktisch
jede Trade-Reduktion die Verluste dämpft, aber das ist kein Beleg für einen echten Edge). Auch
stbots `energy_zscore` (hier "Energy hoch", direkt übernommen) funktioniert bei ltbbot NICHT —
konsistent mit der Hypothese aus der Aufgabenstellung: ein Momentum-Fortsetzungs-Signal ist bei
einem Mean-Reversion-Bot fehl am Platz. Aber auch die Gegenrichtung ("Energy niedrig" = echte
Erschöpfung) hilft nicht, ebensowenig RSI-Extremwerte, Overshoot, Volumen, Wick-Rejection,
ATR-Perzentil oder avalanche_percentile in beide Richtungen.

**Interpretation:** Envelope-Mean-Reversion selektiert durch die Band-Berührung selbst schon auf
"statistisch ungewöhnliche" Kerzen. Ein zusätzlicher Exhaustion-Filter schneidet primär
profitable "moderate" Touches weg, ohne die schlechten gezielt herauszufiltern — die Trade-Zahl
sinkt überall drastisch (bis zu -98%), aber die PnL sinkt real *stärker* als proportional zur
Trade-Zahl in fast allen Fällen. Keiner der 12 Kandidaten (in 28 Schwellenwert-Varianten) zeigt
einen breiten Netto-Vorteil.

---

## Was implementiert wurde

1. **`src/ltbbot/analysis/backtester.py`** — `run_envelope_backtest()`: zwei neue optionale
   `strategy`-Parameter, standardmäßig deaktiviert (Default = altes Verhalten unverändert):
   - `disable_strong_trend_block` (bool, Default `False`) — wenn `True`, wird ein ADX-Wert über
     der Schwelle wie `TREND` statt `STRONG_TREND` behandelt (Trading in Trendrichtung erlaubt,
     1.5×-SL, statt komplett gesperrt).
   - `strong_trend_adx_threshold` (float, Default `30.0`) — die ADX-Schwelle selbst, ebenfalls
     konfigurierbar.
   - Betrifft die per-Kerze-Regime-Berechnung im Backtest-Loop (vormals hartcodiert `> 30`).
2. **`src/ltbbot/strategy/envelope_logic.py`** — `detect_market_regime()`: gleiche zwei Parameter
   als `strategy_params`-Dict-Argument, für Live-Backtest-Konsistenz (identische Interpretation
   wie im Backtester). `calculate_indicators_and_signals()` reicht `params['strategy']` durch.
   Default-Verhalten unverändert, wenn die Keys in einer Config fehlen.
3. **`src/ltbbot/analysis/optimizer.py`** — `objective()`: `disable_strong_trend_block` als
   Optuna-`categorical([True, False])`, `strong_trend_adx_threshold` als conditional
   `suggest_float(20, 40)` (nur wenn Block nicht deaktiviert) — **pro Symbol/Timeframe
   individuell**, nicht global, wegen der BNB-Ausnahme. Wird in `final_params_dict` und damit in
   die gespeicherte Config übernommen.
4. **5 der 6 Config-Dateien direkt aktualisiert** (validierte Verbesserung sofort umgesetzt, da
   niedriges Risiko und eindeutiges Ergebnis):
   `config_AAVEUSDTUSDT_6h_envelope.json`, `config_ADAUSDTUSDT_6h_envelope.json`,
   `config_ETHUSDTUSDT_6h_envelope.json`, `config_LTCUSDTUSDT_6h_envelope.json`,
   `config_XRPUSDTUSDT_6h_envelope.json` — jeweils `"disable_strong_trend_block": true` gesetzt,
   mit `_meta.note` zur Nachvollziehbarkeit. **`config_BNBUSDTUSDT_6h_envelope.json` bewusst NICHT
   verändert** (profitierte im fairen Test von einem strengeren statt lockereren Gate — ein
   manueller Bolt-on ohne vollständigen Re-Optimierungslauf wäre hier nicht sauber begründbar).
   Der nächste reguläre Optuna-Lauf (wöchentlich laut `settings.json`) überschreibt/bestätigt dies
   für alle 6 Symbole automatisch.
5. **`livemodus/trade_forensics.py`** (neu) — Reproduzierbares Forensik-Skript für die 14
   Live-Trades (Band-Match, ADX-Rückrechnung, Exit-Klassifikation, MAE/MFE via 15m-Feindaten).
   Output: `livemodus/trade_forensics_summary.csv`.
6. **`livemodus/fair_filter_validation.py`** (neu) — Reproduzierbares Sweep-Skript für den fairen
   Config-Pool-Test (Regime-Gate + 28 Filter-Varianten). Output:
   `livemodus/fair_filter_validation_results.json` (inkl. Pro-Symbol-Breakdown je Variante).

## Was verworfen wurde (und warum)

Alle 12 Exhaustion-Filter-Kandidaten (28 Schwellenwert-Varianten) — RSI-Extremwert, Band-Overshoot,
Volumen-Klimax, Volumen-Erschöpfung, Wick-Rejection, ATR-Perzentil-Kontraktion, ATR-Perzentil-
Expansion, avalanche_percentile (beide Richtungen), energy_zscore (beide Richtungen), Zeit seit
letztem Touch — wurden **implementiert, fair getestet und danach wieder aus `backtester.py`
entfernt** (kein toter Code), da ausnahmslos alle im fairen Pool-Test netto negativ waren (siehe
Tabelle B oben). Nicht in `optimizer.py` verdrahtet.

## Offene Frage für den User

Keine — beide Backtest-Signale (ADX-Gate-Sweep, Exhaustion-Filter-Sweep) sind eindeutig und
konsistent genug für eine klare Entscheidung. Die einzige echte Nuance ist BNB, die aber bereits
sauber gelöst ist (unverändert gelassen, per-Symbol-Optuna-Parameter statt globaler Hardcode löst
das strukturell für zukünftige Re-Optimierungen).
