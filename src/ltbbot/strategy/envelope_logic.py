# src/ltbbot/strategy/envelope_logic.py
import pandas as pd
import numpy as np
import ta
import logging

logger = logging.getLogger(__name__)


def calculate_position_margin(amount_coins: float, entry_price: float, leverage: float) -> float:
    """Isolierte Margin einer Position, exakt wie Bitget sie anzeigt: Notional/Hebel.

    Geteilte Funktion fuer Live (trade_manager.py) UND Backtest/Portfolio-Simulator
    (backtester.py, portfolio_simulator.py) -- 2026-09-02 eingefuehrt, nachdem die
    Margin-Verfuegbarkeitspruefung zunaechst dreimal separat inline kopiert wurde.
    Live und Backtest muessen bei sowas immer dieselbe Funktion nutzen, sonst
    driften sie unbemerkt auseinander (siehe [[feedback_live_backtest_must_match]]).
    """
    if not leverage:
        return 0.0
    return (amount_coins * entry_price) / leverage


def margin_fits(used_margin: float, margin_required: float, available_capital: float) -> bool:
    """True, wenn eine zusaetzliche Position noch in die freie Margin passt.

    available_capital ist bei Live der echte, gerade abgerufene Kontostand
    (spiegelt bereits alle anderen offenen Positionen wider), bei Backtest/
    Portfolio-Simulator das aktuelle realisierte Kapital. Reicht es nicht, wuerde
    die Order live mit InsufficientFunds abgelehnt -- also NICHT oeffnen, statt
    auf Kredit zu simulieren/handeln.
    """
    return (used_margin + margin_required) <= available_capital


# ---------------------------------------------------------------------------
# RobotTraders-Original-Modus (2026-10-05)
# ---------------------------------------------------------------------------
# Ursprung des ltbbot (github.com/RobotTraders, Envelope): Orders liegen direkt an
# den Baendern (kein Close-Bestaetigungs-Rueckweg), keine ADX-/Trend-Sperren, weiter
# Not-SL, TP an der wandernden Mitte, nach einem SL bleibt die Seite gesperrt bis
# ein Close wieder jenseits der Mitte liegt. Ehrlich getestet (Scratchpad rt_check.py,
# 846 Binance-Perps inkl. delisteter, OOS ab 2025-11-08): nur Long + BTC>SMA200
# +0.86 %/Trade, t=6.5. Alles per Config-Schalter -- ohne die Schalter bleibt die
# bisherige Logik unveraendert. Jede Funktion hier wird von Live (trade_manager.py),
# backtester.py UND portfolio_simulator.py geteilt.

MIN_NOTIONAL_USDT = 5.0  # Bitget-Mindest-Ordergroesse (Notional)


def is_touch_mode(params):
    """Orders direkt am Band (RobotTraders) statt Close-Bestaetigung + Ruecklauf-Trigger."""
    return params.get('strategy', {}).get('entry_mode') == 'touch'


def regime_filter_enabled(params):
    return params.get('strategy', {}).get('regime_filter', True)


def btc_filter_enabled(params):
    return bool(params.get('strategy', {}).get('btc_trend_filter', False))


def reentry_blocks_until_cross(params):
    return params.get('strategy', {}).get('reentry_after_sl') == 'cross_average'


def btc_trend_series(btc_daily, sma_period=200, with_sma50=False):
    """BTC-Trend aus Tageskerzen: True wenn Tages-Close > SMA200.

    Index = Zeitpunkt, AB dem der Wert bekannt ist (Tages-Open + 1 Tag = Kerzenschluss).
    Abfrage fuer eine Kerze mit Open ts per asof(ts) -- live identisch: letzte
    ABGESCHLOSSENE BTC-Tageskerze (drop_incomplete_last_candle)."""
    if btc_daily is None or len(btc_daily) == 0:
        return pd.DataFrame(columns=['up', 'below50']) if with_sma50 else pd.Series(dtype=bool)
    close = btc_daily['close'].astype(float)
    sma = close.rolling(sma_period).mean()
    up = (close > sma)[sma.notna()]
    if with_sma50:
        # zusaetzlich BTC-Close < SMA50 (strenger Baerenfilter fuer Shorts, siehe btc_side_allowed)
        below50 = (close < close.rolling(50).mean())[sma.notna()]
        out = pd.DataFrame({'up': up, 'below50': below50})
        out.index = out.index + pd.Timedelta(days=1)
        return out
    up.index = up.index + pd.Timedelta(days=1)
    return up


def btc_trend_up_at(btc_trend, ts):
    """BTC-Trend zum Zeitpunkt ts (None, wenn unbekannt)."""
    if btc_trend is None or len(btc_trend) == 0:
        return None
    pos = btc_trend.index.searchsorted(ts, side='right') - 1
    if pos < 0:
        return None
    if isinstance(btc_trend, pd.DataFrame):
        return bool(btc_trend['up'].iloc[pos])
    return bool(btc_trend.iloc[pos])


def btc_below50_at(btc_trend, ts):
    """BTC-Tagesclose < SMA50 zum Zeitpunkt ts (None ohne SMA50-Daten)."""
    if not isinstance(btc_trend, pd.DataFrame) or len(btc_trend) == 0:
        return None
    pos = btc_trend.index.searchsorted(ts, side='right') - 1
    return None if pos < 0 else bool(btc_trend['below50'].iloc[pos])


def btc_side_allowed(params, side, btc_up, btc_below50=None):
    """Long nur bei BTC-Aufwaertstrend, Short nur bei Abwaertstrend (wenn Filter aktiv).
    Unbekannter Trend (zu wenig BTC-Historie) -> kein Einstieg.
    Short mit strategy.short.btc_filter = 'sma200_sma50': zusaetzlich BTC-Close < SMA50
    (keine neuen Shorts in Erholungsrallyes innerhalb des Baerenmarkts)."""
    if not btc_filter_enabled(params):
        return True
    if btc_up is None:
        return False
    if side == 'long':
        return btc_up
    if btc_up:
        return False
    sp = short_params(params) or {}
    if sp.get('btc_filter') == 'sma200_sma50':
        return btc_below50 is True
    return True


def short_regime_exit(params):
    """Offene Shorts schliessen, sobald BTC wieder ueber der SMA200 schliesst (strategy.short.regime_exit)."""
    return bool((short_params(params) or {}).get('regime_exit')) and btc_filter_enabled(params)


def tp_already_reached(side, price, tp_price):
    """Kurs steht schon auf/jenseits des TP (Mitte der Positionsseite).

    Backtester: oeffnet eine Kerze jenseits des TP -> Ausstieg zum Open ('TP', tp_gap).
    Live muss dann sofort per Market schliessen: ein Trigger-TP auf der falschen Seite
    des Kurses deutet Bitget als Stop ('Mark <= TP' bei Long) -- die Position bliebe
    offen (LPT 4h, 2026-10-08: Mitte fiel unter den Kurs, TP wurde zum Stop)."""
    if price is None or tp_price is None or not tp_price > 0 or not price > 0:
        return False
    return price >= tp_price if side == 'long' else price <= tp_price


def reentry_block_cleared(side, close_price, average_price):
    """Sperre nach SL aufheben: Close wieder jenseits der Mitte (Long: darueber)."""
    if close_price is None or average_price is None or pd.isna(close_price) or pd.isna(average_price):
        return False
    return close_price > average_price if side == 'long' else close_price < average_price


def fraction_band_amount(free_capital, params, band_price, num_bands, amount_step=None, side='long'):
    """Positionsgroesse im Kapitalanteil-Modus (risk.sizing == 'fraction').

    Marge je Band = freies Kapital * position_size_pct / Anzahl Baender, Notional =
    Marge * Hebel. Liegt das unter Bitgets Mindest-Notional (5 USDT), wird auf 5 USDT
    angehoben (min_notional_bump, Standard an) -- sonst koennte ein kleines Konto
    gar nicht handeln. Ob die Marge dann noch frei ist, prueft margin_fits() beim
    Aufrufer (live wie Backtest). Returns amount_coins oder None.

    amount_step: Mengen-Schrittweite des Kontrakts (Bitget, z.B. LINK 1 Coin). Die Menge wird
    darauf AUFGERUNDET -- abgerundet laege sie unter dem Mindest-Notional bzw. bei 0 (LINK:
    5 USDT = 0.36 Coins -> 0). Live: Markt-Praezision, Backtest: params['market']['amount_step']."""
    risk = params.get('risk', {})
    if band_price is None or band_price <= 0 or num_bands <= 0 or free_capital <= 0:
        return None
    lev = risk.get('leverage', 1) or 1
    pct = risk.get('position_size_pct', 30.0)
    if side == 'short' and risk.get('short_position_size_pct') is not None:
        pct = risk['short_position_size_pct']  # eigene (kleinere) Short-Groesse
    margin = free_capital * pct / 100.0 / num_bands
    notional = margin * lev
    if notional < MIN_NOTIONAL_USDT:
        if not risk.get('min_notional_bump', True):
            return None
        notional = MIN_NOTIONAL_USDT * 1.01  # knapp drueber (Rundung auf Kontraktgroesse)
    amount = notional / band_price
    step = amount_step if amount_step is not None else params.get('market', {}).get('amount_step')
    if step:
        amount = round(np.ceil(amount / step - 1e-9) * step, 10)
    return amount


def uses_fraction_sizing(params):
    return params.get('risk', {}).get('sizing') == 'fraction'


# Short-Seite mit eigenen Parametern (2026-10-05): die gespiegelten Long-Werte (DCM 5, 7 %) verloren,
# EMA 20 + Baender ab 10 % + SL 30 % + Short nur bei BTC < SMA200 bestand praeregistriert auf dem
# ungesehenen Zeitraum 2020-06..2023-09 (Binance 846 Perps inkl. delisteter: +0.49 %/Trade, t=3.8)
# und im OOS 11/2025-10/2026 (+0.42 %, t=3.1; Bitget +1.09 %). Schwach in kurzen Einbruechen im
# Bullenmarkt (10/2023-11/2025: -0.39 %). Konfig: strategy.short = {average_type, average_period,
# envelopes}, risk.short_stop_loss_pct. Ohne strategy.short gelten die Long-Werte fuer beide Seiten.

def max_concurrent_positions(settings=None):
    """Hoechstzahl gleichzeitig offener Positionen (Strategien) ueber das ganze Konto
    (settings.json::live_trading_settings.max_concurrent_positions, 2026-10-06 User: 10).
    None = unbegrenzt. Geteilt von Live (trade_manager) und portfolio_simulator."""
    if settings is None:
        try:
            import json as _json, os as _os
            _root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..', '..'))
            with open(_os.path.join(_root, 'settings.json')) as _f:
                settings = _json.load(_f)
        except Exception:
            return None
    v = (settings.get('live_trading_settings') or {}).get('max_concurrent_positions')
    return int(v) if v else None


def concurrency_allows_new(open_count, limit):
    """Neue Position (auf einem Symbol ohne Position) nur, solange weniger als `limit` offen sind."""
    return limit is None or open_count < limit


def short_params(params):
    """Eigener Short-Block (oder None -> Short nutzt Mitte/Baender der Long-Seite)."""
    return params.get('strategy', {}).get('short') or None


def average_col(side):
    """Spalte der Mitte (= TP und Sperr-Referenz) fuer eine Seite."""
    return 'average_short' if side == 'short' else 'average'


def _compute_average(df, avg_type, avg_period):
    if avg_type == 'DCM':
        return ta.volatility.DonchianChannel(df['high'], df['low'], df['close'], window=avg_period).donchian_channel_mband()
    if avg_type == 'SMA':
        return ta.trend.sma_indicator(df['close'], window=avg_period)
    if avg_type == 'EMA':
        return ta.trend.ema_indicator(df['close'], window=avg_period)
    if avg_type == 'WMA':
        return ta.trend.wma_indicator(df['close'], window=avg_period)
    raise ValueError(f"Ungültiger average_type: {avg_type}")


def classify_regime(adx_value, close_price, average_price, sma20, sma50, strategy_params=None):
    """Regime-Entscheidung aus den Indikatorwerten EINER abgeschlossenen Kerze.

    Geteilte Funktion fuer Live (detect_market_regime) UND Backtest/Portfolio-
    Simulator -- vorher dreimal inline kopiert, dabei driftete portfolio_simulator.py
    ab (kannte die Regime-Gate-Overrides nicht).

    Returns: (regime, trade_allowed, trend_direction)
    """
    strategy_params = strategy_params or {}
    if not strategy_params.get('regime_filter', True):
        # RobotTraders-Modus: keine ADX-/Trend-Sperren, kein 1.5x-SL im Trend
        return "UNCERTAIN", True, "NEUTRAL"
    disable_strong_trend_block = strategy_params.get('disable_strong_trend_block', False)
    strong_trend_adx_threshold = strategy_params.get('strong_trend_adx_threshold', 30.0)

    adx_value = float(adx_value) if pd.notna(adx_value) else 20.0
    if pd.notna(average_price) and average_price > 0 and pd.notna(close_price):
        price_distance_pct = abs(close_price - average_price) / average_price * 100
    else:
        price_distance_pct = 0.0

    if pd.notna(sma20) and pd.notna(sma50) and sma50 > 0 and sma20 > sma50 * 1.02:
        trend_direction = "UPTREND"
    elif pd.notna(sma20) and pd.notna(sma50) and sma50 > 0 and sma20 < sma50 * 0.98:
        trend_direction = "DOWNTREND"
    else:
        trend_direction = "NEUTRAL"

    if adx_value > strong_trend_adx_threshold:
        if disable_strong_trend_block:
            return "TREND", True, trend_direction
        return "STRONG_TREND", False, trend_direction
    if adx_value > 25:
        return "TREND", True, trend_direction
    if adx_value < 20 and price_distance_pct < 3:
        return "RANGE", True, "NEUTRAL"
    return "UNCERTAIN", True, trend_direction


def compute_band_sl_price(side, band_price, band_index, params, regime, atr_value=None):
    """Feste SL fuer ein Band (Prioritaet: sl_to_env1_ratio -> ATR-Multiplikator -> fixer %),
    im TREND/STRONG_TREND 1.5x breiter. band_index ist 0-basiert.

    Geteilte Funktion fuer Live (place_entry_orders) UND Backtest/Portfolio-Simulator.
    Gibt None zurueck, wenn kein gueltiger SL berechnet werden kann.
    """
    risk_params = params['risk']
    envelopes = params['strategy'].get('envelopes', [0.03, 0.05, 0.08])
    sl_multiplier = 1.5 if regime in ("TREND", "STRONG_TREND") else 1.0

    if side == 'short' and risk_params.get('short_stop_loss_pct') is not None:
        sl_pct = risk_params['short_stop_loss_pct'] / 100.0 * sl_multiplier
    elif 'sl_to_env1_ratio' in risk_params:
        env_pct = envelopes[band_index] if band_index < len(envelopes) else envelopes[0]
        sl_pct = env_pct * risk_params['sl_to_env1_ratio'] * sl_multiplier
    elif 'stop_loss_atr_multiplier' in risk_params:
        min_sl_pct = risk_params.get('min_stop_loss_pct', 0.5) / 100.0
        if atr_value is not None and pd.notna(atr_value) and atr_value > 0 and band_price > 0:
            sl_pct = max(float(atr_value) * risk_params['stop_loss_atr_multiplier'] / band_price, min_sl_pct)
        else:
            sl_pct = min_sl_pct
        sl_pct *= sl_multiplier
    else:
        sl_pct = risk_params['stop_loss_pct'] / 100.0 * sl_multiplier

    sl_price = band_price * (1 - sl_pct) if side == 'long' else band_price * (1 + sl_pct)
    return sl_price if sl_price > 0 else None


# Zusaetzliche Stop-Slippage im Backtest als Anteil der typischen Kerzenbewegung
# (ATR/Close der letzten abgeschlossenen Kerze). 2026-09-30: Backtester/Portfolio-Sim
# fuellten jeden Stop exakt zum SL-Preis (+ fixe 0.05%). Bei engen Stops auf
# volatilen Coins (US 4h: SL 0.13%, Kerze ~7%) rutscht eine Stop-Market-Order live
# aber ein Vielfaches davon weiter -- der Optimizer bevorzugte deshalb systematisch
# Rausch-Stops mit riesigem R:R, die live nicht halten. Mit volatilitaetsskalierter
# Slippage (BTC ~0.07%, US ~0.36%) sind solche Configs im Backtest nicht mehr
# attraktiv. Wert konservativ geschaetzt; kalibrieren, sobald echte SL-Fills gemessen.
STOP_SLIPPAGE_ATR_FRACTION = 0.05


def stop_fill_price(side, stop_price, atr_pct):
    """Realistischer Fill einer ausgeloesten Stop-Market-Order (geteilt: backtester.py,
    portfolio_simulator.py). atr_pct als Dezimal (0.03 = 3%)."""
    if atr_pct is None or atr_pct != atr_pct or atr_pct <= 0:
        return stop_price
    extra = STOP_SLIPPAGE_ATR_FRACTION * atr_pct
    return stop_price * (1 - extra) if side == 'long' else stop_price * (1 + extra)


def median_atr_pct(df, period=14):
    """Typische Kerzenbewegung: Median von ATR/Close ueber den Datensatz (Dezimal, 0.03 = 3%)."""
    if df is None or len(df) <= period:
        return float('nan')
    atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=period)
    return float((atr / df['close']).median())


def sl_atr_fraction(params, atr_pct):
    """Stop-Abstand von Band 1 als Anteil der typischen Kerzenbewegung (median ATR%).

    2026-09-30: Optimizer fand Configs mit Stops von 2-5% einer normalen Kerze (z.B.
    US 4h: SL 0.13% bei 7.2% ATR). Im Backtest wird jeder Stop exakt zum SL-Preis
    gefuellt; live rutscht eine Stop-Market-Order in solchen Bewegungen leicht ein
    Vielfaches des SL-Abstands weiter -> solche Configs sind live nicht belastbar.
    Nur fuer den SL-Modus sl_to_env1_ratio (einziger Optimizer-Modus), sonst None.
    """
    ratio = params.get('risk', {}).get('sl_to_env1_ratio')
    envelopes = params.get('strategy', {}).get('envelopes') or []
    if ratio is None or not envelopes or not atr_pct or atr_pct != atr_pct:
        return None
    return sorted(envelopes)[0] * ratio / atr_pct


def band_structure_ok(params, atr_pct, min_env1_atr, min_gap_atr):
    """Bandabstaende relativ zur typischen Kerzenbewegung (median ATR%, Dezimal).

    2026-09-30: Der Optimizer suchte Baender in festen Prozent (Band 1 ab 0.5%,
    Abstand ab 0.5%). Auf volatilen Coins lagen dadurch alle drei Baender praktisch
    auf dem MA und aufeinander (US 4h: 0.5/1.06/1.81% bei ~5-7% Kerzenbewegung) --
    die drei Einstiege loesen dann fast gleichzeitig aus (= dreifaches Risiko an
    einem Punkt statt gestaffelter Position), und Band 1 liegt im Rauschen.
    Regel: Band 1 >= min_env1_atr * ATR vom MA, jede weitere Luecke >= min_gap_atr * ATR.
    Geteilt von optimizer.py (Suchraum + Neubewertung) und sync_confirmed_flags.py.
    """
    if params.get('strategy', {}).get('entry_mode') == 'touch':
        return True  # RobotTraders-Modus: Baender bewusst in festen Prozent (7/11/15 %), weit ausserhalb der ATR
    env = sorted(params.get('strategy', {}).get('envelopes') or [])
    if not env or not atr_pct or atr_pct != atr_pct:
        return True
    if env[0] < min_env1_atr * atr_pct:
        return False
    return all((b - a) >= min_gap_atr * atr_pct for a, b in zip(env, env[1:]))


def entry_blocked_by_sl(side, current_price, sl_price):
    """Live ueberspringt ein Band, wenn der aktuelle Preis schon jenseits dessen SL liegt
    (Entry wuerde sofort gestoppt). Geteilt mit Backtest/Portfolio-Simulator."""
    if current_price is None or sl_price is None:
        return False
    return current_price < sl_price if side == 'long' else current_price > sl_price


def simulate_entry_fill(side, candle_open, candle_high, candle_low, candle_close, trigger_price, sl_price,
                        fine_bars=None):
    """Fill einer Live-Entry-Order (Bitget-Trigger-Limit) innerhalb EINER Coarse-Kerze,
    deren Baender aus der vorherigen, abgeschlossenen Kerze stammen (wie live).

    Live-Mechanik (siehe live_sim.py, an echten Fills 2026-08/09 geprueft):
    - Liegt der Preis bei Platzierung (~Kerzen-Open) schon jenseits des SL, wird
      das Band uebersprungen (entry_blocked_by_sl). Live gilt das nur fuer den
      jeweiligen 15-Min-Zyklus (kommt der Preis zurueck, wird spaeter noch
      platziert) -- auf Coarse-Kerzen nicht exakt abbildbar. Bewusst die
      KONSERVATIVE Wahl: Abgleich gegen live_sim.py (1m, Sept. 2026, 5 Configs)
      ergab Skip = 38 Trades/WR 18.4%/-5.35 USDT, Nicht-Skip = 89/24.7%/+5.48,
      live_sim = 64/18.7%/-8.30. Skip unterschaetzt die Trade-Anzahl, trifft
      aber Winrate und PnL -- fuer Optimizer/OOS-Gate sind Scheinfunde teurer.
    - Die Ausloese-Richtung der Plan-Order ergibt sich aus Trigger vs. Preis bei
      Platzierung. Liegt der Preis schon jenseits des Triggers (typisch nach
      Close-Confirmation), feuert sie erst, wenn der Preis ZURUECK zum Trigger
      laeuft. Fill ~ Triggerpreis (Limit am Band ist dann marketable).
    - SL-Treffer in der Entry-Kerze:
      * Order wartet auf Bewegung Richtung SL (Long: Preis faellt zum Trigger):
        wird das SL-Niveau in der Kerze erreicht, lief der Preis zwingend erst
        durch den Trigger -> Stop.
      * Order wartet auf Ruecklauf (Long: Preis steigt zum Trigger): ein SL-Kontakt
        VOR dem Fill loest keinen Stop aus (reduceOnly-SL ohne Position). Die
        Reihenfolge ist auf Coarse-Kerzen unbekannt -- als Stop gewertet, wenn
        die Kerze jenseits des SL schliesst (gleiche Regel wie live_sim.py je 1m-Bar).

    fine_bars: optionale Fein-Kerzen (DataFrame open/high/low/close) GENAU dieser
      Coarse-Kerze -- oder ein Callable, das sie liefert (wird nur im Ruecklauf-Fall
      aufgerufen). Damit wird die Reihenfolge Fill -> SL im Ruecklauf-Fall real
      aufgeloest statt per Close-Regel (2026-10-03, AVAX 2h live: Fill 22:01, SL
      22:16 in derselben Kerze; Kerze schloss ueber dem SL -> Backtest hielt den
      Trade bis zum TP, +1.41 statt -0.20 USDT).

    Returns: (fill_price, stopped_in_entry_candle) oder (None, False) ohne Fill.
    """
    if entry_blocked_by_sl(side, candle_open, sl_price):
        return None, False
    if side == 'long':
        if candle_open > trigger_price:
            if candle_low > trigger_price:
                return None, False
            return trigger_price, bool(candle_low <= sl_price)
        if candle_high < trigger_price:
            return None, False
        return trigger_price, _return_fill_stopped(side, fine_bars, trigger_price, sl_price,
                                                   bool(candle_close <= sl_price))
    else:
        if candle_open < trigger_price:
            if candle_high < trigger_price:
                return None, False
            return trigger_price, bool(candle_high >= sl_price)
        if candle_low > trigger_price:
            return None, False
        return trigger_price, _return_fill_stopped(side, fine_bars, trigger_price, sl_price,
                                                   bool(candle_close >= sl_price))


def _return_fill_stopped(side, fine_bars, trigger_price, sl_price, coarse_stopped):
    """Ruecklauf-Fall per Fein-Kerzen: erste Fein-Kerze, die den Trigger erreicht = Fill.
    SL-Kontakte davor zaehlen nicht (live existiert der SL erst ab Fill, siehe
    angehaengter Bitget-SL). In der Fill-Kerze selbst gilt die Close-Regel (Reihenfolge
    dort unbekannt), danach jeder SL-Kontakt. Ohne brauchbare Fein-Daten: coarse_stopped."""
    if callable(fine_bars):
        try:
            fine_bars = fine_bars()
        except Exception:
            fine_bars = None
    if fine_bars is None or len(fine_bars) == 0:
        return coarse_stopped
    filled = False
    for o, h, l, c in fine_bars[['open', 'high', 'low', 'close']].itertuples(index=False):
        if not filled:
            if (h < trigger_price) if side == 'long' else (l > trigger_price):
                continue
            filled = True
            if (c <= sl_price) if side == 'long' else (c >= sl_price):
                return True
            continue
        if (l <= sl_price) if side == 'long' else (h >= sl_price):
            return True
    return False if filled else coarse_stopped


def detect_market_regime(df, avg_period=14, silent=False, strategy_params=None):
    """
    Erkennt das aktuelle Marktregime (TREND vs RANGE) mit Supertrend-Filter.

    Args:
        df: DataFrame mit OHLC-Daten
        avg_period: Periode für Durchschnittsberechnung
        silent: Wenn True, keine Log-Ausgaben (für Backtest)
        strategy_params: optionales dict mit Overrides für das ADX-Regime-Gate
            (disable_strong_trend_block, strong_trend_adx_threshold) -- muss
            IDENTISCH zu den Overrides im Backtester (run_envelope_backtest)
            interpretiert werden, damit Live und Backtest konsistent bleiben.

    Returns:
        tuple: (regime_name: str, trade_allowed: bool, trend_direction: str, supertrend_direction: str)
    """
    strategy_params = strategy_params or {}
    disable_strong_trend_block = strategy_params.get('disable_strong_trend_block', False)
    strong_trend_adx_threshold = strategy_params.get('strong_trend_adx_threshold', 30.0)
    try:
        # ADX für Trendstärke berechnen
        adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
        current_adx = adx.iloc[-1] if not adx.empty else 20

        # Preis-Position zum gleitenden Durchschnitt
        if 'average' in df.columns and not df['average'].empty:
            current_price = df['close'].iloc[-1]
            ma = df['average'].iloc[-1]
            price_distance_pct = abs(current_price - ma) / ma * 100 if ma > 0 else 0
        else:
            price_distance_pct = 0

        # Supertrend-Indikator (übergeordneter Trendfilter)
        # Periode 10, Multiplier 3 für mittelfristigen Trend
        supertrend_indicator = ta.trend.STCIndicator(
            close=df['close'],
            window_slow=50,
            window_fast=23,
            cycle=10,
            smooth1=3,
            smooth2=3
        )
        
        # Alternativ: Einfacher Supertrend basierend auf ATR
        try:
            atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=10)
            hl2 = (df['high'] + df['low']) / 2
            multiplier = 3.0
            
            upperband = hl2 + (multiplier * atr)
            lowerband = hl2 - (multiplier * atr)
            
            # Supertrend Richtung
            supertrend_direction = "NEUTRAL"
            if not upperband.empty and not lowerband.empty:
                if current_price > upperband.iloc[-1]:
                    supertrend_direction = "BULLISH"
                elif current_price < lowerband.iloc[-1]:
                    supertrend_direction = "BEARISH"
                else:
                    supertrend_direction = "NEUTRAL"
        except Exception as e:
            logger.debug(f"Supertrend-Berechnung fehlgeschlagen: {e}")
            supertrend_direction = "NEUTRAL"

        # Trend-Richtung + Regime-Entscheidung: geteilte Funktion mit Backtest/Portfolio-Sim
        sma_fast = ta.trend.sma_indicator(df['close'], window=20)
        sma_slow = ta.trend.sma_indicator(df['close'], window=50)
        fast_val = sma_fast.iloc[-1] if not sma_fast.empty else float('nan')
        slow_val = sma_slow.iloc[-1] if not sma_slow.empty else float('nan')
        close_val = df['close'].iloc[-1]
        avg_val = df['average'].iloc[-1] if 'average' in df.columns and not df['average'].empty else float('nan')

        regime, trade_allowed, trend_direction = classify_regime(
            current_adx, close_val, avg_val, fast_val, slow_val, strategy_params)

        if not silent:
            if regime == "STRONG_TREND":
                logger.warning(f"STRONG_TREND: ADX={current_adx:.2f} > {strong_trend_adx_threshold:.1f}. Supertrend={supertrend_direction}. Trading gesperrt.")
            elif regime == "TREND" and current_adx > strong_trend_adx_threshold:
                logger.info(f"TREND (Gate deaktiviert): ADX={current_adx:.2f} > {strong_trend_adx_threshold:.1f}. "
                            f"Supertrend={supertrend_direction}. Trading in Trendrichtung erlaubt (Block deaktiviert).")
            elif regime == "TREND":
                logger.info(f"TREND: ADX={current_adx:.2f} > 25.0. Supertrend={supertrend_direction}. Trading nur in Trendrichtung erlaubt.")
            elif regime == "RANGE":
                logger.info(f"RANGE: ADX={current_adx:.2f} < 20.0, price_distance_pct={price_distance_pct:.2f} < 3.0. Supertrend={supertrend_direction}. Mean-Reversion erlaubt.")
            else:
                logger.info(f"UNCERTAIN: ADX={current_adx:.2f}, price_distance_pct={price_distance_pct:.2f}. Supertrend={supertrend_direction}. Vorsichtiges Trading erlaubt.")
        return regime, trade_allowed, trend_direction, supertrend_direction

    except Exception as e:
        if not silent:
            logger.warning(f"Fehler bei Marktregime-Erkennung: {e}. Defaulte auf UNCERTAIN.")
        return "UNCERTAIN", True, "NEUTRAL", "NEUTRAL"

def calculate_indicators_and_signals(df, params):
    """
    Berechnet die Envelope-Indikatoren und identifiziert potenzielle Ein- und Ausstiegspunkte.

    Returns:
        pd.DataFrame: DataFrame mit Indikatoren und potenziellen Signalen.
        dict: Dictionary mit den berechneten Preisen für die letzte Kerze.
    """
    strategy_params = params['strategy']
    avg_type = strategy_params['average_type']
    avg_period = strategy_params['average_period']
    envelopes = strategy_params['envelopes']

    df_copy = df.copy()

    # --- Berechne den zentralen Durchschnitt (Long) und ggf. eigene Short-Mitte ---
    df_copy['average'] = _compute_average(df_copy, avg_type, avg_period)
    sp = short_params(params)
    if sp:
        df_copy['average_short'] = _compute_average(df_copy, sp.get('average_type', avg_type),
                                                    sp.get('average_period', avg_period))
        short_envelopes = sp.get('envelopes', envelopes)
    else:
        df_copy['average_short'] = df_copy['average']
        short_envelopes = envelopes

    # --- Berechne ATR für SL-Berechnung ---
    atr_period = params.get('risk', {}).get('stop_loss_atr_period', 14)
    df_copy['atr'] = ta.volatility.average_true_range(
        df_copy['high'], df_copy['low'], df_copy['close'], window=atr_period
    )

    # --- Berechne die Envelopes ---
    band_prices = {'average': None, 'long': [], 'short': []}
    for i, e_pct in enumerate(envelopes):
        low_col = f'band_low_{i + 1}'
        df_copy[low_col] = df_copy['average'] * (1 - e_pct)
        if not df_copy.empty:
            band_prices['long'].append(df_copy[low_col].iloc[-1])
    for i, e_pct in enumerate(short_envelopes):
        high_col = f'band_high_{i + 1}'
        df_copy[high_col] = df_copy['average_short'] / (1 - e_pct)
        if not df_copy.empty:
            band_prices['short'].append(df_copy[high_col].iloc[-1])

    if not df_copy.empty:
        band_prices['average'] = df_copy['average'].iloc[-1]
        band_prices['average_short'] = df_copy['average_short'].iloc[-1]
        band_prices['atr'] = float(df_copy['atr'].iloc[-1]) if pd.notna(df_copy['atr'].iloc[-1]) else None

    # Optional: Hier könnte man noch explizite Signal-Spalten hinzufügen,
    # aber für die Live-Logik reichen die berechneten Bandpreise.
    # df_copy['long_signal_1'] = df_copy['low'] <= df_copy['band_low_1']
    # df_copy['short_signal_1'] = df_copy['high'] >= df_copy['band_high_1']
    # ... etc.

    df_copy.dropna(inplace=True)
    
    # Marktregime erkennen (Regime-Gate-Overrides aus strategy_params, falls gesetzt --
    # muss konsistent mit den Overrides im Backtester (run_envelope_backtest) sein)
    regime, trade_allowed, trend_direction, supertrend_direction = detect_market_regime(
        df_copy, avg_period, strategy_params=strategy_params)

    # ADX und price_distance_pct für Logging extrahieren
    try:
        adx = ta.trend.adx(df_copy['high'], df_copy['low'], df_copy['close'], window=14)
        band_prices['adx'] = float(adx.iloc[-1]) if not adx.empty else None
    except Exception:
        band_prices['adx'] = None
    try:
        if 'average' in df_copy.columns and not df_copy['average'].empty:
            current_price = df_copy['close'].iloc[-1]
            ma = df_copy['average'].iloc[-1]
            band_prices['price_distance_pct'] = float(abs(current_price - ma) / ma * 100) if ma > 0 else None
        else:
            band_prices['price_distance_pct'] = None
    except Exception:
        band_prices['price_distance_pct'] = None

    # Erweitere band_prices um Regime-Info
    band_prices['regime'] = regime
    band_prices['trade_allowed'] = trade_allowed
    band_prices['trend_direction'] = trend_direction
    band_prices['supertrend_direction'] = supertrend_direction

    return df_copy, band_prices
