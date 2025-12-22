# /root/ltbbot/tests/test_workflow.py
import pytest
import os
import sys
import json
import logging
import time
from unittest.mock import patch, MagicMock

# Füge das src-Verzeichnis zum Python-Pfad hinzu
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange
# Importiere direkt die Funktion zum Platzieren der Entry-Orders
from ltbbot.utils.trade_manager import place_entry_orders, cancel_strategy_orders

# Globaler Logger für Tests
test_logger = logging.getLogger("test-ltbbot-workflow")
test_logger.setLevel(logging.INFO)
if not test_logger.handlers:
    test_logger.addHandler(logging.StreamHandler(sys.stdout))

@pytest.fixture(scope="module")
def test_setup():
    """Bereitet die Testumgebung vor (Exchange) und räumt danach auf."""
    print("\n--- Starte LIVE ltbbot-Order-Platzierungs-Test ---")
    print("\n[Setup] Bereite Testumgebung vor...")

    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip("secret.json nicht gefunden. Überspringe Live-Workflow-Test.")

    with open(secret_path, 'r') as f:
        secrets = json.load(f)

    # *** Geändert: Suche nach 'ltbbot' Schlüssel ***
    if not secrets.get('ltbbot') or not secrets['ltbbot']:
        pytest.skip("Es wird mindestens ein Account unter 'ltbbot' in secret.json für den Workflow-Test benötigt.")

    test_account = secrets['ltbbot'][0]
    telegram_config = secrets.get('telegram', {}) # Wird hier nicht direkt gebraucht, aber für Vollständigkeit

    try:
        exchange = Exchange(test_account)
        if not exchange.markets:
            pytest.fail("Exchange konnte nicht initialisiert werden (Märkte nicht geladen).")
    except Exception as e:
        pytest.fail(f"Exchange konnte nicht initialisiert werden: {e}")

    # Test-Symbol und eine Beispiel-Konfiguration für Envelope
    symbol = 'PEPE/USDT:USDT' # Wähle ein Symbol, das du testen möchtest
    params = {
        'market': {'symbol': symbol, 'timeframe': '5m'}, # Zeitrahmen ist hier weniger kritisch
        'strategy': {
            'average_type': 'EMA',
            'average_period': 10,
            'envelopes': [0.01, 0.02, 0.03], # Beispiel-Bänder (1%, 2%, 3%)
            'trigger_price_delta_pct': 0.05
        },
        'risk': {
            'margin_mode': 'isolated',
            'risk_per_entry_pct': 0.1, # SEHR KLEINES Risiko für den Test!
            'leverage': 5,            # Niedriger Hebel für den Test!
            'stop_loss_pct': 1.0        # Beispiel SL (1%)
        },
        'behavior': {'use_longs': True, 'use_shorts': True},
        'initial_capital_live': 1000 # Annahme für die Risikoberechnung
    }

    print("-> Führe initiales Aufräumen durch (storniere alle Orders)...")
    try:
        cancel_strategy_orders(exchange, symbol, test_logger)
        time.sleep(2) # Warte kurz, damit Orders sicher weg sind
        # Prüfe, ob noch Positionen offen sind (sollten nicht sein, aber sicher ist sicher)
        pos_check = exchange.fetch_open_positions(symbol)
        if pos_check:
             pytest.fail(f"FEHLER: Unerwartete offene Position für {symbol} vor dem Test gefunden!")
        print("-> Ausgangszustand ist sauber (keine Orders/Positionen).")
    except Exception as e:
        pytest.fail(f"Fehler beim initialen Aufräumen: {e}")

    yield exchange, params, symbol, telegram_config # Gib benötigte Objekte an den Test weiter

    # --- Teardown ---
    print("\n[Teardown] Räume nach dem Test auf...")
    try:
        print("-> Lösche alle Orders für das Test-Symbol...")
        cancel_strategy_orders(exchange, symbol, test_logger)
        time.sleep(2)
        print("-> Aufräumen abgeschlossen.")
    except Exception as e:
        print(f"FEHLER beim Aufräumen nach dem Test: {e}")


def test_place_entry_orders_on_bitget(test_setup):
    """
    Testet die `place_entry_orders`-Funktion, indem fiktive Bandpreise
    simuliert und geprüft wird, ob die entsprechenden Orders (Entry, TP, SL)
    an die Börse gesendet werden.
    """
    exchange, params, symbol, telegram_config = test_setup

    print("\n[Schritt 1/2] Simuliere Bandpreise und rufe place_entry_orders auf...")

    # Simuliere aktuelle Bandpreise (z.B. basierend auf dem aktuellen Ticker)
    try:
        ticker = exchange.fetch_ticker(symbol)
        if not ticker or 'last' not in ticker:
            pytest.fail("Konnte Ticker nicht abrufen, um Bandpreise zu simulieren.")
        current_price = ticker['last']

        # Fiktive Bandpreise für den Test (z.B. 1%, 2%, 3% unter/über dem Preis)
        simulated_band_prices = {
            'average': current_price * 1.005, # Simulierter Average leicht drüber
            'long': [
                current_price * (1 - params['strategy']['envelopes'][0]),
                current_price * (1 - params['strategy']['envelopes'][1]),
                current_price * (1 - params['strategy']['envelopes'][2])
            ],
            'short': [
                current_price * (1 + params['strategy']['envelopes'][0]),
                current_price * (1 + params['strategy']['envelopes'][1]),
                current_price * (1 + params['strategy']['envelopes'][2])
            ]
        }
        # Simulierter Kontostand für die Funktion
        simulated_balance = 50.0 # Niedriger Wert für Test

        # Tracker-Datei Pfad (wird von der Funktion intern verwendet)
        tracker_file_path = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker', f"{symbol.replace('/', '-').replace(':', '-')}_{params['market']['timeframe']}.json")
        # Sicherstellen, dass die Datei existiert oder erstellt werden kann
        os.makedirs(os.path.dirname(tracker_file_path), exist_ok=True)
        if os.path.exists(tracker_file_path): os.remove(tracker_file_path) # Alte löschen

        # Rufe die Funktion auf, die die Orders platzieren soll
        # Füge telegram_config als Argument hinzu
        place_entry_orders(exchange, simulated_band_prices, params, simulated_balance, tracker_file_path, telegram_config, test_logger)

        print("-> place_entry_orders aufgerufen. Warte 5s auf Order-Platzierung...")
        time.sleep(5)

    except Exception as e:
        pytest.fail(f"Fehler während des Aufrufs von place_entry_orders: {e}")

    print("\n[Schritt 2/2] Überprüfe, ob die Trigger-Orders erstellt wurden...")
    try:
        # Erwarte Trigger-Orders: 3x Long Entry, 3x Long TP, 3x Long SL, 3x Short Entry, 3x Short TP, 3x Short SL = 18 Orders
        # ABER: TP/SL werden *NACH* Entry ausgelöst. Da hier kein Entry stattfindet, sollten nur Entry-Trigger erstellt werden?
        # NEIN: Die Logik in place_entry_orders platziert Entry, TP und SL direkt nacheinander.
        # Es sollten also pro Band 3 Orders erstellt werden (Entry Trigger Limit, TP Trigger Market, SL Trigger Market)
        # Für Longs und Shorts, also 3 Bänder * 3 Order-Typen * 2 Richtungen = 18 Trigger Orders.

        open_trigger_orders = exchange.fetch_open_trigger_orders(symbol)
        expected_orders = len(params['strategy']['envelopes']) * 3 * 2 # Bänder * (Entry+TP+SL) * (Long+Short)

        print(f"-> Erwartete Trigger Orders: {expected_orders}")
        print(f"-> Gefundene Trigger Orders: {len(open_trigger_orders)}")

        # Gib Details der gefundenen Orders aus (optional für Debugging)
        # for order in open_trigger_orders:
        #    print(f"  - ID: {order['id']}, Side: {order['side']}, Type: {order.get('type')}, Trigger: {order.get('stopPrice')}, Limit: {order.get('price')}")


        assert len(open_trigger_orders) == expected_orders, f"FEHLER: Falsche Anzahl an offenen Trigger-Orders gefunden ({len(open_trigger_orders)} statt {expected_orders}). Möglicherweise zu geringe Menge oder API-Probleme?"

        print("-> ✔ Korrekte Anzahl an Trigger-Orders (Entry, TP, SL) gefunden.")
        print("\n--- ✅ ORDER-PLATZIERUNGS-TEST ERFOLGREICH! ---")

    except Exception as e:
         pytest.fail(f"Fehler bei der Überprüfung der Orders: {e}")
