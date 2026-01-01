# Telegram-Benachrichtigungen für eingegangene Trades - Update

## Zusammenfassung der Änderungen

Das System wurde erweitert, um **detaillierte Telegram-Benachrichtigungen für tatsächlich eingegangene Trades** zu versenden. 

### Was wurde geändert?

#### 1. Neue Funktion `check_and_notify_new_position()`

Diese Funktion prüft bei jedem Handelszyklus, ob eine **neue Position eröffnet** wurde und sendet sofort eine detaillierte Telegram-Benachrichtigung.

**Funktionsweise:**
- Vergleicht die aktuelle Position mit der zuletzt gemeldeten Position im Tracker
- Erkennt neue Positionen anhand von Entry-Preis und Handelsrichtung
- Sendet nur dann eine Benachrichtigung, wenn es sich um eine **NEUE** Position handelt
- Verhindert doppelte Benachrichtigungen für dieselbe Position

**Benachrichtigungs-Details:**
- 💼 Account-Name
- 📊 Symbol (z.B. BTC/USDT:USDT)
- ⏱ Timeframe (z.B. 4h)
- 📈 Richtung (LONG/SHORT)
- 📦 Positionsgröße in Kontrakten
- 💵 Entry-Preis
- ⚡️ Hebel
- 💰 Verwendete Margin
- 🎯 Take-Profit Preis und Distanz in %
- 🛑 Stop-Loss Preis und Distanz in %
- ⚖️ Risk/Reward Verhältnis
- 📉 Unrealisierter P&L
- ⚠️ Liquidationspreis (falls verfügbar)
- 🕐 Zeitstempel

#### 2. Tracker-System erweitert

Der Tracker speichert nun zusätzliche Informationen:
- `last_notified_entry_price`: Entry-Preis der zuletzt gemeldeten Position
- `last_notified_side`: Seite (long/short) der zuletzt gemeldeten Position
- `last_notified_timestamp`: Zeitstempel der letzten Benachrichtigung

#### 3. Automatische Bereinigung

Die Tracker-Informationen werden automatisch bereinigt wenn:
- Ein Stop-Loss ausgelöst wird
- Ein Take-Profit erreicht wird
- Die Position geschlossen wird
- Der Cooldown-Modus aktiv ist (nach SL)

Dies stellt sicher, dass beim nächsten Trade wieder eine Benachrichtigung gesendet wird.

### Integration in den Handelszyklus

Die neue Funktion wird in der `full_trade_cycle()` Funktion aufgerufen:

```python
if position:
    # Position ist offen -> TP/SL aktualisieren
    manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
    
    # ✨ NEU: Prüfe ob dies eine NEUE Position ist und sende Telegram-Benachrichtigung
    check_and_notify_new_position(exchange, position, params, tracker_file_path, telegram_config, logger)
```

### Vorteile

1. **Sofortige Benachrichtigungen**: Keine Verzögerung bis zum nächsten Zyklus
2. **Detaillierte Informationen**: Alle wichtigen Trade-Details auf einen Blick
3. **Keine Duplikate**: Intelligentes Tracking verhindert mehrfache Benachrichtigungen
4. **Automatische Bereinigung**: System bereitet sich automatisch auf den nächsten Trade vor
5. **Robuste Fehlerbehandlung**: Fehler in der Benachrichtigungsfunktion stoppen nicht den Handelszyklus

### Beispiel-Benachrichtigung

```
🟢 NEUE POSITION ERÖFFNET

💼 Account: Bitget-Account
📊 Symbol: BTC/USDT:USDT
⏱ Timeframe: 4h
📈 Richtung: LONG
📦 Menge: 0.0150 Kontrakte
💵 Entry-Preis: 42350.500000 USDT
⚡️ Hebel: 3x
💰 Margin verwendet: 212.50 USDT
🎯 Take-Profit: 43200.000000 USDT (+2.01%)
🛑 Stop-Loss: 41505.000000 USDT (-2.00%)
⚖️ Risk/Reward: 1:2.01

📉 Unreal. P&L: 0.00 USDT
⚠️ Liquidation: 38500.250000 USDT

🕐 Zeit: 2026-01-01 14:25:30
```

### Wichtige Hinweise

- Die Funktion prüft **nur tatsächlich eingegangene Trades** (offene Positionen)
- Es werden **keine Benachrichtigungen** für platzierte Trigger-Orders gesendet
- Die Benachrichtigung erfolgt im **nächsten Handelszyklus** nach der Positionseröffnung
- Bei sehr schnellen Zyklen kann es zu minimalen Verzögerungen kommen

### Konfiguration

Stelle sicher, dass in deiner Konfiguration die Telegram-Details korrekt eingetragen sind:

```json
{
  "telegram": {
    "bot_token": "DEIN_BOT_TOKEN",
    "chat_id": "DEINE_CHAT_ID"
  }
}
```

### Logs

Die Funktion loggt folgende Informationen:
- ✅ Erfolgreiche Benachrichtigungen mit Trade-Details
- 🔍 Debug-Logs wenn Position bereits gemeldet wurde
- ⚠️ Warnungen bei fehlenden TP/SL-Informationen
- ❌ Fehler bei Problemen mit der Benachrichtigung

### Datei-Änderungen

Geänderte Datei:
- `src/ltbbot/utils/trade_manager.py`
  - Neue Funktion `check_and_notify_new_position()` hinzugefügt
  - Aufruf in `full_trade_cycle()` integriert
  - Tracker-Bereinigung in `check_stop_loss_trigger()` erweitert
  - Tracker-Bereinigung in `check_take_profit_trigger()` erweitert
  - Tracker-Bereinigung im Cooldown-Block hinzugefügt

### Testing

Um die Änderungen zu testen:

1. Starte den Bot normal mit `python master_runner.py`
2. Warte bis eine Entry-Order ausgelöst wird
3. Im nächsten Zyklus sollte eine detaillierte Telegram-Benachrichtigung erscheinen
4. Die Logs zeigen: `✅ Telegram-Benachrichtigung für NEUE Position gesendet...`

### Troubleshooting

**Keine Benachrichtigung erhalten?**
- Prüfe die Telegram-Konfiguration (Bot-Token und Chat-ID)
- Prüfe die Logs auf Fehler bei der Benachrichtigung
- Stelle sicher, dass der Bot die Position korrekt erkennt
- Prüfe den Tracker: `artifacts/tracker/SYMBOL_TIMEFRAME.json`

**Doppelte Benachrichtigungen?**
- Dies sollte nicht passieren, aber falls doch: Prüfe ob der Tracker korrekt aktualisiert wird
- Lösche ggf. die Tracker-Datei und starte neu

**Benachrichtigung fehlt Details?**
- Die Funktion versucht TP/SL-Preise aus offenen Orders zu holen
- Falls diese nicht verfügbar sind, wird "Nicht gefunden" angezeigt
- Dies ist normal und beeinträchtigt nicht die Funktionalität

---

**Datum:** 01.01.2026
**Version:** 2.0+
**Autor:** GitHub Copilot
