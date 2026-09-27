# src/ltbbot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
from datetime import timedelta
import json
import sys
import logging
import ta as _ta

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange # Für load_data
from ltbbot.strategy.envelope_logic import (calculate_indicators_and_signals, calculate_position_margin, margin_fits,
                                            classify_regime, compute_band_sl_price, simulate_entry_fill)

secrets_cache = None

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION ---
SLIPPAGE_PCT_EXIT  = 0.0005  # 0.05% Slippage auf Exit (Market Order TP/SL)
SLIPPAGE_PCT_ENTRY = 0.0012  # 0.12% Slippage auf Entry (Trigger-Limit, beobachtet live: +0.10–0.20%)
# --- ENDE KONSTANTEN ---

# Feinere Timeframe je Strategie-Timeframe fuer die SL/TP-Intrabar-Reihenfolgen-
# Aufloesung (oraclebot-Muster).
FINE_TF_MAP = {
    '5m': '1m', '15m': '1m', '30m': '1m',
    '1h': '5m', '2h': '5m',
    '4h': '15m', '6h': '15m',
    '1d': '1h',
}


def _resolve_ambiguous_exit(fine_slice, sl_price, tp_price, side):
    """
    Wenn eine Coarse-Kerze SOWOHL SL als auch den (fuer diese Kerze aktuellen)
    TP-MA-Wert beruehrt haette, per feineren Kerzen die tatsaechliche
    Reihenfolge aufloesen, statt SL blind zu bevorzugen (bisherige Konvention).
    """
    if fine_slice is None or fine_slice.empty:
        return None
    for _, bar in fine_slice.iterrows():
        if side == 'long':
            if bar['low'] <= sl_price:
                return sl_price
            if bar['high'] >= tp_price:
                return tp_price
        else:
            if bar['high'] >= sl_price:
                return sl_price
            if bar['low'] <= tp_price:
                return tp_price
    return None


class LazyFineData:
    def __init__(self, symbol, fine_tf):
        self.symbol = symbol
        self.fine_tf = fine_tf
        self._days = {}
        self._exchange = None

    def _get_exchange(self):
        global secrets_cache
        if self._exchange is not None:
            return self._exchange
        try:
            if secrets_cache is None:
                with open(os.path.join(PROJECT_ROOT, 'secret.json'), "r") as f:
                    secrets_cache = json.load(f)
            api_setup = None
            if 'ltbbot' in secrets_cache and isinstance(secrets_cache['ltbbot'], list) and secrets_cache['ltbbot']:
                api_setup = secrets_cache['ltbbot'][0]
            if api_setup:
                self._exchange = Exchange(api_setup)
        except Exception:
            self._exchange = None
        return self._exchange

    def _ensure_day(self, day):
        if day in self._days:
            return
        exchange = self._get_exchange()
        if exchange is None or not exchange.markets:
            self._days[day] = None
            return
        try:
            day_str = day.strftime('%Y-%m-%d')
            next_day_str = (day + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            df = exchange.fetch_historical_ohlcv(self.symbol, self.fine_tf, day_str, next_day_str)
            self._days[day] = df if df is not None and not df.empty else None
        except Exception:
            self._days[day] = None

    def get_slice(self, start_ts, end_ts):
        if self.fine_tf is None:
            return None
        start_ts = pd.Timestamp(start_ts)
        end_ts = pd.Timestamp(end_ts)
        first_day = start_ts.floor('D')
        last_day = (end_ts - pd.Timedelta(microseconds=1)).floor('D')
        parts = []
        day = first_day
        while day <= last_day:
            self._ensure_day(day)
            if self._days[day] is not None:
                parts.append(self._days[day])
            day += pd.Timedelta(days=1)
        if not parts:
            return None
        combined = pd.concat(parts).sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]
        return combined.loc[(combined.index >= start_ts) & (combined.index < end_ts)]


def _get_fine_slice(fine_data, start_ts, end_ts):
    if fine_data is None:
        return None
    if hasattr(fine_data, 'get_slice'):
        return fine_data.get_slice(start_ts, end_ts)
    return fine_data.loc[(fine_data.index >= start_ts) & (fine_data.index < end_ts)]


def load_data(symbol, timeframe, start_date_str, end_date_str):
    """Lädt historische OHLCV-Daten, entweder aus dem Cache oder von der Börse."""
    cache_dir = os.path.join(PROJECT_ROOT, 'data', 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    symbol_filename = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(cache_dir, f"{symbol_filename}_{timeframe}.csv")

    data = pd.DataFrame() # Initialisiere leeres DataFrame

    # --- Versuch 1: Aus Cache laden ---
    if os.path.exists(cache_file):
        try:
            data = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            if data.index.tz is None:
                data.index = data.index.tz_localize('UTC')
            else:
                data.index = data.index.tz_convert('UTC')

            cache_start = data.index.min()
            cache_end = data.index.max()
            req_start = pd.to_datetime(start_date_str, utc=True)
            req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True)

            if cache_start <= req_start and cache_end >= req_end:
                return data.loc[req_start:req_end].copy()
            else:
                logger.info(f"Cache für {symbol} ({timeframe}) deckt Zeitraum NICHT ab. Download notwendig.")
                data = pd.DataFrame()
        except Exception as e:
            logger.error(f"Fehler beim Lesen oder Verarbeiten der Cache-Datei {cache_file}: {e}")
            data = pd.DataFrame()

    # --- Versuch 2: Von Börse herunterladen ---
    if data.empty:
        logger.info(f"Starte Download für {symbol} ({timeframe}) von der Börse [{start_date_str} bis {end_date_str}]...")
        try:
            secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
            with open(secret_path, "r") as f:
                secrets = json.load(f)
            # Prüfe, ob 'ltbbot' Key existiert und eine Liste ist
            if 'ltbbot' not in secrets or not isinstance(secrets['ltbbot'], list) or not secrets['ltbbot']:
                 raise ValueError("Kein gültiger 'ltbbot'-Eintrag in secret.json gefunden.")
            api_setup = secrets['ltbbot'][0]
            exchange = Exchange(api_setup)

            full_data = exchange.fetch_historical_ohlcv(symbol, timeframe, start_date_str, end_date_str)

            if full_data is not None and not full_data.empty:
                logger.info(f"Download erfolgreich. Speichere {len(full_data)} Kerzen im Cache: {cache_file}")
                if full_data.index.tz is None:
                    full_data.index = full_data.index.tz_localize('UTC')
                else:
                    full_data.index = full_data.index.tz_convert('UTC')
                full_data.to_csv(cache_file)
                req_start = pd.to_datetime(start_date_str, utc=True)
                req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True)
                # Sicherstellen, dass nur der angeforderte Bereich zurückgegeben wird
                return full_data.loc[req_start:req_end].copy()
            else:
                logger.error(f"Download für {symbol} ({timeframe}) fehlgeschlagen oder keine Daten erhalten.")
                return pd.DataFrame()
        except FileNotFoundError:
            logger.error(f"secret.json nicht gefunden unter {secret_path}. Download nicht möglich.")
            return pd.DataFrame()
        except (IndexError, KeyError, ValueError) as e: # Fängt Fehler bei ungültigem secret.json Format ab
            logger.error(f"Fehlerhafter oder fehlender Account-Eintrag ('ltbbot') in secret.json: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Fehler beim Daten-Download für {symbol} ({timeframe}): {e}", exc_info=True)
            return pd.DataFrame()

    logger.error(f"Konnte Daten für {symbol} ({timeframe}) weder aus Cache laden noch herunterladen.")
    return pd.DataFrame()

# --- NEUER BACKTESTER FÜR ENVELOPE (MIT KORREKTUREN) ---
def run_envelope_backtest(data, params, start_capital=1000, show_progress=True, sim_start_date=None, fine_data=None, multi_band_entries=True):
    """
    Führt einen Backtest für die Envelope-Strategie durch.
    KORRIGIERT: Verwendet Startkapital für Positionsgrößen, simuliert Slippage und Max Position Size.

    multi_band_entries (Default True seit 2026-08-27):
    Der Live-Bot (trade_manager.py) platziert pro Kerze IMMER Orders fuer ALLE
    konfigurierten Envelope-Baender gleichzeitig (z.B. 3 offene Limit-Orders pro
    Seite). War urspruenglich (2026-08-26) als Opt-in mit Default False eingefuehrt
    worden, um bestehende Aufrufer nicht zu veraendern -- das fuehrte aber
    wiederholt dazu, dass neue/uebersehene Aufrufstellen (u.a. show_results.py
    Einzel-Analyse-Modus, erst am 2026-08-27 durch einen Live-Test auf dem VPS
    aufgefallen) weiterhin die alte, NICHT live-konsistente Logik nutzten, ohne
    dass es auffiel. Mit multi_band_entries=False (jetzt explizit anzufordern)
    oeffnet der Backtester pro Kerze/Seite weiterhin hoechstens EINE Position --
    das flachste passende Band gewinnt, tiefere Baender werden nie geprueft
    (empirisch verifiziert 2026-08-26: 0 von 1363 Trades ueber 4 Configs nutzten
    Band 2/3) -- nur noch fuer explizite historische Vergleiche mit dem
    urspruenglichen (Band-1-only) 12-Konfigurationen-Sweep gedacht.
    """
    if data.empty:
        logger.warning("Leeres DataFrame an Backtester übergeben.")
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Parameter extrahieren ---
    # Fange potenzielle KeyError ab, falls Config unvollständig ist
    try:
        strategy_params = params['strategy']
        risk_params = params['risk']
        behavior_params = params['behavior']

        leverage = risk_params['leverage']
        risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer
        num_envelopes = len(strategy_params['envelopes'])
        use_longs = behavior_params.get('use_longs', True)
        use_shorts = behavior_params.get('use_shorts', True)
        trigger_delta_pct = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0
        # SL-Modus (Priorität: sl_ratio → ATR-mult → fixer %)
        if 'sl_to_env1_ratio' in risk_params:
            _sl_mode = 'ratio'
            _sl_ratio = risk_params['sl_to_env1_ratio']
            _envelopes = strategy_params['envelopes']
            stop_loss_pct_param = None; _atr_sl_mult = None; _atr_sl_period = 14; _min_sl_pct = 0.0
        elif 'stop_loss_atr_multiplier' in risk_params:
            _sl_mode = 'atr'
            _atr_sl_mult   = risk_params['stop_loss_atr_multiplier']
            _atr_sl_period = risk_params.get('stop_loss_atr_period', 14)
            _min_sl_pct    = risk_params.get('min_stop_loss_pct', 0.5) / 100.0
            stop_loss_pct_param = None; _sl_ratio = None; _envelopes = []
        else:
            _sl_mode = 'fixed'
            stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0
            _sl_ratio = None; _envelopes = []; _atr_sl_mult = None; _atr_sl_period = 14; _min_sl_pct = 0.0
        use_atr_sl = (_sl_mode == 'atr')
    except KeyError as e:
         logger.error(f"Fehlender Schlüssel in Parameter-Dict: {e}. Backtest abgebrochen.")
         return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}


    fee_pct = 0.0006 # Beispiel: 0.06% Maker/Taker Fee

    # --- Indikatoren berechnen ---
    try:
        df, band_prices = calculate_indicators_and_signals(data.copy(), params)
        if df.empty:
            raise ValueError("Indikatorberechnung ergab leeres DataFrame.")
        
        # Extrahiere Regime-Informationen für spätere Verwendung
        # Die calculate_indicators_and_signals Funktion gibt band_prices mit regime/trend_direction zurück
        # Aber da wir Kerze für Kerze durchgehen, müssen wir das Regime für jede Kerze neu bestimmen
        # Das Regime ist bereits im df als Spalten vorhanden (falls detect_market_regime es setzt)
        # Alternativ: Wir berechnen es pro Kerze neu im Loop
        
    except Exception as e:
        logger.warning(f"Fehler bei Indikatorberechnung im Backtest: {e}")
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital # Aktuelles REALISIERTES Kapital
    # peak_capital = start_capital # Höchststand inkl. unreal. PnL (wird jetzt aus equity_curve berechnet)
    # max_drawdown_pct = 0.0 # (wird jetzt aus equity_curve berechnet)

    positions = [] # [{entry_price, amount_coins, side, sl_price, tp_price, leverage, margin}, ...]
    closed_trades = [] # [{pnl, side}, ...]
    equity_curve_data = [] # Für Drawdown-Berechnung am Ende

    # Margin ist eine gemeinsame, endliche Ressource (wie beim echten Exchange-Konto):
    # jede offene Position bindet capital/leverage USDT Margin. used_margin trackt die
    # Summe ueber alle aktuell offenen Positionen; vor jeder neuen Order wird geprueft,
    # ob genug FREIE Margin (capital - used_margin) uebrig ist -- sonst wuerde die
    # Order live mit InsufficientFunds abgelehnt (User-Vorgabe 2026-09-02: ein Trade,
    # der schon (fast) das ganze Kapital bindet, darf einen zweiten NICHT zulassen).
    used_margin = 0.0
    
    # Starte Equity Curve mit Start Capital
    if not df.empty:
        first_timestamp = df.index[0]
        equity_curve_data.append({'timestamp': first_timestamp, 'equity': start_capital})
    
    # ATR-Serie für SL-Berechnung (nur wenn ATR-SL aktiv)
    _atr_sl_pre = _ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=_atr_sl_period) if use_atr_sl else None

    # Regime-Indikatoren einmalig vorab berechnen (O(n) statt O(n²))
    _adx_pre   = _ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    _sma20_pre = _ta.trend.sma_indicator(df['close'], window=20)
    _sma50_pre = _ta.trend.sma_indicator(df['close'], window=50)

    # sim_start_date: Warmup-Kerzen werden für Indikatoren genutzt, Trades erst ab hier
    sim_start_ts = pd.to_datetime(sim_start_date, utc=True) if sim_start_date else None

    # Progress Bar Setup
    total_candles = len(df)
    if show_progress:
        logger.info(f"Starte Backtest mit {total_candles} Kerzen...")
    progress_interval = max(1, total_candles // 20)  # 20 Updates (5% Schritte)
    coarse_duration = df.index[1] - df.index[0] if len(df.index) >= 2 else None

    def _close_position(pos, exit_price, exit_reason, exit_ts):
        """Realisiert eine Position (Gebuehren + Slippage) und gibt den PnL zurueck."""
        pos_entry = pos['entry_price']; pos_amount = pos['amount_coins']
        if pos['side'] == 'long':
            pnl = (exit_price - pos_entry) * pos_amount
        else:
            pnl = (pos_entry - exit_price) * pos_amount
        # Gebühren abziehen (Notional ohne Leverage-Faktor)
        entry_notional_value = pos_entry * pos_amount
        exit_notional_value = exit_price * pos_amount
        pnl -= (entry_notional_value * fee_pct) + (exit_notional_value * fee_pct)
        # Slippage auf Entry (Trigger-Limit) UND Exit (Market Order)
        pnl -= abs(entry_notional_value * SLIPPAGE_PCT_ENTRY) + abs(exit_notional_value * SLIPPAGE_PCT_EXIT)
        closed_trades.append({
            'pnl': pnl, 'side': pos['side'], 'band': pos.get('band'),
            'entry_time': pos.get('entry_time'), 'exit_time': exit_ts,
            'entry_price': pos_entry, 'exit_price': exit_price,
            'exit_reason': exit_reason,
        })
        return pnl

    # KEIN LOOKAHEAD (2026-09-27): Waehrend Kerze i gelten -- wie live
    # (drop_incomplete_last_candle) -- Baender, TP-MA, Regime und ATR der letzten
    # ABGESCHLOSSENEN Kerze i-1. Vorher kamen sie aus Kerze i selbst (inkl. deren
    # Close); der Optimizer hat das systematisch ausgenutzt (alle Configs am
    # Suchraum-Rand, Backtest-WR 31.8% vs. ehrlich 19.4%, siehe
    # bugfix_ltbbot_backtester_band_lookahead). Kerze i liefert nur noch den
    # Preisverlauf (open/high/low), gegen den Orders ausgefuehrt werden.
    for i in range(1, len(df)):
        # Progress Bar Update
        if show_progress and (i % progress_interval == 0 or i == total_candles - 1):
            progress_pct = (i + 1) / total_candles * 100
            bar_length = 30
            filled = int(bar_length * (i + 1) / total_candles)
            bar = '█' * filled + '░' * (bar_length - filled)
            print(f"\r  Progress: [{bar}] {progress_pct:.1f}% ({i+1}/{total_candles})", end='', flush=True)

        current_candle = df.iloc[i]
        signal_candle = df.iloc[i - 1]  # letzte abgeschlossene Kerze
        timestamp = current_candle.name # Zeitstempel der Kerze
        candle_open = current_candle['open']
        candle_high = current_candle['high']
        candle_low = current_candle['low']

        # Warmup-Kerzen: Indikatoren berechnen, aber keine Trades
        if sim_start_ts and timestamp < sim_start_ts:
            continue


        # --- Ausstiege prüfen (TP und SL) ---
        remaining_positions = []
        exit_pnl_current_candle = 0.0
        # Same-Candle-Sperre wie live (arm_same_candle_reentry_guard): ein in dieser
        # Kerze geschlossenes Band wird erst in der naechsten Kerze neu eroeffnet
        closed_bands_this_candle = set()
        # TP = MA der letzten abgeschlossenen Kerze (live von manage_existing_position()
        # jeden Zyklus neu gesetzt)
        tp_price_current = signal_candle['average']
        tp_valid = pd.notna(tp_price_current) and tp_price_current > 0

        for pos in positions:
            pos_side = pos['side']
            pos_sl = pos['sl_price']
            exit_price = None
            exit_reason = None

            if pos_side == 'long':
                sl_hit = candle_low <= pos_sl
                tp_hit = tp_valid and candle_high >= tp_price_current
                sl_gap = candle_open <= pos_sl
                tp_gap = tp_valid and candle_open >= tp_price_current
            else:
                sl_hit = candle_high >= pos_sl
                tp_hit = tp_valid and candle_low <= tp_price_current
                sl_gap = candle_open >= pos_sl
                tp_gap = tp_valid and candle_open <= tp_price_current

            if sl_gap:
                # Preis oeffnet schon jenseits des SL -> Stop-Market fuellt zum Open
                exit_price, exit_reason = candle_open, 'SL'
            elif tp_gap:
                exit_price, exit_reason = candle_open, 'TP'
            elif sl_hit and tp_hit:
                # Beide Level in derselben Kerze moeglich -- Reihenfolge unklar
                # ohne feinere Daten. Per fine_data (falls vorhanden) real
                # aufloesen statt SL blind zu bevorzugen (oraclebot-Muster).
                resolved = None
                if fine_data is not None and coarse_duration is not None:
                    fine_slice = _get_fine_slice(fine_data, timestamp, timestamp + coarse_duration)
                    resolved = _resolve_ambiguous_exit(fine_slice, pos_sl, tp_price_current, pos_side)
                if resolved is None or resolved == pos_sl:
                    exit_price, exit_reason = pos_sl, 'SL'  # Fallback: SL-first-Konvention
                else:
                    exit_price, exit_reason = tp_price_current, 'TP'
            elif sl_hit:
                exit_price, exit_reason = pos_sl, 'SL'
            elif tp_hit:
                exit_price, exit_reason = tp_price_current, 'TP'

            if exit_price is not None and exit_price > 0:
                exit_pnl_current_candle += _close_position(pos, exit_price, exit_reason, timestamp)
                used_margin -= pos.get('margin', 0.0) # Margin wieder freigeben
                closed_bands_this_candle.add((pos_side, pos.get('band')))
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        used_margin = max(0.0, used_margin) # Rundungsdrift abfangen
        capital += exit_pnl_current_candle # Realisiertes Kapital nach Ausstiegen aktualisieren

        # --- Einstiege prüfen ---
        # Wie Live Bot: bei offener Position nur weitere, noch nicht offene Baender
        # auf DERSELBEN Seite (Bitget One-Way-Modus, restrict_side in place_entry_orders)
        open_side = positions[0]['side'] if positions else None
        open_bands = {(p['side'], p.get('band')) for p in positions}
        if capital > 0:

            # Marktregime der letzten abgeschlossenen Kerze (geteilte Funktion mit Live)
            if i - 1 >= 49:
                regime, trade_allowed, trend_direction = classify_regime(
                    _adx_pre.iloc[i - 1], signal_candle['close'], signal_candle['average'],
                    _sma20_pre.iloc[i - 1], _sma50_pre.iloc[i - 1], strategy_params)
            else:
                regime, trade_allowed, trend_direction = "UNCERTAIN", True, "NEUTRAL"

            # STRONG_TREND: Kein Einstieg (wie Live Bot)
            if trade_allowed:
                # Trend-Bias: Im Uptrend nur Longs, im Downtrend nur Shorts (wie Live Bot)
                current_use_longs = use_longs and trend_direction != "DOWNTREND"
                current_use_shorts = use_shorts and trend_direction != "UPTREND"

                # Risiko basiert auf dem AKTUELLEN realisierten Kapital (Compounding,
                # konsistent mit Live Bot -- User-Entscheidung 2026-08-26: "keine
                # kuenstliche Bremse", Positionsgroesse soll mit dem Gesamtkapital
                # mitwachsen/-schrumpfen statt an einem fixen Startwert zu kleben).
                # Risikobasis = freies Kapital (live: fetch_balance_usdt() = Bitgets 'free')
                risk_amount_usd = max(0.0, capital - used_margin) * (risk_per_entry_pct / 100.0)
                atr_value = _atr_sl_pre.iloc[i - 1] if use_atr_sl else None

                # Wie Live Bot: Long- UND Short-Trigger unabhängig prüfen.
                # Wenn beide in derselben Kerze getroffen werden, gewinnt der dessen
                # Trigger-Preis näher am Kerzeneröffnungspreis liegt (= zuerst ausgelöst).
                MIN_NOTIONAL_USDT = 5.0
                candidates = {'long': [], 'short': []}
                for side, allowed in (('long', current_use_longs), ('short', current_use_shorts)):
                    if not allowed or risk_amount_usd <= 0:
                        continue
                    if open_side is not None and side != open_side:
                        continue
                    for k in range(1, num_envelopes + 1):
                        if (side, k) in open_bands or (side, k) in closed_bands_this_candle:
                            continue
                        band_col = f'band_low_{k}' if side == 'long' else f'band_high_{k}'
                        band_price = signal_candle.get(band_col, float('nan'))
                        if pd.isna(band_price) or band_price <= 0 or pd.isna(signal_candle['close']):
                            continue
                        # Close-Confirmation: letzte abgeschlossene Kerze muss jenseits des Bands geschlossen haben
                        if side == 'long' and signal_candle['close'] > band_price:
                            continue
                        if side == 'short' and signal_candle['close'] < band_price:
                            continue
                        sl_price = compute_band_sl_price(side, band_price, k - 1, params, regime, atr_value)
                        if sl_price is None:
                            continue
                        sl_dist = abs(band_price - sl_price)
                        if sl_dist <= 0:
                            continue
                        trigger_price = band_price * (1 - trigger_delta_pct) if side == 'long' else band_price * (1 + trigger_delta_pct)
                        fill_price, stopped = simulate_entry_fill(side, candle_open, candle_high, candle_low,
                                                                  current_candle['close'], trigger_price, sl_price)
                        if fill_price is None:
                            continue
                        # Groesse wie live: aus Band-Preis und SL-Abstand
                        amount_coins = risk_amount_usd / sl_dist
                        if amount_coins * band_price < MIN_NOTIONAL_USDT:
                            continue
                        candidates[side].append((fill_price, sl_price, amount_coins,
                                                 abs(candle_open - trigger_price), k, stopped))
                        if not multi_band_entries:
                            break

                # Wenn beide Seiten gleichzeitig getriggert: naeherer Trigger zum Open gewinnt
                if candidates['long'] and candidates['short']:
                    nearest_long = min(c[3] for c in candidates['long'])
                    nearest_short = min(c[3] for c in candidates['short'])
                    if nearest_long <= nearest_short:
                        candidates['short'] = []
                    else:
                        candidates['long'] = []

                # Reihenfolge Band 1->3 = engste (naeheste) Baender zuerst, wie sie live
                # auch zuerst ausloesen wuerden. Jede Order muss sich gegen die noch FREIE
                # Margin behaupten -- reicht sie nicht mehr, wird die Order uebersprungen
                # (= live InsufficientFunds), NICHT auf Kredit geoeffnet.
                entry_pnl_current_candle = 0.0
                for side in ('long', 'short'):
                    for entry_price, sl_price, amount_coins, _, band_k, stopped in candidates[side]:
                        margin_required = calculate_position_margin(amount_coins, entry_price, leverage)
                        if not margin_fits(used_margin, margin_required, capital):
                            continue
                        pos = {
                            'entry_price': entry_price, 'amount_coins': amount_coins,
                            'side': side, 'sl_price': sl_price, 'leverage': leverage,
                            'band': band_k, 'entry_time': timestamp, 'margin': margin_required
                        }
                        if stopped:
                            # SL noch in der Entry-Kerze erreicht (konservativ, siehe simulate_entry_fill)
                            entry_pnl_current_candle += _close_position(pos, sl_price, 'SL', timestamp)
                            continue
                        used_margin += margin_required
                        positions.append(pos)
                capital += entry_pnl_current_candle
                exit_pnl_current_candle += entry_pnl_current_candle

        # --- Equity Curve aktualisieren (nur bei Trade-Exit) ---
        if exit_pnl_current_candle != 0.0:
            equity_curve_data.append({'timestamp': timestamp, 'equity': capital})

        # --- Abbruch bei Totalverlust (basierend auf Equity Curve Start) ---
    
    # Progress Bar abschließen
    if show_progress:
        print()  # Newline nach Progress Bar
        logger.info("Backtest abgeschlossen. Berechne Metriken...")
    
    # --- Endauswertung ---
    final_equity = capital # KORREKTUR: Verwende die Variable 'capital'
    final_unrealized_pnl = 0.0
    if not df.empty and positions: # Nur wenn Positionen am Ende noch offen sind
        last_close_price = df['close'].iloc[-1]
        # Sicherstellen, dass der letzte Preis gültig ist
        if pd.isna(last_close_price) or last_close_price <= 0:
             logger.warning("Letzter Schlusspreis ungültig. Unrealisierter PnL am Ende könnte 0 sein.")
             # Fallback: Versuche letzten gültigen Close zu finden
             valid_closes = df['close'].dropna()
             last_close_price = valid_closes.iloc[-1] if not valid_closes.empty else 0

        if last_close_price > 0:
            for pos in positions:
                pos_amount_final = pos['amount_coins']
                pos_entry_final = pos['entry_price']
                if pos['side'] == 'long':
                    final_unrealized_pnl += (last_close_price - pos_entry_final) * pos_amount_final
                else: # short
                    final_unrealized_pnl += (pos_entry_final - last_close_price) * pos_amount_final

    final_total_equity = max(0, final_equity + final_unrealized_pnl) # Endgültiges Gesamtkapital

    # Füge den letzten Equity-Punkt hinzu (basierend auf Schlusskurs), falls Daten vorhanden
    if not df.empty:
         last_timestamp = df.index[-1]
         # Nur hinzufügen, wenn der Zeitstempel noch nicht existiert (verhindert Duplikate bei Abbruch)
         if not equity_curve_data or equity_curve_data[-1]['timestamp'] != last_timestamp:
              equity_curve_data.append({'timestamp': last_timestamp, 'equity': final_total_equity})

    total_pnl = final_total_equity - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital != 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    # Drawdown aus Equity Curve berechnen
    equity_df = pd.DataFrame(equity_curve_data)
    calculated_max_dd_pct = 0.0
    if not equity_df.empty:
        # Nur wenn Timestamp nicht bereits Index ist und Spalte existiert
        if 'timestamp' in equity_df.columns:
            # Duplikate im Timestamp entfernen, bevor Index gesetzt wird
            equity_df = equity_df.drop_duplicates(subset=['timestamp'], keep='last')
            equity_df.set_index('timestamp', inplace=True)
        # Überprüfe, ob 'equity' Spalte existiert
        if 'equity' in equity_df.columns:
            # Stelle sicher, dass Equity numerisch ist und fülle NaNs evtl. mit ffill
            equity_df['equity'] = pd.to_numeric(equity_df['equity'], errors='coerce')
            equity_df['equity'] = equity_df['equity'].ffill().fillna(start_capital) # Vorwärts füllen, dann mit Startkapital

            equity_df['peak'] = equity_df['equity'].cummax()
            # Vermeide Division durch Null oder NaNs im Peak
            peak_for_calc = equity_df['peak'].replace(0, np.nan)
            equity_df['drawdown_pct'] = ((peak_for_calc - equity_df['equity']) / peak_for_calc).fillna(0) * 100
            # Stelle sicher, dass DD nicht negativ wird (kann durch ffill passieren)
            equity_df['drawdown_pct'] = equity_df['drawdown_pct'].clip(lower=0)

            calculated_max_dd_pct = equity_df['drawdown_pct'].max() if not equity_df['drawdown_pct'].empty else 0.0
        else:
             logger.warning("Spalte 'equity' nicht im Equity DataFrame gefunden für Drawdown-Berechnung.")
    else:
        logger.warning("Equity Curve DataFrame ist leer. Drawdown kann nicht berechnet werden.")


    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(calculated_max_dd_pct, 2), # Verwende berechneten DD
        "end_capital": round(final_total_equity, 2), # Verwende finales Gesamtkapital
        "start_capital": start_capital,
        "equity_curve": equity_curve_data,  # Füge Equity Curve hinzu für Chart-Darstellung
        "trades": closed_trades  # Vollstaendige Trade-Liste (Band/Zeiten/Preise) fuer Introspektion
    }
    return results
