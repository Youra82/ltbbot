"""Phasen-Tor: Tagesreihe des Signal-Pools fortschreiben (taeglich vom master_runner gestartet).

Signal-Pool = alle bestaetigten Envelope-Configs (_meta.confirmed). Fuer jeden fehlenden, abgeschlossenen UTC-Tag
seit dem letzten Stand: Backtest jeder Config (run_envelope_backtest, ohne Feindaten -- wie der Erststand
deploy/phase_gate_seed.json und die Pruefung in gewinn-forschung/ltbbot_team_phase.py) ueber die letzten
LOOKBACK_DAYS Tage, gezaehlt werden Trades mit Ausstieg an diesen Tagen; Gewinn = Backtest-PnL > 0.
Ergebnis: artifacts/phase_gate_state.json (siehe strategy/phase_gate.py). Laeuft nur, wenn
live_trading_settings.phase_gate_since gesetzt ist. Aufruf manuell: python phase_gate_update.py [--force]
"""
import os, sys, io, json, glob, contextlib
from datetime import datetime, timezone, timedelta

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'src'))
os.chdir(SCRIPT_DIR)
import logging; logging.disable(logging.WARNING)
from ltbbot.strategy import phase_gate as pg

LOOKBACK_DAYS = 60
LOG_FILE = os.path.join(SCRIPT_DIR, 'logs', 'phase_gate_update.log')
LOCK_FILE = os.path.join(SCRIPT_DIR, 'artifacts', 'phase_gate_update.lock')


def _log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def pool_trades(von_tag, bis_tag):
    """(exit_time, win) aller bestaetigten Configs mit Ausstieg an Tagen von_tag..bis_tag."""
    from ltbbot.analysis.backtester import load_data, run_envelope_backtest
    from ltbbot.analysis.show_results import get_warmup_start_date
    start = (pd.Timestamp(von_tag, unit='D') - pd.Timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    ende = (pd.Timestamp(bis_tag + 1, unit='D')).strftime('%Y-%m-%d')
    files = [f for f in sorted(glob.glob('src/ltbbot/strategy/configs/*_envelope.json'))
             if json.load(open(f)).get('_meta', {}).get('confirmed')]
    out, fehler = [], 0
    for f in files:
        cfg = json.load(open(f)); sym, tf = cfg['market']['symbol'], cfg['market']['timeframe']
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_data(sym, tf, get_warmup_start_date(start, tf), ende)
                r = run_envelope_backtest(df, cfg, start_capital=1000.0, show_progress=False, sim_start_date=start, fine_data=None,
                                          size_multiplier=None)  # Pool ohne Tor (keine Rueckkopplung)
        except Exception as e:
            fehler += 1; _log(f"  Fehler {os.path.basename(f)}: {e}"); continue
        for t in r.get('trades', []):
            d = pg.day_index(t['exit_time'])
            if von_tag <= d <= bis_tag:
                out.append((t['exit_time'], t['pnl'] > 0))
    return out, len(files), fehler


def main(force=False):
    if pg.enabled_since() is None and not force:
        return
    st = pg.load_state()
    gestern = pg.day_index(datetime.now(timezone.utc)) - 1
    if st['last_day'] >= gestern:
        return
    # master_runner startet dieses Skript bei jedem Lauf -> nur eine Instanz zugleich (Sperre max. 3 h alt)
    if os.path.exists(LOCK_FILE) and datetime.now().timestamp() - os.path.getmtime(LOCK_FILE) < 3 * 3600:
        return
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    open(LOCK_FILE, 'w').write(str(os.getpid()))
    try:
        _update(st, gestern)
    finally:
        os.remove(LOCK_FILE)


def _update(st, gestern):
    von = st['last_day'] + 1
    trades, n_cfg, fehler = pool_trades(von, gestern)
    if fehler > n_cfg // 4:          # zu viele Ausfaelle: Tage nicht schreiben, Tor bleibt fuer heute bei x1
        _log(f"Abbruch: {fehler}/{n_cfg} Configs fehlgeschlagen -- Stand bleibt {st['last_day']}")
        return
    neu = pg.daily_stats(trades)
    for d in range(von, gestern + 1):
        st['stats'][d] = neu.get(d, (0, 0))
    st['last_day'] = gestern
    pg.save_state(st)
    heute = pg.multipliers_by_day(st['stats'], pg.day_index(st['pool_start']), gestern + 1)[gestern + 1]
    _log(f"Tage {von}..{gestern} ergaenzt ({len(trades)} Trades, {n_cfg} Configs, {fehler} Fehler) | "
         f"heute: Einsatz x{heute[0]:.2f} (s={heute[1]:+.2f}, a={heute[2]:.2f})")


if __name__ == '__main__':
    main(force='--force' in sys.argv)
