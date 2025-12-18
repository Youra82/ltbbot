# /root/ltbbot/tests/test_structure.py
import os
import sys
import pytest

# Füge das src-Verzeichnis zum Python-Pfad hinzu
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src')) # Fügt src/ an den Anfang des Pfades hinzu

def test_project_structure():
    """Stellt sicher, dass alle erwarteten Hauptverzeichnisse existieren."""
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src')), "Das 'src'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'artifacts')), "Das 'artifacts'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'tests')), "Das 'tests'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'ltbbot')), "Das 'src/ltbbot'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy')), "Das 'src/ltbbot/strategy'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'analysis')), "Das 'src/ltbbot/analysis'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'utils')), "Das 'src/ltbbot/utils'-Verzeichnis fehlt."


def test_core_script_imports():
    """
    Stellt sicher, dass die wichtigsten Funktionen aus den Kernmodulen importiert werden können.
    Dies ist ein schneller Check, ob die grundlegende Code-Struktur intakt ist.
    """
    try:
        # Importiere Kernkomponenten von ltbbot
        from ltbbot.utils.trade_manager import full_trade_cycle, place_entry_orders, manage_existing_position, cancel_strategy_orders
        from ltbbot.utils.exchange import Exchange
        from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals
        from ltbbot.analysis.backtester import run_envelope_backtest
        # Importiere 'main' aus dem optimizer und gib ihr einen Alias
        from ltbbot.analysis.optimizer import main as optimizer_main
        from ltbbot.analysis.portfolio_optimizer import run_portfolio_optimizer
        # from ltbbot.utils.guardian import guardian_decorator # <-- DIESE ZEILE ENTFERNT ODER AUSKOMMENTIERT

    except ImportError as e:
        pytest.fail(f"Kritischer Import-Fehler. Die Code-Struktur scheint defekt zu sein. Fehler: {e}")
