# src/ltbbot/analysis/portfolio_live_sim.py
"""Portfolio-Simulator im Live-Takt (2026-11-07).

Warum: portfolio_simulator.py rechnet Kerze fuer Kerze (Strategie-Timeframe). Der Abgleich
mit den echten Bitget-Trades (03.-07.11.2026, Live +4.77 vs. Simulator +14.11 USDT) zeigte
drei Abweichungen, die auf Kerzen nicht loesbar sind:
  1. Margin-Wettlauf: live bekommt die Margin, wessen Band ZUERST beruehrt wird. Der
     Kerzen-Simulator vergab sie nach Kerzen-Beginn (6h-Kerze 12:00 vor 1h-Kerze 15:00)
     und bei gleichem Beginn alphabetisch -> andere Coins als live.
  2. Ausloesung am Mark-Preis (ccxt/Bitget-Standard triggerType mark_price fuer Entry,
     angehaengten SL und TP). Kurze Dochte im Last-Preis loesen live nicht aus.
  3. TP-Ablauf: der TP wird live erst im naechsten 15-Min-Zyklus nach dem Fill gesetzt
     (Mitte der letzten abgeschlossenen Kerze) und loest dann intrabar aus.

Dieser Simulator spielt den Livebot auf 1m-MARK-Kerzen nach:
  - alle 15 Min (Cron */15, je Strategie gestaffelt) ein Zyklus je Strategie wie trade_manager.full_trade_cycle:
    Sperren (gleiche Kerze, nach SL, BTC-Trend), Short-Regime-Ausstieg, alle Entry-Orders
    stornieren, bei Position TP-Pruefung/TP setzen + weitere Baender derselben Seite, sonst
    Positionsgrenze und neue Entry-Orders -- Groesse aus dem FREIEN Kapital zum Zykluszeitpunkt.
  - Entry-Orders sind Trigger-Limit-Orders (Ausloese-Richtung aus Trigger vs. Preis bei
    Platzierung, Fill ~ Trigger), die Margin wird erst beim Ausloesen geprueft (Bitget lehnt
    dann ab) -- deshalb gewinnt die zuerst beruehrte Order.
  - SL je Band fest, ab Fill aktiv; TP gebuendelt je Position.
Indikatoren/Baender aus den Last-Preis-Kerzen (wie live fetch_ohlcv), Entscheidungsregeln aus
envelope_logic (dieselben Funktionen wie live).

Nicht abbildbar: Reihenfolge zweier Ausloesungen in DERSELBEN Minute (hier: Team-Reihenfolge),
Netzwerk-/Boersenfehler, Teil-Fills.
"""
import logging

import numpy as np
import pandas as pd
import ta as _ta

from ltbbot.strategy.envelope_logic import (calculate_indicators_and_signals, calculate_position_margin, margin_fits,
                                            classify_regime, compute_band_sl_price, stop_fill_price, is_touch_mode,
                                            btc_filter_enabled, btc_trend_up_at, btc_side_allowed,
                                            reentry_blocks_until_cross, reentry_block_cleared, fraction_band_amount,
                                            uses_fraction_sizing, average_col, btc_below50_at, short_regime_exit,
                                            max_concurrent_positions, concurrency_allows_new, tp_already_reached,
                                            entry_blocked_by_sl, side_leverage)
from ltbbot.strategy.phase_gate import day_index, resolve as _resolve_size_multiplier

logger = logging.getLogger(__name__)

FEE = 0.0006           # Taker (ausgeloeste Trigger-Limit ist marketable, Exits = Market)
SLIP_EXIT = 0.0005     # TP-/Market-Ausstieg
SLIP_ENTRY = 0.0002    # Fill vs. Trigger (live gemessen, siehe live_sim.py)
MIN_NOTIONAL = 5.0
CYCLE_MIN = 15
CYCLE_OFFSET_MIN = 0   # Cron */15 (README), Startverzoegerung je Strategie siehe CYCLE_START_S
CYCLE_START_S = 15     # Sekunden bis zur ersten Strategie (Start master_runner + API-Aufrufe)


class _Strat:
    def __init__(self, sid, info, grid, mark):
        self.sid = sid
        self.symbol = info['symbol']
        self.params = info['params']
        self.periods = info.get('active_periods')
        df, _ = calculate_indicators_and_signals(info['data'].copy(), self.params)
        df = df.reindex(info['data'].index)
        self.df = df
        self.cidx = df.index
        self.tf = df.index[1] - df.index[0]
        h, l, c = info['data']['high'], info['data']['low'], info['data']['close']
        self.adx = _ta.trend.adx(h, l, c, window=14).to_numpy()
        self.sma20 = _ta.trend.sma_indicator(c, window=20).to_numpy()
        self.sma50 = _ta.trend.sma_indicator(c, window=50).to_numpy()
        self.atr_pct = (_ta.volatility.average_true_range(h, l, c, window=14) / c).to_numpy()
        rp = self.params['risk']
        self.atr_sl = None
        if 'sl_to_env1_ratio' not in rp and 'stop_loss_atr_multiplier' in rp:
            self.atr_sl = _ta.volatility.average_true_range(h, l, c, window=rp.get('stop_loss_atr_period', 14)).to_numpy()
        m = mark.reindex(grid).ffill()
        self.o, self.h, self.l, self.c = (m[k].to_numpy(np.float64) for k in ('open', 'high', 'low', 'close'))
        # Zustand
        self.layers = []      # dict(side, band, entry, amount, sl, margin, entry_t, atr)
        self.orders = []      # dict(side, band, T, B, sl, amount, margin, up)
        self.tp = None        # dict(side, level, up)
        self.closed_since_cycle = []   # (side, band, sl)
        self.guard = {}       # (side, band) -> Kerzenindex, in dem nicht neu eroeffnet wird
        self.block = {'long': None, 'short': None}  # Kerzen-Zeitstempel der SL-Erkennung

    def side(self):
        return self.layers[0]['side'] if self.layers else None

    def in_team(self, t):
        return self.periods is None or any(a <= t and (b is None or t < b) for a, b in self.periods)


def run_portfolio_live_sim(start_capital, strategies_data, mark_1m, start_ts, end_ts, symbol_blocks=None,
                           size_multiplier='auto', max_open_strategies='settings', btc_trend=None, order=None):
    """strategies_data: {sid: {'symbol', 'timeframe', 'data' (Last-Preis-Kerzen inkl. Vorlauf), 'params',
    'active_periods' (optional)}}; mark_1m: {symbol: 1m-Mark-Kerzen}; start_ts/end_ts: UTC-Zeitpunkte.
    order: Reihenfolge der Strategien je Zyklus (live: Team-Liste), Standard = Schluessel-Reihenfolge.
    Rueckgabe wie portfolio_simulator: {'trades_df', 'open_positions', 'end_capital'}."""
    start_ts, end_ts = pd.Timestamp(start_ts), pd.Timestamp(end_ts)
    grid = pd.date_range(start_ts.floor('min'), end_ts.floor('min'), freq='1min', inclusive='left')
    if len(grid) == 0:
        return {'trades_df': pd.DataFrame(), 'open_positions': [], 'end_capital': start_capital}
    strats = []
    for sid in (order or list(strategies_data)):
        info = strategies_data.get(sid)
        if info is None:
            continue
        mk = mark_1m.get(info['symbol'])
        if mk is None or mk.empty:
            logger.warning(f"Keine 1m-Mark-Kerzen fuer {info['symbol']} -- {sid} nicht simuliert.")
            continue
        strats.append(_Strat(sid, info, grid, mk))
    mult = _resolve_size_multiplier(size_multiplier)
    max_open = max_concurrent_positions() if max_open_strategies == 'settings' else max_open_strategies
    blocks = symbol_blocks or []
    minutes = (grid.hour * 60 + grid.minute).to_numpy()
    is_cycle = (minutes % CYCLE_MIN) == CYCLE_OFFSET_MIN

    st = dict(equity=float(start_capital), used=0.0)
    trades = []

    def free():
        return max(0.0, st['equity'] - st['used'])

    def close_layer(s, lay, price, t, reason):
        e, a = lay['entry'], lay['amount']
        pnl = ((price - e) if lay['side'] == 'long' else (e - price)) * a - (e * a + price * a) * FEE
        st['equity'] += pnl
        st['used'] = max(0.0, st['used'] - lay['margin'])
        trades.append(dict(strategy_id=s.sid, symbol=s.symbol, side=lay['side'], band=lay['band'],
                           entry_time=lay['entry_t'], exit_time=t, entry_price=e, exit_price=price,
                           amount_coins=a, pnl_usd=pnl, exit_reason=reason))
        s.closed_since_cycle.append((lay['side'], lay['band'], lay['sl']))

    def close_all(s, price, t, reason):
        for lay in list(s.layers):
            px = price * (1 - SLIP_EXIT) if lay['side'] == 'long' else price * (1 + SLIP_EXIT)
            close_layer(s, lay, px, t, reason)
        s.layers = []
        s.tp = None
        s.orders = []

    def cycle(s, j, t, px):
        c = s.cidx.searchsorted(t, side='right') - 1   # laufende Kerze
        if c < 2 or pd.isna(s.df['average'].iat[c - 1]):
            return
        sig = s.df.iloc[c - 1]                          # letzte abgeschlossene Kerze
        p = s.params
        # Gleiche-Kerze-Sperre + Sperre nach SL fuer seit dem letzten Zyklus geschlossene Baender
        for side, band, sl in s.closed_since_cycle:
            s.guard[(side, band)] = c
            if reentry_blocks_until_cross(p) and s.block[side] is None:
                if (px <= sl * 1.05) if side == 'long' else (px >= sl * 0.95):
                    s.block[side] = s.cidx[c - 1]
        s.closed_since_cycle = []
        blocked = set()
        if reentry_blocks_until_cross(p):
            for side in ('long', 'short'):
                if s.block[side] is not None and s.cidx[c - 1] > s.block[side] and reentry_block_cleared(
                        side, sig['close'], sig.get(average_col(side), sig['average'])):
                    s.block[side] = None
            blocked |= {k for k, v in s.block.items() if v is not None}
        if btc_filter_enabled(p):
            up, b50 = btc_trend_up_at(btc_trend, t), btc_below50_at(btc_trend, t)
            blocked |= {side for side in ('long', 'short') if not btc_side_allowed(p, side, up, b50)}
            if short_regime_exit(p) and up and s.side() == 'short':
                close_all(s, px, t, 'REGIME')
                return
        s.orders = []                                   # alle Entry-Orders stornieren
        in_team = s.in_team(t)
        sym_blocked = any(sym == s.symbol and a <= t and (b is None or t < b) for sym, a, b in blocks)
        if s.layers:
            side = s.side()
            tp = sig.get(average_col(side), sig['average'])
            if tp_already_reached(side, px, tp):
                close_all(s, px, t, 'TP')
                return
            if tp is not None and tp > 0 and not pd.isna(tp):
                s.tp = dict(side=side, level=float(tp), up=px < tp)
            if not in_team:
                return
            place_entries(s, c, sig, px, restrict=side, blocked=blocked, t=t)
        else:
            s.tp = None
            if not in_team or sym_blocked:
                return
            n_open = len({x.symbol for x in strats if x.layers})
            if not concurrency_allows_new(n_open, max_open):
                return
            if free() <= 1:
                return
            place_entries(s, c, sig, px, restrict=None, blocked=blocked, t=t)

    def place_entries(s, c, sig, px, restrict, blocked, t):
        p = s.params
        sp, rp, bp = p['strategy'], p['risk'], p['behavior']
        i = c - 1
        regime, allowed, trend = classify_regime(s.adx[i], sig['close'], sig['average'], s.sma20[i], s.sma50[i], sp)
        if regime == 'STRONG_TREND' or not allowed:
            return
        use = {'long': bp.get('use_longs', True) and trend != 'DOWNTREND' and 'long' not in blocked,
               'short': bp.get('use_shorts', True) and trend != 'UPTREND' and 'short' not in blocked}
        balance = free()
        n_env = len(sp['envelopes'])
        delta = sp.get('trigger_price_delta_pct', 0.05) / 100.0
        lev = rp['leverage']
        atr = s.atr_sl[i] if s.atr_sl is not None else None
        m = mult.get(day_index(t), (1.0,))[0] if mult else 1.0
        committed = {(l['side'], l['band']) for l in s.layers}
        cyc_used = 0.0
        for side in ('long', 'short'):
            if not use[side] or restrict not in (None, side):
                continue
            for k in range(1, n_env + 1):
                if (side, k) in committed or s.guard.get((side, k)) == c:
                    continue
                B = sig.get(f'band_low_{k}' if side == 'long' else f'band_high_{k}')
                if B is None or pd.isna(B) or B <= 0:
                    continue
                if not is_touch_mode(p):
                    if (side == 'long' and sig['close'] > B) or (side == 'short' and sig['close'] < B):
                        continue
                sl = compute_band_sl_price(side, B, k - 1, p, regime, atr)
                if sl is None or entry_blocked_by_sl(side, px, sl) or entry_blocked_by_sl(side, sig['close'], sl):
                    continue
                if abs(B - sl) <= 0:
                    continue
                if uses_fraction_sizing(p):
                    amt = fraction_band_amount(balance, p, B, n_env, side=side, size_mult=m)
                    if amt is None:
                        continue
                else:
                    amt = balance * rp.get('risk_per_entry_pct', 0.5) / 100.0 / abs(B - sl) * m
                if amt * B < MIN_NOTIONAL:
                    continue
                marg = calculate_position_margin(amt, B, side_leverage(p, side))
                if not margin_fits(cyc_used, marg, balance):
                    continue
                cyc_used += marg
                T = B * (1 - delta) if side == 'long' else B * (1 + delta)
                s.orders.append(dict(side=side, band=k, T=T, B=B, sl=sl, amount=amt, margin=marg, up=px < T,
                                     atr=s.atr_pct[i]))

    def exits(s, j, t):
        o, h, l = s.o[j], s.h[j], s.l[j]
        tp_px = None
        if s.tp is not None:
            lv = s.tp['level']
            if s.tp['up'] and h >= lv:
                tp_px = max(lv, o)
            elif not s.tp['up'] and l <= lv:
                tp_px = min(lv, o)
        keep = []
        for lay in s.layers:
            sl = lay['sl']
            if lay['side'] == 'long' and l <= sl:
                close_layer(s, lay, stop_fill_price('long', min(sl, o), lay['atr']), t, 'SL')
            elif lay['side'] == 'short' and h >= sl:
                close_layer(s, lay, stop_fill_price('short', max(sl, o), lay['atr']), t, 'SL')
            elif tp_px is not None:
                px = tp_px * (1 - SLIP_EXIT) if lay['side'] == 'long' else tp_px * (1 + SLIP_EXIT)
                close_layer(s, lay, px, t, 'TP')
            else:
                keep.append(lay)
        s.layers = keep
        if not s.layers:
            s.tp = None

    def triggered(s, j):
        """Ausgeloeste Orders dieser Minute: [(Abstand Open->Trigger relativ, s, order, fill)]."""
        o, h, l = s.o[j], s.h[j], s.l[j]
        hits, rest = [], []
        for od in s.orders:
            side, T, B = od['side'], od['T'], od['B']
            fill = None
            if side == 'long':
                if od['up']:
                    if h >= T:
                        fill = T if o < T else (o if o <= B else (B if l <= B else None))
                elif l <= T:
                    fill = min(T, o)
            else:
                if not od['up']:
                    if l <= T:
                        fill = T if o > T else (o if o >= B else (B if h >= B else None))
                elif h >= T:
                    fill = max(T, o)
            if fill is None:
                rest.append(od)
            else:
                hits.append((abs(o - T) / o if o > 0 else 0.0, s, od, fill))
        s.orders = rest
        return hits

    def fill_order(s, od, fill, j, t):
        side, sl = od['side'], od['sl']
        if s.layers and s.side() != side:
            return  # Gegenseite bei offener Position (One-Way) -- verworfen
        if not margin_fits(st['used'], od['margin'], st['equity']):
            return  # Bitget lehnt beim Ausloesen mangels freier Margin ab
        h, l, cl = s.h[j], s.l[j], s.c[j]
        fill = fill * (1 + SLIP_ENTRY) if side == 'long' else fill * (1 - SLIP_ENTRY)
        lay = dict(side=side, band=od['band'], entry=fill, amount=od['amount'], sl=sl, margin=od['margin'],
                   entry_t=t, atr=od['atr'])
        st['used'] += od['margin']
        # SL im selben Bar: Bewegung Richtung SL laeuft durch den Trigger
        same_bar_sl = ((side == 'long' and not od['up'] and l <= sl) or (side == 'short' and od['up'] and h >= sl)
                       or (side == 'long' and od['up'] and cl <= sl) or (side == 'short' and not od['up'] and cl >= sl))
        if same_bar_sl:
            close_layer(s, lay, stop_fill_price(side, sl, od['atr']), t, 'SL')
            return
        s.layers.append(lay)

    # Cron */15 startet master_runner, der die Strategien nacheinander startet (2 s Abstand, je Zyklus
    # einige Sekunden API-Aufrufe): Strategie i (Team-Reihenfolge) ist ~(2*i + 15) s nach Zyklusbeginn
    # dran, bis dahin bleiben ihre alten Orders aktiv. Live 2026-11: DOGE 1h (Nr. 38, ~91 s) bekam um
    # 03:01:09 noch einen Fill der 02:46-Order, CYBER 1h (Nr. 24, ~63 s) um 23:01:16 schon einen der
    # neuen Order. Modell: der Zyklus wirkt vor der Minute, die diesem Zeitpunkt am naechsten beginnt.
    delay_min = [int(round((2 * i + CYCLE_START_S) / 60)) for i in range(len(strats))]
    curve_t, curve_eq = [], []
    for j, t in enumerate(grid):
        if is_cycle[j]:   # Kapitalkurve inkl. offener Positionen (Mark-Open), alle 15 Min
            eq = st['equity']
            for s in strats:
                for lay in s.layers:
                    eq += ((s.o[j] - lay['entry']) if lay['side'] == 'long' else (lay['entry'] - s.o[j])) * lay['amount']
            curve_t.append(t); curve_eq.append(eq)
        for i, s in enumerate(strats):
            jc = j - delay_min[i]
            if jc >= 0 and is_cycle[jc]:
                cycle(s, j, t, px=s.o[j])
        for s in strats:
            if s.layers:
                exits(s, j, t)
        # In derselben Minute ausgeloeste Orders: naeher am Minuten-Open = frueher beruehrt
        hits = [x for s in strats if s.orders for x in triggered(s, j)]
        hits.sort(key=lambda x: x[0])
        for _, s, od, fill in hits:
            fill_order(s, od, fill, j, t)

    open_positions = []
    for s in strats:
        for lay in s.layers:
            open_positions.append(dict(strategy_id=s.sid, side=lay['side'], entry_time=lay['entry_t'],
                                       entry_price=lay['entry'], amount_coins=lay['amount'], band=lay['band']))
    tdf = pd.DataFrame(trades)
    curve = pd.Series(curve_eq, index=pd.DatetimeIndex(curve_t), dtype=float)
    max_dd = float(((curve.cummax() - curve) / curve.cummax()).max() * 100) if len(curve) else 0.0
    end_eq = st['equity'] + sum(((s.c[-1] - l['entry']) if l['side'] == 'long' else (l['entry'] - s.c[-1])) * l['amount']
                                for s in strats for l in s.layers)
    return {'trades_df': tdf, 'open_positions': open_positions, 'end_capital': st['equity'],
            'end_equity': end_eq, 'equity_curve': curve, 'max_drawdown_pct': max_dd}


# --- Adapter fuer den Portfolio-Optimizer (gleiche Schnittstelle/Ergebnis wie run_portfolio_simulation) ---

_MARK_MEM = {}   # symbol -> 1m-Mark-Kerzen (je Prozess einmal geladen; Team-Laeufe wiederholen sich)


def _marks_for(symbols, start_ts, end_ts, log=None):
    from ltbbot.analysis.backtester import prefetch_mark_1m, load_mark_data
    need = sorted({s for s in symbols if s not in _MARK_MEM})
    if need:
        prefetch_mark_1m(need, start_ts.strftime('%Y-%m-%d'), end_ts.strftime('%Y-%m-%d'), log=log)
        for s in need:
            m = load_mark_data(s, '1m', start_ts.strftime('%Y-%m-%d'), end_ts.strftime('%Y-%m-%d'))
            if m is not None and not m.empty:
                _MARK_MEM[s] = m[['open', 'high', 'low', 'close']].astype(np.float64)
    return {s: _MARK_MEM[s].loc[start_ts:end_ts] for s in symbols if s in _MARK_MEM}


def simulate_team(start_capital, strategies_data, start_date, end_date, size_multiplier='auto',
                  max_open_strategies='settings', log=None):
    """Wie portfolio_simulator.run_portfolio_simulation(start_capital, strategies_data, start_date, end_date),
    aber mit dem Live-Takt-Simulator (1m-Mark, 15-Min-Zyklen, Margin beim Ausloesen). start_date/end_date
    'YYYY-MM-DD' (end_date einschliesslich). strategies_data[id]: symbol, timeframe, data (inkl. Vorlauf),
    params. Rueckgabe mit denselben Schluesseln, die Optimizer/Berichte lesen."""
    from ltbbot.analysis.backtester import load_btc_trend
    start_ts = pd.Timestamp(start_date, tz='UTC')
    end_ts = min(pd.Timestamp(end_date, tz='UTC') + pd.Timedelta(days=1), pd.Timestamp.now(tz='UTC').floor('min'))
    sd = {}
    for sid, info in strategies_data.items():
        d = info['data']
        tf = pd.Timedelta(info['timeframe'].replace('m', 'min')) if info['timeframe'].endswith('m') else pd.Timedelta(info['timeframe'])
        d = d[~d.index.duplicated()].sort_index()
        sd[sid] = dict(info, data=d[d.index + tf <= end_ts])   # nur abgeschlossene Kerzen
    marks = _marks_for([v['symbol'] for v in sd.values()], start_ts, end_ts, log=log)
    needs_btc = any(btc_filter_enabled(v['params']) for v in sd.values())
    res = run_portfolio_live_sim(start_capital, sd, marks, start_ts, end_ts, size_multiplier=size_multiplier,
                                 max_open_strategies=max_open_strategies,
                                 btc_trend=load_btc_trend(start_ts, end_ts) if needs_btc else None)
    tdf = res['trades_df']
    if not tdf.empty:
        tdf = tdf.copy()
        tdf['timeframe'] = tdf['strategy_id'].map(lambda k: strategies_data[k]['timeframe'])
        tdf['leverage'] = [side_leverage(strategies_data[k]['params'], sd_) for k, sd_ in zip(tdf['strategy_id'], tdf['side'])]
        notional = tdf['entry_price'] * tdf['amount_coins']
        tdf['pnl_pct'] = np.where(notional > 0, tdf['pnl_usd'] / notional * 100, 0.0)
        tdf['reason'] = np.where(tdf['pnl_usd'] > 0, 'WIN', 'SL')
    curve = res['equity_curve']
    eq_df = pd.DataFrame({'equity': curve.values}, index=pd.DatetimeIndex(curve.index, name='timestamp'))
    end_eq = res['end_equity']
    n = len(tdf)
    return {
        'start_capital': start_capital, 'end_capital': end_eq,
        'total_pnl_pct': (end_eq / start_capital - 1) * 100 if start_capital else 0.0,
        'trade_count': n, 'win_rate': float((tdf['pnl_usd'] > 0).mean() * 100) if n else 0.0,
        'max_drawdown_pct': res['max_drawdown_pct'], 'max_drawdown_date': None,
        'min_equity': float(curve.min()) if len(curve) else start_capital,
        'liquidation_date': None if end_eq > 0 else end_ts,
        'trades_df': tdf, 'equity_curve': eq_df, 'open_positions': res['open_positions'],
        'pnl_per_strategy': tdf.groupby('strategy_id')['pnl_usd'].sum().reset_index().rename(columns={'pnl_usd': 'pnl'}) if n else pd.DataFrame(columns=['strategy_id', 'pnl']),
        'trades_per_strategy': tdf.groupby('strategy_id').size().reset_index(name='trades') if n else pd.DataFrame(columns=['strategy_id', 'trades']),
        'simulator': 'live',
    }
