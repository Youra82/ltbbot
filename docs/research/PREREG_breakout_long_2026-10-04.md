# Präregistrierung: Long-Ausbruch auf breitem Coin-Universum (2026-10-04)

Festgelegt **vor** dem Lauf. Nachträglich wird nichts geändert.

## Hypothese
Altcoins setzen eine starke Aufwärtsabweichung vom Durchschnitt fort. Ein Long-Einstieg nach einem
Schluss über MA20 + 3·ATR14 ist nach Kosten im Mittel profitabel – auch auf Coins, die nicht
vorab ausgewählt wurden.

## Regel (fest, keine Optimierung)
- Signal: Close(t) > SMA20(t) + 3 · ATR14(t) (Wilder-EWMA)
- Einstieg: Long zum Open(t+1)
- Ausstieg (erstes Ereignis): Stop bei Entry − 3·ATR14(t) (Fill am Stop bzw. am Open bei Gap, +0,05 % Slippage);
  Close < SMA20 → Ausstieg zum nächsten Open; spätestens nach 30 Kerzen zum Close
- Kosten: 0,06 % je Seite. Kein Hebel, Ergebnis in % je Trade. Eine Position je Coin.
- Zeiteinheiten: 6h (Hauptprüfung), 4h und 2h (Nebenprüfung)

## Universum
Alle Bitget-USDT-M-Perpetuals mit Krypto-Basiswert (keine Aktien, Rohstoffe, Indizes) und mindestens
180 Tagen Historie, **ohne** die 43 Coins der Entdeckungsstichprobe. Rückblick bis 3 Jahre (ab 2023-10-04).
Bekannte Restverzerrung: delistete Coins sind nicht abrufbar.

## Erfolgskriterien (6h, alle müssen erfüllt sein)
1. Mittelwert je Trade > 0 nach Kosten, und zwar **vor** und **nach** dem 2025-10-01 jeweils für sich
2. Mehr als 50 % der Coins mit mindestens 3 Trades haben einen positiven Mittelwert
3. Ohne die 5 besten Coins bleibt der Mittelwert > 0
4. Mindestens 300 Trades insgesamt

Erfüllt → nächster Schritt Portfolio-Simulation (Sizing, Funding, Drawdown).
Nicht erfüllt → Hypothese verworfen, kein Live-Einsatz.

## Nachtrag H2 (vor Sichtung der Ergebnisse, 2026-10-04, Vorschlag des Users)
**Hypothese:** Ob Long- oder Short-Ausbrüche funktionieren, hängt vom Gesamtmarkt ab.
- Gesamttrend = BTC-Tagesschluss über/unter seinem SMA200 (Tageskerzen, nur Werte bis zum Vortag des Signals).
- Regel: Long-Ausbruch (wie oben) nur bei BTC > SMA200; Short-Ausbruch (gespiegelt: Close < SMA20 − 3·ATR,
  Short zum nächsten Open, Stop Entry + 3·ATR, Exit bei Close > SMA20 bzw. nach 30 Kerzen) nur bei BTC < SMA200.
- Gleiches Universum, gleiche Kosten, gleiche Erfolgskriterien K1–K4 (für die kombinierte Regel)
  und zusätzlich: die Short-Trades allein haben einen Mittelwert > 0.

## Nachtrag H3 (vor Sichtung der Ergebnisse auf dem breiten Universum, 2026-10-04)
Erkundung NUR auf den 43 Entdeckungs-Coins (exit_explore.py, 22 Ausstiegs-Varianten x 6h/4h/2h).
Robusteste Variante auf allen drei Zeiteinheiten: Band k = 3 ATR, SL 3·ATR, fester TP = 4R (12·ATR).
Engere Stops (1,5 ATR) und das engere Band (2 ATR) waren durchweg schwächer, "Exit beim Rückfall ins Band" am schwächsten.
**H3-Regel:** Long bei Close > SMA20 + 3·ATR14, Entry Open(t+1), SL Entry − 3·ATR, TP Entry + 12·ATR,
spätestens nach 60 Kerzen zum Close. Kosten/Slippage wie oben. Prüfung auf demselben breiten Universum,
Kriterien K1–K4 wie oben, für 6h (Haupt) sowie 4h und 2h.

## Nachtrag H4 (vor Sichtung der Ergebnisse, 2026-10-04, Vorschlag des Users: "shorten, wenn es zum übergeordneten Trend passt")
H3-Ausstieg (SL 3·ATR, TP 12·ATR, max. 60 Kerzen) in BEIDE Richtungen, Band k = 3 ATR:
- Long: Close > SMA20 + 3·ATR und übergeordneter Trend aufwärts
- Short (gespiegelt): Close < SMA20 − 3·ATR und übergeordneter Trend abwärts; SL Entry + 3·ATR, TP Entry − 12·ATR
Zwei Trend-Definitionen, jeweils nur mit Werten bis zur Signalkerze bzw. bis zum Vortag:
- **H4a:** BTC-Tagesschluss vs. SMA200 (Tageskerzen, Vortag)
- **H4b:** der Coin selbst: Close vs. SMA über 200 Tage (in Kerzen der jeweiligen Zeiteinheit, z. B. 800 Kerzen auf 6h)
Kriterien: K1–K4 für die kombinierte Regel, zusätzlich Shorts allein Mittel > 0. Universum wie oben.

## Ergebnisse 6h (breites Universum, 393 Coins, ohne Entdeckungs-Coins)
- H1 (Long, MA-Exit): +1,32 %/Trade, aber nur 37 % der Coins positiv, ohne Top 5 −0,27 % → VERWORFEN
- H3 (Long, SL 3·ATR / TP 12·ATR): +1,60 %/Trade (vor +3,25 % / nach +0,69 %), ohne Top 5 +1,25 %,
  Coins positiv 194/391 = 49,6 % (Grenze 50 %) → VERWORFEN (knapp, nur K2)
- H4a (BTC-SMA200-Filter, beide Richtungen): Longs bei BTC > SMA200 +4,20 % (n=2288), Shorts bei BTC < SMA200 −1,45 % (n=2347)
  → VERWORFEN (Shorts negativ)
- H4b (eigener 200-Tage-Trend): −0,58 % → VERWORFEN

## Nachtrag H5 (nach den 6h-Ergebnissen, VOR Sichtung von 4h/2h auf dem breiten Universum)
Aus H4a abgeleitet, daher nur auf neuen Daten prüfbar: **H3-Long nur bei BTC > SMA200, keine Shorts.**
Bestätigung ausschließlich auf 4h und 2h (breites Universum, Daten noch nicht ausgewertet), Kriterien K1–K4.

## Nachtrag H6 (Idee des Users: Top-Coins als "Dauergast", 2026-10-04, vor Auswertung)
Frage: Sagt die vergangene Leistung eines Coins mit der H3-Long-Regel seine zukünftige Leistung voraus
(wie es die wöchentliche Portfolio-Auswahl voraussetzt)?
Walk-Forward auf den vorhandenen 6h-Trades (breites Universum, H3-Long): Zu jedem Monatsanfang Ranking der Coins
nach Summe der Trade-Ergebnisse der letzten 6 Monate (mindestens 3 Trades), Auswahl Top 20; gezählt werden nur deren
Trades mit Signal im Folgemonat. Vergleich: alle Coins im selben Monat.
Bestätigt, wenn der Mittelwert je Trade der Auswahl über alle Monate höher ist als der aller Coins **und**
in mindestens 60 % der Monate höher ist.

## Ergebnis H6
35 Monate: Top-20-Auswahl besser als alle Coins nur in 14 Monaten (40 %); Mittel je Trade Auswahl +0,88 % (n=469)
vs. alle +1,15 % (n=4610) → **VERWORFEN**. Vergangene Gewinner-Coins bleiben keine Gewinner-Coins; das Ergebnis
hängt stark vom Monat ab (gute Monate = Altcoin-Rallys: 2024-02, 2024-11, 2025-07).

## Ergebnis H2 (6h, MA-Exit, BTC-SMA200-Filter)
Long & BTC>SMA200 +1,97 % (n=2357), Long & BTC<SMA200 +0,74 %, Short & BTC<SMA200 −1,09 % (n=2624),
Short & BTC>SMA200 −2,56 %. Kombiniert +0,36 %, nach 2025-10 −0,09 % → **VERWORFEN**. Shorts verlieren in jedem Regime.

## Ergebnis H5 (frische 4h-Daten, breites Universum) — BESTANDEN
n=3255 Trades, 382 Coins, +2,66 %/Trade, PF 1,47, WR 39 %; vor 2025-10 +2,46 % / nach +3,07 %;
Coins positiv 169/242 (70 %); ohne Top 5 +2,33 %. Zusätzlich H3 (alle Longs, ohne Filter) auf 4h: +1,16 %, bestanden.
2h-Bestätigung folgt.
