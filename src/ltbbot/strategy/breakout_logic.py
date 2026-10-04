# src/ltbbot/strategy/breakout_logic.py
"""Band-Durchbruch-Strategie (2026-10-04) -- geteilte Logik fuer Live (trade_manager),
Backtest (backtester.run_breakout_backtest), Portfolio-Simulator und Optimizer.

Hintergrund (docs/research/PREREG_breakout_long_2026-10-04.md): Die Baender um den
Durchschnitt wirken bei Altcoins als Durchbruchzone, nicht als Umkehr. Die fruehere
Envelope-Umkehrlogik verlor in allen 64 festen Varianten; der Long-Ausbruch
(Close > SMA20 + 3 ATR, SL 3 ATR, TP 4R, nur bei BTC > SMA200) bestand die
praeregistrierte Pruefung auf frischen 4h-Daten (+2.66 %/Trade, PF 1.47, 382 Coins).

Config-Schema (strategy.mode == 'breakout'):
  strategy: average_type ('SMA'|'EMA'), average_period, atr_period, band_atr,
            max_hold_candles, use_btc_filter, btc_sma_days
  risk:     sl_atr, tp_r, risk_per_entry_pct, leverage, margin_mode
  behavior: use_longs, use_shorts
"""
import numpy as np
import pandas as pd

MIN_NOTIONAL_USDT = 5.0


def is_breakout(params):
    return (params.get('strategy') or {}).get('mode') == 'breakout'


def compute_breakout_indicators(df, params):
    """Fuegt 'average' und 'atr' hinzu (Wilder-ATR). Kein Lookahead: Werte der Kerze t
    nutzen nur Daten bis einschliesslich t; gehandelt wird erst zum Open von t+1."""
    sp = params['strategy']
    out = df.copy()
    period = int(sp.get('average_period', 20))
    if str(sp.get('average_type', 'SMA')).upper() == 'EMA':
        out['average'] = out['close'].ewm(span=period, adjust=False).mean()
    else:
        out['average'] = out['close'].rolling(period).mean()
    prev_close = out['close'].shift(1)
    tr = pd.concat([out['high'] - out['low'], (out['high'] - prev_close).abs(),
                    (out['low'] - prev_close).abs()], axis=1).max(axis=1)
    out['atr'] = tr.ewm(alpha=1.0 / int(sp.get('atr_period', 14)), adjust=False).mean()
    return out


def btc_regime_from_daily(btc_daily, sma_days=200):
    """Tagesdatum (UTC, normalisiert) -> True, wenn BTC am VORTAG ueber seinem SMA lag."""
    if btc_daily is None or btc_daily.empty:
        return pd.Series(dtype=object)
    bull = (btc_daily['close'] > btc_daily['close'].rolling(sma_days).mean()).where(
        btc_daily['close'].rolling(sma_days).count() >= sma_days)
    bull = bull.shift(1)
    bull.index = bull.index.normalize()
    return bull


def regime_allows(side, ts, btc_regime, params):
    """BTC-Filter: Long nur bei BTC > SMA, Short nur bei BTC < SMA. Ohne Regime-Daten -> kein Trade."""
    if not (params['strategy'].get('use_btc_filter', True)):
        return True
    if btc_regime is None or len(btc_regime) == 0:
        return False
    v = btc_regime.get(pd.Timestamp(ts).normalize())
    if v is None or pd.isna(v):
        return False
    return bool(v) if side == 'long' else (not bool(v))


def breakout_signal(close, average, atr, params):
    """Signal aus der ABGESCHLOSSENEN Kerze: 'long', 'short' oder None (ohne Regime-Filter)."""
    if any(pd.isna(x) for x in (close, average, atr)) or atr <= 0:
        return None
    k = float(params['strategy'].get('band_atr', 3.0))
    beh = params.get('behavior', {})
    if beh.get('use_longs', True) and close > average + k * atr:
        return 'long'
    if beh.get('use_shorts', False) and close < average - k * atr:
        return 'short'
    return None


def breakout_levels(side, entry_price, atr, params):
    """(sl, tp) fuer einen Einstieg. SL sl_atr * ATR der Signalkerze, TP tp_r * SL-Abstand."""
    rp = params['risk']
    dist = float(rp.get('sl_atr', 3.0)) * atr
    tp_dist = float(rp.get('tp_r', 4.0)) * dist
    if side == 'long':
        return entry_price - dist, entry_price + tp_dist
    return entry_price + dist, entry_price - tp_dist


def position_size(free_capital, entry_price, sl_price, params, used_margin=0.0):
    """Coins und Margin: Risiko = risk_per_entry_pct des freien Kapitals, begrenzt durch die
    freie Margin; None, wenn unter Bitgets Mindestorder (5 USDT)."""
    rp = params['risk']
    lev = float(rp.get('leverage', 5))
    risk_usd = max(0.0, free_capital) * float(rp.get('risk_per_entry_pct', 2.0)) / 100.0
    dist = abs(entry_price - sl_price)
    if dist <= 0 or entry_price <= 0 or risk_usd <= 0:
        return None
    notional = risk_usd / (dist / entry_price)
    notional = min(notional, max(0.0, free_capital - used_margin) * lev)
    if notional < MIN_NOTIONAL_USDT:
        return None
    return notional / entry_price, notional / lev
