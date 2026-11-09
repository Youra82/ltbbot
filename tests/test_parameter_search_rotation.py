"""Rotation der automatischen Parameter-Suche: ein nicht bestaetigtes Paar darf nicht taeglich erneut gewaehlt werden
(Fehler bis 2026-11-09: optimized_at aendert sich nur bei Bestaetigung -> LTC 4h jeden Tag)."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_parameter_optimizer_scheduler as S  # noqa: E402


def _cfg(d, sym, tf, optimized_at):
    path = os.path.join(d, f"config_{sym}USDTUSDT_{tf}_envelope.json")
    with open(path, "w") as f:
        json.dump({"market": {"symbol": f"{sym}/USDT:USDT", "timeframe": tf},
                   "_meta": {"optimized_at": optimized_at.isoformat()}}, f)
    return path


def test_unconfirmed_pair_rotates_away(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        old_cfg, old_cache, old_checked = S.CONFIGS_DIR, S.CACHE_DIR, S.CHECKED_FILE
        S.CONFIGS_DIR, S.CACHE_DIR, S.CHECKED_FILE = d, d, os.path.join(d, ".checked.json")
        try:
            now = datetime(2026, 11, 9, 12)
            ltc = _cfg(d, "LTC", "4h", now - timedelta(days=90))
            _cfg(d, "ETH", "1h", now - timedelta(days=60))
            assert S._pick_stalest_pair("_envelope")[1] == "LTC/USDT:USDT"
            S._mark_checked(ltc, now)                    # Suche lief, nicht bestaetigt -> optimized_at unveraendert
            pick = S._pick_stalest_pair("_envelope")
            assert pick[1] == "ETH/USDT:USDT"            # Rotation geht weiter
            assert pick[0] == now - timedelta(days=60)
        finally:
            S.CONFIGS_DIR, S.CACHE_DIR, S.CHECKED_FILE = old_cfg, old_cache, old_checked


def test_missing_or_broken_checked_file_falls_back():
    with tempfile.TemporaryDirectory() as d:
        old_cfg, old_checked = S.CONFIGS_DIR, S.CHECKED_FILE
        S.CONFIGS_DIR, S.CHECKED_FILE = d, os.path.join(d, ".checked.json")
        try:
            with open(S.CHECKED_FILE, "w") as f:
                f.write("{kaputt")
            _cfg(d, "LTC", "4h", datetime(2026, 8, 1))
            assert S._pick_stalest_pair("_envelope")[1] == "LTC/USDT:USDT"
        finally:
            S.CONFIGS_DIR, S.CHECKED_FILE = old_cfg, old_checked
