"""Phasen-Tor (strategy/phase_gate.py): kein Vorausblick, x1 ohne Beweis/aus/ohne Daten, Groessen-Anschluss."""
import json
import numpy as np
import pytest

from ltbbot.strategy import phase_gate as pg
from ltbbot.strategy.envelope_logic import fraction_band_amount


def _stats(qs, n=10):
    return {d: (int(round(q * n)), n) for d, q in enumerate(qs)}


def test_ohne_beweis_immer_eins():
    m = pg.multipliers_by_day(_stats(np.random.default_rng(0).random(5)), 0, 5)
    assert all(v[0] == 1.0 for v in m.values())          # < 10 Tage: kein Signal


def test_kein_vorausblick():
    g = np.random.default_rng(1); qs = g.random(200)
    a = pg.multipliers_by_day(_stats(qs), 0, 199)
    qs2 = qs.copy(); qs2[150:] = 1 - qs2[150:]            # Zukunft ab Tag 150 aendern
    b = pg.multipliers_by_day(_stats(qs2), 0, 199)
    assert all(a[d] == b[d] for d in range(151))          # Tag t nutzt nur Tage < t


def test_anhaltende_phase_wird_erkannt():
    zust = np.repeat(np.tile([.8, .3], 20), 10)            # 10-Tage-Phasen
    m = pg.multipliers_by_day(_stats(zust, 20), 0, len(zust) - 1)
    gut = [m[d][0] for d in range(200, len(zust)) if zust[d - 1] == .8 and zust[d] == .8]
    schlecht = [m[d][0] for d in range(200, len(zust)) if zust[d - 1] == .3 and zust[d] == .3]
    assert np.mean(gut) > 1.2 and np.mean(schlecht) < .8


def test_fehlender_vortag_gibt_eins():
    zust = np.repeat(np.tile([.8, .3], 20), 10); st = _stats(zust, 20)
    last = len(zust) - 1; del st[last]
    assert pg.multipliers_by_day(st, 0, last + 1)[last + 1][0] == 1.0


def test_size_mult_in_fraction_band_amount():
    p = {'risk': {'leverage': 1, 'position_size_pct': 30.0}}
    assert fraction_band_amount(1000.0, p, 10.0, 3, size_mult=1.5) == pytest.approx(1.5 * fraction_band_amount(1000.0, p, 10.0, 3))


def test_current_multiplier_aus_und_ohne_daten(tmp_path, monkeypatch):
    s = tmp_path / 'settings.json'
    s.write_text(json.dumps({'live_trading_settings': {'phase_gate_since': None}}))
    monkeypatch.setattr(pg, 'SETTINGS_PATH', str(s))
    monkeypatch.setattr(pg.enabled_since, '__defaults__', (str(s),))
    assert pg.current_multiplier() == (1.0, 'aus')
    s.write_text(json.dumps({'live_trading_settings': {'phase_gate_since': '2020-01-01'}}))
    monkeypatch.setattr(pg, 'SEED_PATH', str(tmp_path / 'fehlt.json'))
    monkeypatch.setattr(pg.load_state, '__defaults__', (str(tmp_path / 'fehlt2.json'),))
    m, info = pg.current_multiplier()
    assert m == 1.0 and info.startswith('keine Daten')


def test_backtest_multipliers_aus_und_an(tmp_path, monkeypatch):
    st = tmp_path / 'state.json'; se = tmp_path / 'settings.json'
    zust = np.repeat(np.tile([.8, .3], 20), 10)
    st.write_text(json.dumps({'pool_start': '1970-01-01', 'last_day': len(zust) - 1,
                              'stats': {str(k): list(v) for k, v in _stats(zust, 20).items()}}))
    monkeypatch.setattr(pg, 'STATE_PATH', str(st)); monkeypatch.setattr(pg, 'SEED_PATH', str(tmp_path / 'x'))
    monkeypatch.setattr(pg, 'SETTINGS_PATH', str(se)); pg._BT_CACHE.clear()
    se.write_text(json.dumps({'live_trading_settings': {}}))
    assert pg.resolve('auto') == {} and pg.resolve(None) is None             # aus -> x1
    se.write_text(json.dumps({'live_trading_settings': {'phase_gate_since': '2099-01-01'}}))
    import os; os.utime(se, (1, 1))                                          # mtime aendern -> Cache neu
    m = pg.resolve('auto')
    assert m == pg.multipliers_by_day(_stats(zust, 20), 0, len(zust))       # ganzer Zeitraum, ohne 'since'
    assert any(v[0] != 1.0 for v in m.values())
