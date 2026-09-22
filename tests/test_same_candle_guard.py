# tests/test_same_candle_guard.py
"""Rein lokale Tests fuer arm_same_candle_reentry_guard() -- keine Bitget-
Verbindung, keine Live-Orders. Verifiziert den robusten Re-Entry-Schutz
(committed_bands-Diff statt fragiler Bitget-Order-Status-Erkennung), der
den live beobachteten ARB/6h-Vorfall (2026-09-15/16: Band 1 wurde 7x
innerhalb einer Kerze neu eroeffnet) beheben soll."""
import os
import sys
import json
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.trade_manager import arm_same_candle_reentry_guard, read_tracker_file, update_tracker_file

test_logger = logging.getLogger("test-same-candle-guard")
test_logger.setLevel(logging.INFO)
if not test_logger.handlers:
    test_logger.addHandler(logging.StreamHandler(sys.stdout))


def _write_tracker(path, committed_bands, sl_fired_candle_ts=None):
    data = {"committed_bands": committed_bands}
    if sl_fired_candle_ts is not None:
        data["sl_fired_candle_ts"] = sl_fired_candle_ts
    update_tracker_file(path, data)


def test_band_that_closed_gets_armed(tmp_path):
    """Band 1 (short) war committed, ist es nach den Checks nicht mehr ->
    muss in sl_fired_candle_ts landen."""
    tracker_path = str(tmp_path / "ARB-USDT-USDT_6h.json")
    committed_before = {"long": [], "short": [1]}
    _write_tracker(tracker_path, committed_bands={"long": [], "short": []})  # Band 1 bereits "gefallen"

    result = arm_same_candle_reentry_guard(tracker_path, committed_before, "2026-09-15 18:00:00+00:00",
                                            test_logger, "ARB/USDT:USDT")

    assert result["short"]["1"] == "2026-09-15 18:00:00+00:00"
    persisted = read_tracker_file(tracker_path)
    assert persisted["sl_fired_candle_ts"]["short"]["1"] == "2026-09-15 18:00:00+00:00"


def test_still_committed_band_not_armed(tmp_path):
    """Band, das weiterhin committed ist, darf NICHT geblockt werden."""
    tracker_path = str(tmp_path / "ARB-USDT-USDT_6h.json")
    committed_before = {"long": [], "short": [1, 2]}
    _write_tracker(tracker_path, committed_bands={"long": [], "short": [1, 2]})  # unveraendert

    result = arm_same_candle_reentry_guard(tracker_path, committed_before, "2026-09-15 18:00:00+00:00",
                                            test_logger, "ARB/USDT:USDT")

    assert result.get("short", {}) == {}
    assert result.get("long", {}) == {}


def test_only_the_closed_band_is_armed_others_untouched(tmp_path):
    """Baender 1+2 committed, nur Band 1 faellt raus -> nur Band 1 wird
    geblockt, Band 2 bleibt frei fuer weitere Zyklen."""
    tracker_path = str(tmp_path / "ARB-USDT-USDT_6h.json")
    committed_before = {"long": [], "short": [1, 2]}
    _write_tracker(tracker_path, committed_bands={"long": [], "short": [2]})  # nur Band 1 raus

    result = arm_same_candle_reentry_guard(tracker_path, committed_before, "2026-09-15 18:00:00+00:00",
                                            test_logger, "ARB/USDT:USDT")

    assert "1" in result["short"]
    assert "2" not in result["short"]


def test_repeated_close_reopen_within_same_candle_stays_blocked(tmp_path):
    """Reproduziert den ARB-Vorfall: Band 1 schliesst, wird (durch den Bug im
    urspruenglichen Mechanismus faelschlich) neu eroeffnet, schliesst wieder --
    alles INNERHALB derselben Kerze. Nach dem ersten Schliessen darf
    place_entry_orders() dieses Band fuer den Rest der Kerze nicht mehr
    oeffnen; dieser Test prueft die Bedingung, die place_entry_orders()
    dafuer nutzt (sl_fired_candle_ts[...] == current_candle_ts)."""
    tracker_path = str(tmp_path / "ARB-USDT-USDT_6h.json")
    current_candle_ts = "2026-09-15 18:00:00+00:00"

    # Zyklus 1: Band 1 committed -> Zyklus-Ende: SL feuert, nicht mehr committed
    committed_before_cycle1 = {"long": [], "short": [1]}
    _write_tracker(tracker_path, committed_bands={"long": [], "short": []})
    arm_same_candle_reentry_guard(tracker_path, committed_before_cycle1, current_candle_ts,
                                   test_logger, "ARB/USDT:USDT")

    # Zyklus 2 (15 Min spaeter, GLEICHE Kerze): angenommen place_entry_orders()
    # haette (ohne die Bremse) erneut eroeffnet und wurde sofort wieder
    # gestoppt -- committed_bands_before fuer Zyklus 2 ist wieder leer (Band 1
    # war zwischen den Zyklen nie sauber committed), das darf den bereits
    # gesetzten sl_fired_candle_ts-Eintrag NICHT loeschen.
    committed_before_cycle2 = {"long": [], "short": []}
    tracker_after_cycle1 = read_tracker_file(tracker_path)
    result = arm_same_candle_reentry_guard(tracker_path, committed_before_cycle2, current_candle_ts,
                                            test_logger, "ARB/USDT:USDT")

    # sl_fired_candle_ts aus Zyklus 1 muss erhalten bleiben
    persisted = read_tracker_file(tracker_path)
    assert persisted["sl_fired_candle_ts"]["short"]["1"] == current_candle_ts

    # Die Bedingung, die place_entry_orders() fuer Band 1 (short) prueft:
    guard_blocks_reentry = persisted["sl_fired_candle_ts"].get("short", {}).get("1") == current_candle_ts
    assert guard_blocks_reentry is True


def test_next_candle_clears_the_block_implicitly(tmp_path):
    """sl_fired_candle_ts traegt den STRING der Kerze, in der geschlossen
    wurde. Sobald current_candle_ts (naechste Kerze) einen anderen Wert hat,
    vergleicht place_entry_orders() ungleich -> Band ist wieder frei. Kein
    aktives Loeschen noetig, nur ein Wertevergleich."""
    tracker_path = str(tmp_path / "ARB-USDT-USDT_6h.json")
    old_candle_ts = "2026-09-15 18:00:00+00:00"
    new_candle_ts = "2026-09-16 00:00:00+00:00"

    _write_tracker(tracker_path, committed_bands={"long": [], "short": []},
                    sl_fired_candle_ts={"short": {"1": old_candle_ts}, "long": {}})

    persisted = read_tracker_file(tracker_path)
    guard_blocks_reentry = persisted["sl_fired_candle_ts"].get("short", {}).get("1") == new_candle_ts
    assert guard_blocks_reentry is False
