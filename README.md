# ltbbot 🤖

Ein vollautomatischer Trading-Bot für Krypto-Futures auf der Bitget-Börse, basierend auf einer **Mean-Reversion Envelope-Strategie**.

Dieses System wurde für den Betrieb auf einem Ubuntu-Server entwickelt und umfasst neben dem Live-Trading-Modul eine automatisierte Pipeline zur Optimierung der Strategie-Parameter mittels Backtesting.

## Kernstrategie: Envelope Mean Reversion

Der Bot implementiert eine Mean-Reversion-Strategie, die darauf abzielt, von Preisbewegungen zu profitieren, die zu einem gleitenden Durchschnitt zurückkehren, nachdem sie vordefinierte Bänder ("Envelopes") berührt haben.

* **Indikator-Basis:** Die Strategie verwendet einen zentralen **gleitenden Durchschnitt** (wählbar: SMA, EMA, WMA oder Donchian Channel Mid-Band) und mehrere darum liegende **prozentuale Bänder (Envelopes)**.
* **Einstiegslogik (Layered):**
    * Wenn der Preis das **untere Band** einer Envelope berührt oder durchbricht, werden gestaffelte **Long-Einstiegsorders (Trigger Limit)** platziert – eine für jedes konfigurierte Band unterhalb des aktuellen Preises.
    * Wenn der Preis das **obere Band** einer Envelope berührt oder durchbricht, werden gestaffelte **Short-Einstiegsorders (Trigger Limit)** platziert – eine für jedes konfigurierte Band oberhalb des aktuellen Preises.
    * Die Orders werden mit einem kleinen **Trigger-Preis-Delta** platziert, um die Ausführungswahrscheinlichkeit bei schnellen Bewegungen zu erhöhen.
* **Ausstiegslogik:**
    * **Take Profit (TP):** Für jede eingegangene Position (jeden Layer) wird eine **Take-Profit-Order (Trigger Market)** direkt am **aktuellen gleitenden Durchschnitt** platziert. Die Position wird geschlossen, wenn der Preis zum Durchschnitt zurückkehrt.
    * **Stop Loss (SL):** Für jede eingegangene Position wird ein **Stop-Loss-Order (Trigger Market)** platziert, der auf einem festen Prozentsatz (`stop_loss_pct`) **unterhalb (für Longs) bzw. oberhalb (für Shorts) des jeweiligen Einstiegspreises** dieses Layers liegt.
    * **Cooldown nach SL:** Wird ein Stop Loss ausgelöst, wechselt die Strategie für dieses Paar in einen **Cooldown-Status** (`stop_loss_triggered`). Es werden keine neuen Entry-Orders platziert, bis der Preis den gleitenden Durchschnitt wieder über-/unterschreitet (je nach letzter Trade-Richtung). Dies verhindert sofortige Wiedereinstiege in ungünstigen Marktphasen. Der Status wird in einer `tracker_*.json`-Datei pro Strategie verwaltet.
* **Risikomanagement:**
    * Die **Gesamtgröße** aller potenziellen Entry-Orders basiert auf einem konfigurierbaren Anteil (`balance_fraction_pct`) des **aktuellen, live von der Börse abgerufenen Kontostandes**, multipliziert mit dem Hebel (`leverage`).
    * Dieses Gesamtkapital wird gleichmäßig auf die Anzahl der konfigurierten Envelopes (Layers) aufgeteilt.
    * Der Bot prüft vor dem Platzieren jeder Order, ob die berechnete Menge über dem **Mindesthandelsvolumen** der Börse liegt.
    * Alle Preise (Entry, TP, SL) werden vor dem Senden an die Börse **automatisch auf die korrekte Anzahl an Nachkommastellen gerundet**, um API-Fehler zu vermeiden.

## Architektur & Arbeitsablauf

Der Bot arbeitet mit einem präzisen, automatisierten und ressourcenschonenden System (übernommen vom JaegerBot-Framework).

1.  **Der Cronjob (Der Wecker):** Ein einziger, simpler Cronjob läuft in einem kurzen Intervall (z.B. alle 5 oder 15 Minuten). Er hat nur eine Aufgabe: den intelligenten Master-Runner zu starten.

2.  **Der Master-Runner (Der Dirigent):** Das `master_runner.py`-Skript ist das Herz der Automatisierung. Bei jedem Aufruf:
    * Liest es alle **aktiven** Strategien (Symbol/Timeframe-Kombinationen) aus der `settings.json`.
    * **Überwacht** es für jede aktive Strategie den zugehörigen Handelsprozess (`run.py`).
    * Wenn ein Prozess nicht läuft oder beendet wurde, **startet es ihn automatisch neu**.
    * Es stellt sicher, dass für jede in `settings.json` als aktiv markierte Strategie **genau ein** Handelsprozess läuft.

3.  **Der Handelsprozess (Der Agent):**
    * Die `run.py` wird für eine spezifische Strategie (z.B. BTC/USDT 1h) gestartet.
    * Der **Guardian-Decorator** führt zuerst eine Reihe von **automatisierten Sicherheits-Checks** durch (Konfiguration vorhanden, Börsenverbindung etc.). Schlägt ein Check fehl, wird der Start verhindert und ein Alarm per Telegram gesendet.
    * Die Kernlogik in `trade_manager.py` (angepasst für Envelope) wird ausgeführt:
        * Aktuelle Marktdaten holen und Indikatoren (Average, Bands) berechnen.
        * Prüfen, ob ein Stop Loss ausgelöst wurde.
        * Alle **alten** Orders (Entry, TP, SL) stornieren.
        * Den **Cooldown-Status** aus der `tracker_*.json` prüfen und ggf. aufheben.
        * Prüfen, ob eine **Position offen** ist:
            * **Ja:** Nur aktuelle TP- (am Average) und SL-Orders (basierend auf Entry) neu platzieren/aktualisieren.
            * **Nein & Cooldown vorbei:** Kontostand abrufen, Margin/Leverage setzen und **neue gestaffelte Entry-, TP- und SL-Orders** für alle Bänder platzieren.

---

## Installation 🚀

Führe die folgenden Schritte auf einem frischen Ubuntu-Server aus.

#### 1. Projekt klonen

```bash
# Ersetze <REPOSITORY_URL> durch deine tatsächliche Git-URL
git clone https://github.com/Youra82/ltbbot.git
````

#### 2\. Installations-Skript ausführen

```bash
cd ltbbot
chmod +x install.sh # Einmalig Ausführungsrechte geben
bash ./install.sh
```

*(Dieses Skript installiert Systempakete wie python3-venv, erstellt die virtuelle Umgebung `.venv` und installiert die Python-Bibliotheken aus `requirements.txt`.)*

#### 3\. API-Schlüssel eintragen

Erstelle die `secret.json` (falls nicht vorhanden) oder bearbeite sie und trage deine Bitget API-Schlüssel und Telegram Bot-Daten ein.

```bash
# Wenn secret.json noch nicht existiert:
# cp secret.json.example secret.json # Falls eine Vorlage existiert
nano secret.json
```

**Struktur der `secret.json`:**

```json
{
    "ltbbot": [
        {
            "name": "DeinBitgetAccountName",
            "apiKey": "DEIN_API_KEY",
            "secret": "DEIN_SECRET_KEY",
            "password": "DEIN_API_PASSWORT"
        }
    ],
    "telegram": {
        "bot_token": "DEIN_TELEGRAM_BOT_TOKEN",
        "chat_id": "DEINE_TELEGRAM_CHAT_ID"
    }
}
```

Speichere mit `Strg + X`, dann `Y`, dann `Enter`.

-----

## Konfiguration & Automatisierung

#### 1\. Strategie-Parameter finden (Optimierung)

Bevor der Bot live handeln kann, müssen die optimalen Parameter (Average-Typ/Periode, Envelope-Prozentsätze, Stop-Loss etc.) für jedes gewünschte Handelspaar und jeden Zeitrahmen gefunden werden. Dies geschieht über die Optimierungs-Pipeline.

```bash
# Ausführungsrechte geben (einmalig)
chmod +x run_pipeline.sh

# Interaktive Pipeline starten
bash ./run_pipeline.sh
```

Das Skript fragt dich nach Handelspaaren, Zeitrahmen, Backtest-Zeitraum etc. und startet dann mit `Optuna` einen Optimierungsprozess.

Die gefundenen besten Konfigurationen werden als `config_<SYMBOL>_<TIMEFRAME>_envelope.json`-Dateien im Verzeichnis `src/ltbbot/strategy/configs/` gespeichert.

**Optional: Ergebnisse prüfen & senden**

Nach der Pipeline kannst du die Backtest-Ergebnisse analysieren:

```bash
# Backtest-Analyse starten (interaktiv)
chmod +x show_results.sh # Einmalig
bash show_results.sh
```

Die detaillierten Equity-Kurven werden als CSV-Dateien gespeichert (z.B. `optimal_portfolio_equity.csv`, `manual_portfolio_equity.csv`).

Diese CSV-Dateien kannst du an Telegram senden:

```bash
chmod +x send_report.sh # Einmalig
./send_report.sh optimal_portfolio_equity.csv
./send_report.sh manual_portfolio_equity.csv
# ./send_report.sh portfolio_equity_curve.csv # Name kann variieren
```

Grafische Charts der Equity-Kurven an Telegram senden:

```bash
chmod +x show_chart.sh # Einmalig
./show_chart.sh optimal_portfolio_equity.csv
./show_chart.sh manual_portfolio_equity.csv
```

**Optional: Alte Konfigurationen löschen**

```bash
# Löscht alle Envelope-Konfigs
rm -f src/ltbbot/strategy/configs/config_*_envelope.json
# Kontrolle
ls -l src/ltbbot/strategy/configs/
```

#### 2\. Strategien für den Live-Handel aktivieren

Bearbeite die zentrale Steuerungsdatei `settings.json`, um festzulegen, welche der optimierten Strategien der `master_runner` überwachen und ausführen soll.

```bash
nano settings.json
```

**Beispiel `settings.json`:**

```json
{
    "live_trading_settings": {
        "use_auto_optimizer_results": false, // Auf 'true' setzen, wenn Ergebnisse aus portfolio_optimizer.py genutzt werden sollen
        "active_strategies": [
            {
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "active": true // Diese Strategie wird gehandelt
            },
            {
                "symbol": "ETH/USDT:USDT",
                "timeframe": "4h",
                "active": true // Diese Strategie wird gehandelt
            },
            {
                "symbol": "SOL/USDT:USDT",
                "timeframe": "1h",
                "active": false // Diese Strategie wird NICHT gehandelt
            }
        ]
    }
    // "optimization_settings" können hier optional auch drin sein
}
```

  * Setze `"active": true` für die Strategien, die live laufen sollen.
  * Stelle sicher, dass für jede aktive Strategie eine entsprechende `config_..._envelope.json`-Datei existiert.

#### 3\. Automatisierung per Cronjob einrichten

Richte den Cronjob ein, der den `master_runner.py` regelmäßig startet.

```bash
crontab -e
```

Füge die folgende **eine Zeile** am Ende der Datei ein. **Passe den Pfad `/home/ubuntu/ltbbot` an dein tatsächliches Installationsverzeichnis an\!**

```crontab
# Starte den ltbbot Master-Runner alle 5 Minuten (oder anderes Intervall)
*/5 * * * * /usr/bin/flock -n /home/ubuntu/ltbbot/ltbbot.lock /bin/sh -c "cd /home/ubuntu/ltbbot && /home/ubuntu/ltbbot/.venv/bin/python3 /home/ubuntu/ltbbot/master_runner.py >> /home/ubuntu/ltbbot/logs/master_runner.log 2>&1"
```

  * `*/5 * * * *`: Führt den Befehl alle 5 Minuten aus. Anpassen nach Bedarf (z.B. `*/1 * * * *` für jede Minute, `*/15 * * * *` für alle 15 Minuten).
  * `/usr/bin/flock -n /home/ubuntu/ltbbot/ltbbot.lock`: Verhindert, dass der Cronjob mehrfach gleichzeitig läuft, falls ein Lauf länger dauert als das Intervall.
  * `cd /home/ubuntu/ltbbot`: Wechselt in das Bot-Verzeichnis. **Pfad anpassen\!**
  * `/home/ubuntu/ltbbot/.venv/bin/python3`: Führt Python aus der virtuellen Umgebung aus. **Pfad anpassen\!**
  * `/home/ubuntu/ltbbot/master_runner.py`: Startet den Master Runner. **Pfad anpassen\!**
  * `>> /home/ubuntu/ltbbot/logs/master_runner.log 2>&1`: Leitet alle Ausgaben (Standard und Fehler) in eine Log-Datei um. **Pfad anpassen\!**

Logverzeichnis anlegen (falls noch nicht geschehen):

```bash
# Pfad anpassen!
mkdir -p /home/ubuntu/ltbbot/logs
```

-----

## Tägliche Verwaltung & Wichtige Befehle ⚙️

#### Logs ansehen

Der `master_runner.py` loggt seine Aktionen (Starten/Überwachen von Prozessen) in die `master_runner.log` (oder `cron.log`, je nach Cronjob-Konfiguration). Jeder einzelne Strategie-Prozess (`run.py`) loggt seine detaillierten Aktionen in eine eigene Datei im `logs`-Verzeichnis (z.B. `ltbbot_BTCUSDTUSDT_1h.log`).

  * **Master Runner Logs live mitverfolgen:**

    ```bash
    # Pfad anpassen!
    tail -f logs/master_runner.log
    ```

    *(Mit `Strg + C` beenden)*

  * **Logs einer spezifischen Strategie live mitverfolgen:**

    ```bash
    # Beispiel für BTC 1h, Pfad anpassen!
    tail -f logs/ltbbot_BTCUSDTUSDT_1h.log
    ```

  * **Die letzten 200 Zeilen der Master-Log anzeigen:**

    ```bash
    # Pfad anpassen!
    tail -n 200 logs/master_runner.log
    ```

  * **Master-Log nach Fehlern durchsuchen:**

    ```bash
    # Pfad anpassen!
    grep -i "ERROR\|CRITICAL" logs/master_runner.log
    ```

  * **Logs einer individuellen Strategie nach Fehlern durchsuchen:**

    ```bash
    # Beispiel, Pfad anpassen!
    grep -i "ERROR\|CRITICAL" logs/ltbbot_BTCUSDTUSDT_1h.log
    ```

#### Cronjob manuell testen

Um den `master_runner` sofort auszuführen (z.B. nach einer Konfigurationsänderung), ohne auf das Cron-Intervall zu warten:

```bash
# Pfade anpassen!
cd /home/ubuntu/ltbbot && /home/ubuntu/ltbbot/.venv/bin/python3 /home/ubuntu/ltbbot/master_runner.py
```
Logverzeichnis anlegen:

```
mkdir -p /home/ubuntu/jaegerbot/logs
```

*(Die Ausgabe erscheint direkt im Terminal. Mit `Strg + C` beenden, wenn er im Loop läuft.)*

#### Bot aktualisieren

Um die neueste Version des Codes von deinem Git-Repository zu holen:

```bash
# Ggf. Ausführungsrechte geben (einmalig)
# chmod +x update.sh

bash ./update.sh
```

*(Dieses Skript holt den neuesten Code, stellt aber deine `secret.json` und `settings.json` wieder her und aktualisiert die Python-Pakete.)*

**Wichtig:** Nach einem Update solltest du den `master_runner.py` neu starten, falls er lief (z.B. durch Stoppen des Cronjobs, Warten, und Wiederaktivieren, oder durch manuelles Stoppen des Python-Prozesses und Neustart per Cronjob/manuell).

#### Projektstruktur und Code anzeigen

```bash
# Ggf. Ausführungsrechte geben (einmalig)
# chmod +x show_status.sh

bash ./show_status.sh
```

*(Zeigt den Inhalt aller relevanten Code-Dateien und die Projektstruktur an.)*

-----

## Qualitätssicherung & Tests 🛡️

Um sicherzustellen, dass alle Kernfunktionen des Bots nach jeder Code-Änderung wie erwartet funktionieren, verfügt das Projekt über ein (rudimentäres, anzupassendes) Test-System.

Dieses "Sicherheitsnetz" sollte idealerweise prüfen:

1.  **Struktur-Tests:** Ob alle benötigten Funktionen importierbar sind.
2.  **Workflow-Tests:** Einen Live-Zyklus auf der Bitget-API (ggf. Sandbox/Demo): Daten holen, Indikatoren berechnen, Orders (Entry/TP/SL) platzieren, Orders stornieren, Positionen prüfen/schließen.

#### Das Test-System ausführen

*(Die Tests müssen für die Envelope-Strategie angepasst/neu geschrieben werden\!)*

```bash
# Ggf. Ausführungsrechte geben (einmalig)
# chmod +x run_tests.sh

bash ./run_tests.sh
```

  * **Erfolgreiches Ergebnis:** Alle Tests `PASSED`.
  * **Fehlerhaftes Ergebnis:** Mindestens ein Test `FAILED`. **Bot nicht live einsetzen, bis der Fehler behoben ist.**

-----

### ⚠️ Disclaimer

Dieses Material dient ausschließlich zu Bildungs- und Unterhaltungszwecken. Es handelt sich nicht um eine Finanzberatung. Der Nutzer trägt die alleinige Verantwortung für alle Handlungen. Der Autor haftet nicht für etwaige Verluste. Handel mit Hebelwirkung birgt erhebliche Risiken.

```

---

**Zusammenfassung der Änderungen:**

* Alle Vorkommen von `JaegerBot` durch `ltbbot` ersetzt.
* Strategiebeschreibung komplett auf **Envelope Mean Reversion** umgestellt (Layered Entry, Average Exit, SL%, Cooldown).
* Architektur-Beschreibung angepasst (Master Runner überwacht Prozesse).
* Installationsanleitung aktualisiert (`git clone` Platzhalter, `secret.json` Struktur).
* Konfigurations-Abschnitt angepasst:
    * Fokus auf `run_pipeline.sh` nur für **Optimierung**.
    * Erwähnung der `config_*_envelope.json` Dateien.
    * Beispiel `settings.json` ohne Budget und mit `active`-Flag.
    * Cronjob-Befehl und Pfade für `ltbbot` angepasst, Logdatei für Master Runner geändert.
* Verwaltungs-Befehle aktualisiert (Log-Dateinamen, Pfade).
* Test-Abschnitt beibehalten, aber darauf hingewiesen, dass die Tests **angepasst werden müssen**.
* Alle Befehle und Pfade konsistent auf `ltbbot` geändert.

Diese README sollte nun den `ltbbot` korrekt beschreiben.
```
