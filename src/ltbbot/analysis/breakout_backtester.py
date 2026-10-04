# src/ltbbot/analysis/breakout_backtester.py
"""Backtest der Band-Durchbruch-Strategie (strategy.mode == 'breakout').

Ablauf je Kerze i (Werte der Kerze i-1 = letzte ABGESCHLOSSENE Kerze, wie live):
  1. Offene Position: Gap ueber SL/TP -> Fill zum Open; sonst SL/TP-Beruehrung in Kerze i
     (beides -> Reihenfolge per Fein-Daten, sonst SL zuerst); Zeitstopp nach max_hold_candles
     zum Open.
  2. Flach (und nicht in dieser Kerze geschlossen): Signal aus Kerze i-1 + BTC-Regime ->
     Market-Entry zum Open der Kerze i (+ Slippage), SL/TP aus ATR(i-1); SL/TP koennen
     schon in Kerze i greifen (Entry zum Open, also danach).
Kosten: 0.06 % Gebuehr je Seite, Entry-Slippage 0.10 %, Stop-Slippage wie Envelope
(stop_fill_price), Exit-Slippage 0.05 %. Funding nicht enthalten.
Rueckgabe im selben Format wie run_envelope_backtest (Optimizer/oos_gate kompatibel)."""
import logging
import pandas as pd

from ltbbot.strategy.breakout_logic import (compute_breakout_indicators, breakout_signal, breakout_levels,
                                            position_size, regime_allows, btc_regime_from_daily)
from ltbbot.strategy.envelope_logic import stop_fill_price

logger = logging.getLogger(__name__)
FEE_PCT = 0.0006
SLIP_ENTRY = 0.0010
SLIP_EXIT = 0.0005
_BTC_REGIME = {}


def get_btc_regime(sma_days=200):
    """BTC-Tagesregime (gecacht). Laedt bewusst ab 2022, damit der SMA200 ueberall definiert ist."""
    if sma_days not in _BTC_REGIME:
        from ltbbot.analysis.backtester import load_data
        end = (pd.Timestamp.utcnow() - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        btc = load_data('BTC/USDT:USDT', '1d', '2022-01-01', end)
        _BTC_REGIME[sma_days] = btc_regime_from_daily(btc, sma_days)
    return _BTC_REGIME[sma_days]


def simulate_breakout_trades(data, params, start_capital, fine_data=None, btc_regime=None, sim_start=None):
    """Kern-Simulation fuer EINEN Coin. Gibt (closed_trades, equity_curve, end_capital) zurueck."""
    from ltbbot.analysis.backtester import _get_fine_slice, _resolve_ambiguous_exit
    sp = params['strategy']
    if btc_regime is None and sp.get('use_btc_filter', True):
        btc_regime = get_btc_regime(int(sp.get('btc_sma_days', 200)))
    df = compute_breakout_indicators(data, params)
    max_hold = int(sp.get('max_hold_candles', 60))
    coarse = df.index[1] - df.index[0] if len(df) > 1 else None
    o, h, l = df['open'].values, df['high'].values, df['low'].values
    c, avg, atr = df['close'].values, df['average'].values, df['atr'].values
    idx = df.index
    capital, pos, trades, curve = float(start_capital), None, [], [{'timestamp': idx[0], 'equity': float(start_capital)}]
    start_ts = pd.Timestamp(sim_start, tz='UTC') if sim_start is not None and pd.Timestamp(sim_start).tzinfo is None \
        else (pd.Timestamp(sim_start) if sim_start is not None else None)

    def close(exit_px, reason, ts):
        nonlocal capital, pos
        side, amt, entry = pos['side'], pos['amount'], pos['entry']
        if reason == 'SL':
            exit_px = stop_fill_price(side, exit_px, pos['atr_pct'])
        else:
            exit_px = exit_px * (1 - SLIP_EXIT) if side == 'long' else exit_px * (1 + SLIP_EXIT)
        pnl = (exit_px - entry) * amt if side == 'long' else (entry - exit_px) * amt
        pnl -= (entry * amt + exit_px * amt) * FEE_PCT
        capital += pnl
        trades.append({'pnl': pnl, 'side': side, 'band': 1, 'entry_time': pos['time'], 'exit_time': ts,
                       'entry_price': entry, 'exit_price': exit_px, 'exit_reason': reason,
                       'sl_price': pos['sl'], 'tp_price': pos['tp']})
        curve.append({'timestamp': ts, 'equity': capital})
        pos = None

    for i in range(1, len(df)):
        ts = idx[i]
        closed_now = False
        if pos is not None:
            side, sl, tp = pos['side'], pos['sl'], pos['tp']
            long_ = side == 'long'
            if (long_ and o[i] <= sl) or (not long_ and o[i] >= sl):
                close(o[i], 'SL', ts); closed_now = True
            elif (long_ and o[i] >= tp) or (not long_ and o[i] <= tp):
                close(o[i], 'TP', ts); closed_now = True
            elif i - pos['i'] >= max_hold:
                close(o[i], 'TIME', ts); closed_now = True
        if pos is None and not closed_now and capital > 0 and (start_ts is None or ts >= start_ts):
            side = breakout_signal(c[i - 1], avg[i - 1], atr[i - 1], params)
            if side and regime_allows(side, idx[i - 1], btc_regime, params):
                entry = o[i] * (1 + SLIP_ENTRY) if side == 'long' else o[i] * (1 - SLIP_ENTRY)
                sl, tp = breakout_levels(side, entry, atr[i - 1], params)
                sz = position_size(capital, entry, sl, params)
                if sz:
                    pos = {'side': side, 'amount': sz[0], 'entry': entry, 'sl': sl, 'tp': tp, 'i': i,
                           'time': ts, 'atr_pct': atr[i - 1] / c[i - 1] if c[i - 1] else None}
        if pos is not None and pos['i'] <= i:
            side, sl, tp = pos['side'], pos['sl'], pos['tp']
            long_ = side == 'long'
            sl_hit = l[i] <= sl if long_ else h[i] >= sl
            tp_hit = h[i] >= tp if long_ else l[i] <= tp
            if sl_hit and tp_hit:
                res = None
                if fine_data is not None and coarse is not None:
                    start = ts if pos['i'] < i else ts  # Entry zum Open -> ganze Kerze relevant
                    res = _resolve_ambiguous_exit(_get_fine_slice(fine_data, start, ts + coarse), sl, tp, side)
                close(tp if res == tp else sl, 'TP' if res == tp else 'SL', ts)
            elif sl_hit:
                close(sl, 'SL', ts)
            elif tp_hit:
                close(tp, 'TP', ts)
    if pos is not None:
        close(c[-1], 'END', idx[-1])
    return trades, curve, capital


def run_breakout_backtest(data, params, start_capital=1000, fine_data=None, btc_regime=None, sim_start_date=None):
    if data is None or data.empty:
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100,
                "end_capital": 0, "start_capital": start_capital, "trades": [], "equity_curve": []}
    trades, curve, end_cap = simulate_breakout_trades(data, params, start_capital, fine_data, btc_regime, sim_start_date)
    eq = pd.Series([p['equity'] for p in curve])
    peak = eq.cummax()
    mdd = float(((peak - eq) / peak.replace(0, float('nan'))).fillna(0).max() * 100) if len(eq) else 0.0
    n = len(trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    return {
        "total_pnl_pct": round((end_cap - start_capital) / start_capital * 100, 2) if start_capital else 0,
        "trades_count": n,
        "win_rate": round(wins / n * 100, 2) if n else 0,
        "max_drawdown_pct": round(mdd, 2),
        "end_capital": round(max(0.0, end_cap), 2),
        "start_capital": start_capital,
        "equity_curve": curve,
        "trades": trades,
    }


def run_breakout_portfolio_simulation(start_capital, strategies_data, start_date, end_date):
    """Portfolio mit GEMEINSAMEM Kapital (ein Bitget-Konto) fuer Breakout-Strategien.
    Je Strategie liefert simulate_breakout_trades() die Ein-/Ausstiege (Zeitpunkte, Preise,
    Exit-Grund -- unabhaengig vom Kapital); die Groesse jedes Trades wird hier zum Entry-Zeitpunkt
    aus dem dann freien Kapital bestimmt (risk_per_entry_pct, Hebel, Mindestorder 5 USDT),
    wie live. Reicht die freie Margin nicht, wird der Trade ausgelassen."""
    import numpy as np
    from ltbbot.strategy.breakout_logic import MIN_NOTIONAL_USDT
    def _ts(x, default):
        if x is None or (isinstance(x, str) and not x):
            return default
        t = pd.Timestamp(x)
        return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')
    s_ts = _ts(start_date, pd.Timestamp('1970-01-01', tz='UTC'))
    e_ts = _ts(end_date, pd.Timestamp('2100-01-01', tz='UTC')) + pd.Timedelta(days=1)
    events = []
    for sid, info in strategies_data.items():
        p = info['params']
        tr, _, _ = simulate_breakout_trades(info['data'], p, 1e9, info.get('fine_data'))
        for t in tr:
            if s_ts <= t['entry_time'] < e_ts:
                events.append((t['entry_time'], t['exit_time'], sid, info['symbol'], info['timeframe'], t, p))
    events.sort(key=lambda x: (x[0], x[2]))
    equity, used, open_pos, closed, curve = float(start_capital), 0.0, [], [], [{'timestamp': s_ts, 'equity': float(start_capital)}]

    def realize(upto):
        nonlocal equity, used, open_pos
        keep = []
        for op in sorted(open_pos, key=lambda x: x['exit_time']):
            if op['exit_time'] <= upto:
                t = op['t']
                amt = op['amount']
                pnl = (t['exit_price'] - t['entry_price']) * amt if t['side'] == 'long' else (t['entry_price'] - t['exit_price']) * amt
                pnl -= (t['entry_price'] + t['exit_price']) * amt * FEE_PCT
                equity += pnl; used -= op['margin']
                closed.append({'exit_time': t['exit_time'], 'entry_time': t['entry_time'], 'symbol': op['symbol'],
                               'timeframe': op['tf'], 'side': t['side'], 'band': 1, 'entry_price': t['entry_price'],
                               'exit_price': t['exit_price'], 'sl_price': t['sl_price'], 'leverage': op['lev'],
                               'amount_coins': amt, 'pnl_usd': pnl, 'pnl_pct': pnl / max(1e-9, op['margin']) * 100,
                               'reason': 'WIN' if pnl > 0 else 'LOSS', 'exit_reason': t['exit_reason'], 'strategy_id': op['sid']})
                curve.append({'timestamp': t['exit_time'], 'equity': equity})
            else:
                keep.append(op)
        open_pos = keep

    for entry_time, exit_time, sid, sym, tf, t, p in events:
        realize(entry_time)
        if equity <= 0:
            break
        rp = p['risk']
        lev = float(rp.get('leverage', 5))
        risk_usd = max(0.0, equity) * float(rp.get('risk_per_entry_pct', 2.0)) / 100.0
        dist_pct = abs(t['entry_price'] - t['sl_price']) / t['entry_price']
        if dist_pct <= 0:
            continue
        notional = min(risk_usd / dist_pct, max(0.0, equity - used) * lev)
        if notional < MIN_NOTIONAL_USDT:
            continue
        margin = notional / lev
        open_pos.append({'exit_time': exit_time, 'amount': notional / t['entry_price'], 'margin': margin, 't': t,
                         'sid': sid, 'symbol': sym, 'tf': tf, 'lev': lev})
        used += margin
    realize(pd.Timestamp.max.tz_localize('UTC'))
    trades_df = pd.DataFrame(closed) if closed else pd.DataFrame(columns=['exit_time', 'entry_time', 'symbol', 'timeframe', 'side', 'band', 'entry_price', 'exit_price', 'sl_price', 'leverage', 'amount_coins', 'pnl_usd', 'pnl_pct', 'reason', 'exit_reason', 'strategy_id'])
    eq = pd.DataFrame(curve).set_index('timestamp')
    eq['peak'] = eq['equity'].cummax()
    eq['drawdown_pct'] = ((eq['peak'] - eq['equity']) / eq['peak'].replace(0, np.nan)).fillna(0) * 100
    mdd_date = eq['drawdown_pct'].idxmax() if len(eq) else None
    n = len(trades_df)
    return {
        "start_capital": start_capital, "end_capital": max(0.0, equity),
        "total_pnl_pct": (max(0.0, equity) / start_capital - 1) * 100 if start_capital else 0,
        "trade_count": n, "win_rate": (trades_df.pnl_usd > 0).mean() * 100 if n else 0,
        "max_drawdown_pct": float(eq['drawdown_pct'].max()) if len(eq) else 0.0, "max_drawdown_date": mdd_date,
        "min_equity": float(eq['equity'].min()) if len(eq) else start_capital, "liquidation_date": None,
        "pnl_per_strategy": trades_df.groupby('strategy_id')['pnl_usd'].sum().reset_index().rename(columns={'pnl_usd': 'pnl'}) if n else pd.DataFrame(columns=['strategy_id', 'pnl']),
        "trades_per_strategy": trades_df.groupby('strategy_id').size().reset_index(name='trades') if n else pd.DataFrame(columns=['strategy_id', 'trades']),
        "equity_curve": eq, "trades_df": trades_df,
    }
