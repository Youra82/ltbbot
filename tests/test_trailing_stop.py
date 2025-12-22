# tests/test_trailing_stop.py
import pytest
import os
import sys
import json
import logging
import time

# Füge das src-Verzeichnis zum Python-Pfad hinzu
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange

# Globaler Logger für Tests
test_logger = logging.getLogger("test-trailing-stop")
test_logger.setLevel(logging.INFO)
if not test_logger.handlers:
    test_logger.addHandler(logging.StreamHandler(sys.stdout))

@pytest.fixture(scope="module")
def exchange_setup():
    """Bereitet Exchange für Trailing Stop Test vor."""
    print("\n--- Starte Trailing Stop Loss Test ---")
    
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip("secret.json nicht gefunden. Überspringe Trailing Stop Test.")
    
    with open(secret_path, 'r') as f:
        secrets = json.load(f)
    
    if not secrets.get('ltbbot') or not secrets['ltbbot']:
        pytest.skip("Kein ltbbot Account in secret.json gefunden.")
    
    test_account = secrets['ltbbot'][0]
    
    try:
        exchange = Exchange(test_account)
        if not exchange.markets:
            pytest.fail("Exchange konnte nicht initialisiert werden.")
    except Exception as e:
        pytest.fail(f"Exchange Init Fehler: {e}")
    
    yield exchange
    
    print("\n[Teardown] Test abgeschlossen.")


def test_trailing_stop_api_availability(exchange_setup):
    """
    Testet, ob die Bitget API-Methode für Trailing Stop verfügbar ist.
    """
    exchange = exchange_setup
    
    print("\n[Test 1] Prüfe API-Verfügbarkeit für Trailing Stop...")
    
    # Prüfe, ob die implizite Methode existiert
    has_method = hasattr(exchange.exchange, 'private_mix_post_plan_place_plan')
    
    print(f"  -> private_mix_post_plan_place_plan verfügbar: {has_method}")
    assert has_method, "Bitget API-Methode für Trailing Stop nicht verfügbar!"
    
    print("  ✅ API-Methode ist verfügbar")


def test_trailing_stop_order_placement(exchange_setup):
    """
    Testet die Platzierung eines Trailing Stop Loss und storniert ihn sofort wieder.
    VORSICHT: Dieser Test platziert eine echte Order auf Bitget!
    """
    exchange = exchange_setup
    symbol = 'BTC/USDT:USDT'
    
    print(f"\n[Test 2] Teste Trailing Stop Platzierung für {symbol}...")
    
    # Hole aktuellen Preis
    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        print(f"  -> Aktueller BTC Preis: {current_price:.2f} USDT")
    except Exception as e:
        pytest.fail(f"Konnte Ticker nicht abrufen: {e}")
    
    # Setze Trailing Stop weit außerhalb des Marktes (wird nicht getriggert)
    # Für Long: Stop weit unter dem Preis
    activation_price = current_price * 0.7  # 30% unter aktuellem Preis
    callback_rate = 0.003  # 0.3%
    test_amount = 0.001  # Sehr kleine Menge
    side = 'sell'  # Für Long-Position Close
    
    print(f"  -> Platziere Trailing Stop:")
    print(f"     Amount: {test_amount} BTC")
    print(f"     Aktivierung: {activation_price:.2f} USDT")
    print(f"     Callback: {callback_rate*100:.2f}%")
    
    order_id = None
    try:
        response = exchange.place_trailing_stop_order(
            symbol=symbol,
            side=side,
            amount=test_amount,
            activation_price=activation_price,
            callback_rate_decimal=callback_rate
        )
        
        print(f"  -> Bitget Response: {response}")
        
        # Prüfe Response-Struktur
        if isinstance(response, dict):
            if response.get('code') == '00000':
                order_id = response.get('data', {}).get('orderId')
                print(f"  ✅ Trailing Stop erfolgreich platziert! Order ID: {order_id}")
            else:
                error_msg = response.get('msg', 'Unbekannter Fehler')
                pytest.fail(f"Bitget API Error: {error_msg}")
        else:
            pytest.fail(f"Unerwartete Response-Struktur: {response}")
        
    except Exception as e:
        pytest.fail(f"Fehler beim Platzieren des Trailing Stops: {e}")
    
    # Warte kurz, damit Order in System ist
    time.sleep(2)
    
    # Storniere die Test-Order sofort wieder
    if order_id:
        print(f"  -> Storniere Test-Order {order_id}...")
        try:
            cancel_response = exchange.cancel_order(order_id, symbol)
            print(f"  -> Cancel Response: {cancel_response}")
            print("  ✅ Test-Order erfolgreich storniert")
        except Exception as e:
            print(f"  ⚠️ Warnung: Konnte Test-Order nicht stornieren: {e}")
            print(f"  -> Bitte Order {order_id} manuell in Bitget stornieren!")


def test_trailing_stop_config_parameter():
    """
    Testet, ob alle Config-Dateien den trailing_callback_rate_pct Parameter haben.
    """
    print("\n[Test 3] Prüfe Config-Dateien auf trailing_callback_rate_pct...")
    
    config_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    
    if not os.path.exists(config_dir):
        pytest.skip("Config-Verzeichnis nicht gefunden.")
    
    config_files = [f for f in os.listdir(config_dir) if f.endswith('.json')]
    
    print(f"  -> Gefunden: {len(config_files)} Config-Dateien")
    
    missing_param = []
    for config_file in config_files:
        config_path = os.path.join(config_dir, config_file)
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            if 'risk' in config:
                if 'trailing_callback_rate_pct' not in config['risk']:
                    missing_param.append(config_file)
        except Exception as e:
            print(f"  ⚠️ Fehler beim Lesen von {config_file}: {e}")
    
    if missing_param:
        print(f"  ❌ Fehlender Parameter in: {', '.join(missing_param)}")
        pytest.fail(f"trailing_callback_rate_pct fehlt in {len(missing_param)} Configs")
    
    print(f"  ✅ Alle {len(config_files)} Config-Dateien haben den Parameter")


if __name__ == "__main__":
    # Ermöglicht direktes Ausführen: python tests/test_trailing_stop.py
    pytest.main([__file__, "-v", "-s"])
