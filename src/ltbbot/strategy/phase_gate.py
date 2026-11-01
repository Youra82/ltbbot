"""Phasen-Tor (aus gewinn-forschung Labor 16, Übertragung ueb_phase_tag.py, bestanden 31.10.2026).

Idee: Gewinnen/verlieren viele Configs gleichzeitig (gemeinsame Marktphase) UND hält die Phase einen Tag an,
dann sagt die Trefferquote von gestern etwas über heute. Das wird nicht angenommen, sondern laufend bewiesen:
  - Zeitabschnitt = Kalendertag (UTC). Tagesquote q = Trefferquote der an dem Tag GESCHLOSSENEN Trades aller
    bestätigten Configs (Signal-Pool, mind. 3 Trades).
  - Signal für Tag t: s = clip((q_{t-1} - Ø q) / sd q, -2, 2) aus den bisherigen Tagen (mind. 10), sonst 0.
  - Beweis einmal je Tag (nie je Trade -- gleichzeitige Trades würden ihn sonst aufblähen): E-Mix über
    lam in (0,1; 0,2; 0,4; 0,8), Faktor 1 + lam * s * (q - p^) / (2 max(p^, 1-p^)), gezählt ab Start.
  - Anteil a = clip(log E / log 10, 0, 1) * (E-gewichtetes Ø lam / 0,8)  -> 0 ohne Beweis.
  - Einsatz-Multiplikator m = 1 + clip(0,5 s, -1, 1) * a   (0 .. 2; ohne Beweis immer 1).
Live und Portfolio-Simulator rufen dieselbe Funktion multipliers_by_day() auf.
Backtests (Einzel-Backtest, Portfolio-Simulator, woechentlicher Portfolio-Optimierer, show_results) nutzen das Tor
ueber backtest_multipliers() automatisch fuer den ganzen Zeitraum, sobald es eingeschaltet ist (size_multiplier='auto').

Betrieb: phase_gate_update.py (taeglich vom master_runner gestartet) schreibt die Tagesreihe des Signal-Pools nach
artifacts/phase_gate_state.json (Erststand aus deploy/phase_gate_seed.json). Eingeschaltet ueber settings.json
live_trading_settings.phase_gate_since = "JJJJ-MM-TT" (null/fehlt = aus). Fehlt der Vortag in der Reihe (Update
noch nicht gelaufen / fehlgeschlagen), ist s = 0 und damit m = 1 -- der Bot handelt dann wie ohne Tor.
"""
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
STATE_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'phase_gate_state.json')
SEED_PATH = os.path.join(PROJECT_ROOT, 'deploy', 'phase_gate_seed.json')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

LAM = np.array([.1, .2, .4, .8])
MIN_TRADES_PER_DAY = 3
MIN_DAYS = 10
PRIOR_N, PRIOR_P = 20, .5


def day_index(ts) -> int:
    """UTC-Kalendertag als ganze Zahl (Tage seit 1970-01-01)."""
    t = pd.Timestamp(ts)
    t = t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')
    return int(t.value // 86_400_000_000_000)


def daily_stats(closed_trades) -> dict:
    """closed_trades: iterierbar aus (exit_time, win) -> {tag: (gewinner, anzahl)}."""
    out = {}
    for exit_time, win in closed_trades:
        d = day_index(exit_time)
        w, n = out.get(d, (0, 0))
        out[d] = (w + int(bool(win)), n + 1)
    return out


def multipliers_by_day(stats: dict, first_day: int, last_day: int) -> dict:
    """Multiplikator je Tag first_day..last_day. Der Wert für Tag t nutzt nur Tage < t (kein Vorausblick).
    Rückgabe {tag: (m, s, a)}."""
    logE = np.zeros(len(LAM)); qs = []; s = 0.0; summe = anz = 0; out = {}
    for tag in range(first_day, last_day + 1):
        mx = logE.max(); lS = mx + np.log(np.exp(logE - mx).mean())
        w = np.exp(logE - mx); w /= w.sum()
        a = float(np.clip(lS / np.log(10), 0, 1) * (w @ LAM) / LAM.max())
        out[tag] = (1.0 + float(np.clip(.5 * s, -1, 1)) * a, s, a)
        tw, tn = stats.get(tag, (0, 0))               # Tag abschließen: erst danach für morgen nutzbar
        if tn >= MIN_TRADES_PER_DAY:
            q = tw / tn; p_hat = (summe + PRIOR_P * PRIOR_N) / (anz + PRIOR_N)
            if s != 0: logE += np.log1p(LAM * s * (q - p_hat) / (2 * max(p_hat, 1 - p_hat)))
            qs.append(q)
            s = float(np.clip((q - np.mean(qs)) / max(np.std(qs), 1e-3), -2, 2)) if len(qs) >= MIN_DAYS else 0.0
        else:
            s = 0.0
        summe += tw; anz += tn
    return out


def load_state(path=STATE_PATH) -> dict:
    """{'pool_start': 'JJJJ-MM-TT', 'last_day': int, 'stats': {tag: (w, n)}}; fehlt die Datei: Erststand (Seed)."""
    for p in (path, SEED_PATH):
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                st = json.load(f)
            st['stats'] = {int(k): tuple(v) for k, v in st['stats'].items()}
            return st
    raise FileNotFoundError(f"Phasen-Tor: weder {path} noch {SEED_PATH} vorhanden")


def save_state(st: dict, path=STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = dict(st, stats={str(k): list(v) for k, v in sorted(st['stats'].items())},
               updated=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    os.replace(tmp, path)


def enabled_since(settings_path=SETTINGS_PATH):
    """UTC-Tag ab dem das Tor wirkt, oder None (aus)."""
    try:
        with open(settings_path, encoding='utf-8') as f:
            v = json.load(f).get('live_trading_settings', {}).get('phase_gate_since')
        return day_index(v) if v else None
    except Exception:
        return None


def multipliers_from_state(st: dict, last_day: int, since=None) -> dict:
    """{tag: (m, s, a)} bis last_day; vor 'since' (oder ohne since) m = 1."""
    if since is None:
        return {}
    out = multipliers_by_day(st['stats'], day_index(st['pool_start']), last_day)
    return {d: (v if d >= since else (1.0, v[1], v[2])) for d, v in out.items()}


_BT_CACHE = {}


def backtest_multipliers() -> dict:
    """Backtests: {tag: (m, s, a)} ueber die ganze Tagesreihe (ab pool_start, kein 'since' -- so als haette der Bot
    das Tor immer genutzt). Aus (phase_gate_since fehlt) oder ohne Daten: {} = Einsatz x1. Zwischengespeichert,
    solange settings.json und Tagesreihe unveraendert sind (der Optimierer simuliert tausende Male)."""
    def mtime(p):
        return os.path.getmtime(p) if os.path.exists(p) else None
    key = (mtime(SETTINGS_PATH), mtime(STATE_PATH), mtime(SEED_PATH))
    if key not in _BT_CACHE:
        out = {}
        if enabled_since(SETTINGS_PATH) is not None:
            try:
                st = load_state(STATE_PATH)
                out = multipliers_by_day(st['stats'], day_index(st['pool_start']), st.get('last_day', -1) + 1)
            except Exception:
                out = {}
        _BT_CACHE.clear()
        _BT_CACHE[key] = out
    return _BT_CACHE[key]


def resolve(size_multiplier):
    """'auto' -> backtest_multipliers(); None/{} -> aus; dict -> unveraendert."""
    return backtest_multipliers() if isinstance(size_multiplier, str) and size_multiplier == 'auto' else size_multiplier


def current_multiplier(now=None):
    """Live: (m, info) fuer den heutigen UTC-Tag. Aus, ohne Daten oder bei Fehler: (1.0, Grund)."""
    since = enabled_since()
    if since is None:
        return 1.0, 'aus'
    today = day_index(now or datetime.now(timezone.utc))
    if today < since:
        return 1.0, 'noch nicht aktiv'
    try:
        st = load_state()
    except Exception as e:
        return 1.0, f'keine Daten ({e})'
    m, s, a = multipliers_by_day(st['stats'], day_index(st['pool_start']), today)[today]
    stand = 'aktuell' if st.get('last_day', -1) >= today - 1 else f"Vortag fehlt (Stand {st.get('last_day')})"
    return m, f's={s:+.2f} a={a:.2f} {stand}'
