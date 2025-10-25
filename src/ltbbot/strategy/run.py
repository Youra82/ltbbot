# src/ltbbot/strategy/run.py
import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import argparse
import ccxt # Behalten für ccxt Exceptions

# --- Pfad Setup ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# --- ltbbot Imports ---
from ltbbot.utils.exchange import Exchange
from ltbbot.utils.telegram import send_message
# ANN Model entfernt
from ltbbot.utils.trade_manager import full_trade_cycle # Haupt-Trading-Logik
from ltbbot.utils.guardian import guardian_decorator # Wichtig für Fehlerbehandlung

# --- Logging Setup ---
def setup_logging(symbol, timeframe):
    """Konfiguriert das Logging für eine spezifische Strategie-Instanz."""
    safe_filename = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    log_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'ltbbot_{safe_filename}.log')

    # Eindeutiger Logger-Name pro Instanz
    logger_name = f'ltbbot_{safe_filename}'
    logger = logging.getLogger(logger_name)

    # Verhindert doppelte Handler, wenn run.py neu gestartet wird
    if not logger.handlers:
        logger.setLevel(logging.INFO) # Oder DEBUG für mehr Details

        # File Handler ( rotiert bei 5MB, behält 3 Backups)
        fh = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        fh_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(fh_formatter)
        logger.addHandler(fh)

        # Console Handler (zeigt Logs auch im Terminal an)
        ch = logging.StreamHandler()
        # Unterschiedliches Format für Konsole für bessere Lesbarkeit
        ch_formatter = logging.Formatter(f'%(asctime)s [{safe_filename}] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
        ch.setFormatter(ch_formatter)
        logger.addHandler(ch)

        # Verhindert, dass Logs an Root-Logger weitergegeben werden
        logger.propagate = False

    return logger

# --- Konfiguration laden ---
def load_config(symbol, timeframe):
    """Lädt die spezifische JSON-Konfigurationsdatei für die Strategie."""
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    # Dateiname basierend auf Symbol und Zeitrahmen (mit Suffix)
    safe_filename_base = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    config_filename = f"config_{safe_filename_base}_envelope.json" # Fester Suffix für Envelope
    config_path = os.path.join(configs_dir, config_filename)

    if not os.path.exists(config_path):
        # Versuche Fallback ohne Suffix (falls alte Configs existieren)
        config_filename_fallback = f"config_{safe_filename_base}.json"
        config_path_fallback = os.path.join(configs_dir, config_filename_fallback)
        if os.path.exists(config_path_fallback):
             config_path = config_path_fallback
             logging.warning(f"Verwende Fallback-Konfigurationsdatei: {config_filename_fallback}")
        else:
             raise FileNotFoundError(f"Konfigurationsdatei '{config_filename}' (oder Fallback) nicht gefunden in {configs_dir}")

    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            # Validierung der Config (optional, aber empfohlen)
            if 'market' not in config or 'strategy' not in config or 'risk' not in config:
                 raise ValueError("Konfigurationsdatei ist unvollständig (market, strategy, risk fehlen).")
            return config
    except json.JSONDecodeError as e:
        raise ValueError(f"Fehler beim Parsen der Konfigurationsdatei '{config_filename}': {e}")


# --- Dekorierte Hauptfunktion für eine Account/Strategie-Kombination ---
@guardian_decorator # Fängt kritische Fehler ab und sendet Telegram-Nachricht
def run_for_account(account, telegram_config, params, logger):
    """
    Führt den Handelszyklus für einen Account und eine Strategie aus.
    Wird vom Guardian Decorator umschlossen.
    """
    account_name = account.get('name', 'Standard-Account')
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']

    logger.info(f"--- Starte ltbbot (Envelope) für {symbol} ({timeframe}) auf Account '{account_name}' ---")

    try:
        exchange = Exchange(account) # Erstellt die Exchange-Verbindung

        # Haupt-Trading-Schleife (könnte in eine Endlosschleife mit sleep gepackt werden)
        # Für den Anfang: Ein einzelner Durchlauf pro Ausführung von run.py
        # Der master_runner.py kümmert sich um den Neustart/die Überwachung
        full_trade_cycle(exchange, params, telegram_config, logger)

    except ccxt.AuthenticationError:
        logger.critical("!!! Authentifizierungsfehler! API-Schlüssel prüfen !!!")
        # Guardian sollte dies fangen, aber zusätzliche Logs schaden nicht
        raise # Fehler weitergeben, damit Guardian ihn sieht
    except ccxt.NotSupported as e:
         logger.critical(f"Funktion nicht unterstützt von der Börse oder ccxt: {e}")
         raise
    except Exception as e:
        logger.error(f"Unerwarteter Fehler in run_for_account für {symbol} ({timeframe}): {e}", exc_info=True)
        raise # Fehler weitergeben


# --- Main Execution Block ---
def main():
    parser = argparse.ArgumentParser(description="ltbbot Envelope Trading-Skript")
    parser.add_argument('--symbol', required=True, type=str, help="Handelspaar (z.B. BTC/USDT:USDT)")
    parser.add_argument('--timeframe', required=True, type=str, help="Zeitrahmen (z.B. 1h)")
    # Kein --use_macd mehr
    args = parser.parse_args()

    symbol = args.symbol
    timeframe = args.timeframe

    # Logger spezifisch für dieses Symbol/Zeitrahmen-Paar initialisieren
    logger = setup_logging(symbol, timeframe)

    try:
        # Lade die passende Konfiguration
        params = load_config(symbol, timeframe)
        logger.info(f"Konfiguration geladen für {symbol} ({timeframe}).")

        # Lade Secrets (API Keys und Telegram Info)
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), "r") as f:
            secrets = json.load(f)
        accounts_to_run = secrets.get('ltbbot', []) # Verwende den korrekten Key 'ltbbot'
        telegram_config = secrets.get('telegram', {})

        if not accounts_to_run:
             logger.critical("Keine Account-Konfigurationen unter 'ltbbot' in secret.json gefunden.")
             sys.exit(1)

        # ANN Modell/Scaler Laden entfernt

    except FileNotFoundError as e:
        logger.critical(f"Kritische Datei nicht gefunden: {e}")
        sys.exit(1)
    except ValueError as e:
         logger.critical(f"Fehler in Konfiguration: {e}")
         sys.exit(1)
    except Exception as e:
        logger.critical(f"Kritischer Initialisierungs-Fehler: {e}", exc_info=True)
        sys.exit(1)

    # Führe den Bot für jeden konfigurierten Account aus
    # Normalerweise wird nur ein Account pro Strategie-Instanz verwendet, aber die Struktur erlaubt mehrere
    for account in accounts_to_run:
        try:
            # Hier wird die dekorierte Funktion aufgerufen
            run_for_account(account, telegram_config, params, logger)
        except Exception as e:
             # Der Guardian fängt das meiste ab, aber zur Sicherheit loggen wir hier auch
             logger.error(f"Schwerwiegender Fehler beim Ausführen für Account {account.get('name', 'Unbenannt')}: {e}", exc_info=True)
             # Beende den Prozess bei einem Fehler in dieser Instanz, master_runner startet ihn neu
             sys.exit(1) # Beendet diesen spezifischen run.py Prozess

    logger.info(f">>> ltbbot-Lauf für {symbol} ({timeframe}) normal abgeschlossen <<<")

if __name__ == "__main__":
    main()
