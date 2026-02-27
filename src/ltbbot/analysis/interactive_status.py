#!/usr/bin/env python3
"""
Interactive Charts für LTBBot
Zeigt Candlestick-Chart mit:
- Trade-Signalen (Entry/Exit Long/Short mit großen farbigen Symbolen)
- Envelope-Bändern (Upper/Lower Bands)
- Backtest-Metriken (Startkapital, Endkapital, PnL%, Max DD%, Trades, Win Rate%, Status)
Nutzt durchnummerierte Konfigurationsdateien zum Auswählen
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange
from ltbbot.analysis.backtester import run_envelope_backtest, load_data, calculate_indicators_and_signals

def setup_logging():
    logger = logging.getLogger('interactive_status')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(ch)
    return logger

logger = setup_logging()

def get_config_files():
    """Sucht alle Konfigurationsdateien auf"""
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    if not os.path.exists(configs_dir):
        return []
    
    configs = []
    for filename in sorted(os.listdir(configs_dir)):
        if filename.startswith('config_') and filename.endswith('.json'):
            filepath = os.path.join(configs_dir, filename)
            configs.append((filename, filepath))
    
    return configs

def select_configs():
    """Zeigt durchnummerierte Konfigurationsdateien und lässt User wählen"""
    configs = get_config_files()
    
    if not configs:
        logger.error("Keine Konfigurationsdateien gefunden!")
        sys.exit(1)
    
    print("\n" + "="*60)
    print("Verfügbare Konfigurationen:")
    print("="*60)
    for idx, (filename, _) in enumerate(configs, 1):
        clean_name = filename.replace('config_', '').replace('.json', '')
        print(f"{idx:2d}) {clean_name}")
    print("="*60)
    
    print("\nWähle Konfiguration(en) zum Anzeigen:")
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    
    selection = input("\nAuswahl: ").strip()
    
    selected_indices = []
    for part in selection.replace(',', ' ').split():
        try:
            idx = int(part)
            if 1 <= idx <= len(configs):
                selected_indices.append(idx - 1)
            else:
                logger.warning(f"Index {idx} außerhalb des Bereichs")
        except ValueError:
            logger.warning(f"Ungültige Eingabe: {part}")
    
    if not selected_indices:
        logger.error("Keine gültigen Konfigurationen gewählt!")
        sys.exit(1)
    
    return [configs[i] for i in selected_indices]

def load_config(filepath):
    """Lädt eine Konfiguration"""
    with open(filepath, 'r') as f:
        return json.load(f)

def create_interactive_chart(symbol, timeframe, df, trades, config, backtest_result=None, start_date=None, end_date=None, window=None):
    """
    Erstellt interaktiven Chart mit:
    - Candlesticks
    - Envelope-Bändern (Upper/Lower)
    - Trade-Signalen (Entry/Exit Long/Short mit großen Symbolen)
    - Backtest-Metriken oberhalb (Start Capital, End Capital, PnL%, Max DD%, Trades, Win Rate%)
    """
    
    # Filter auf Fenster
    if window:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=window)
        df = df[df.index >= cutoff_date].copy()
    
    # Filter auf Start/End Datum
    if start_date:
        df = df[df.index >= pd.to_datetime(start_date, utc=True)]
    if end_date:
        df = df[df.index <= pd.to_datetime(end_date, utc=True)]
    
    fig = go.Figure()
    
    # ===== 1. CANDLESTICK CHART =====
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='OHLC',
            increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626",
            showlegend=True
        )
    )
    
    # ===== 2. ENVELOPE-BÄNDER (falls vorhanden) =====
    if 'upper_band' in df.columns and 'lower_band' in df.columns:
        # Upper Band
        fig.add_trace(go.Scatter(
            x=df.index, y=df['upper_band'],
            mode='lines',
            name='Upper Band',
            line=dict(color='#9333ea', width=2, dash='dot'),
            showlegend=True,
            hovertemplate='Upper Band: %{y:.8f}<extra></extra>'
        ))
        
        # Lower Band
        fig.add_trace(go.Scatter(
            x=df.index, y=df['lower_band'],
            mode='lines',
            name='Lower Band',
            line=dict(color='#ec4899', width=2, dash='dot'),
            showlegend=True,
            hovertemplate='Lower Band: %{y:.8f}<extra></extra>'
        ))
        
        # Gefüllter Bereich zwischen Bändern
        fig.add_trace(go.Scatter(
            x=df.index.tolist() + df.index.tolist()[::-1],
            y=df['upper_band'].tolist() + df['lower_band'].tolist()[::-1],
            fill='toself',
            fillcolor='rgba(147, 51, 234, 0.1)',
            line=dict(color='rgba(255,255,255,0)'),
            showlegend=False,
            name='Envelope Zone',
            hoverinfo='skip'
        ))
    
    # ===== 3. TRADE-SIGNALE =====
    entry_long_x, entry_long_y = [], []
    exit_long_x, exit_long_y = [], []
    entry_short_x, entry_short_y = [], []
    exit_short_x, exit_short_y = [], []
    
    for trade in trades:
        if 'entry_long' in trade:
            entry_time = trade['entry_long'].get('time')
            entry_price = trade['entry_long'].get('price')
            if entry_time and entry_price:
                entry_long_x.append(pd.to_datetime(entry_time))
                entry_long_y.append(entry_price)
        
        if 'exit_long' in trade:
            exit_time = trade['exit_long'].get('time')
            exit_price = trade['exit_long'].get('price')
            if exit_time and exit_price:
                exit_long_x.append(pd.to_datetime(exit_time))
                exit_long_y.append(exit_price)
        
        if 'entry_short' in trade:
            entry_time = trade['entry_short'].get('time')
            entry_price = trade['entry_short'].get('price')
            if entry_time and entry_price:
                entry_short_x.append(pd.to_datetime(entry_time))
                entry_short_y.append(entry_price)
        
        if 'exit_short' in trade:
            exit_time = trade['exit_short'].get('time')
            exit_price = trade['exit_short'].get('price')
            if exit_time and exit_price:
                exit_short_x.append(pd.to_datetime(exit_time))
                exit_short_y.append(exit_price)
    
    # Entry Long: grünes Dreieck nach oben (größer)
    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode="markers",
            marker=dict(
                color="#16a34a", 
                symbol="triangle-up", 
                size=18,
                line=dict(width=2.5, color="#0f5132")
            ),
            name="Entry Long",
            showlegend=True,
            hovertemplate='<b>Entry Long</b><br>Price: %{y:.8f}<br>Time: %{x}<extra></extra>'
        ))
    
    # Exit Long: cyan Kreis (größer)
    if exit_long_x:
        fig.add_trace(go.Scatter(
            x=exit_long_x, y=exit_long_y, mode="markers",
            marker=dict(
                color="#22d3ee", 
                symbol="circle", 
                size=16,
                line=dict(width=2.2, color="#0e7490")
            ),
            name="Exit Long",
            showlegend=True,
            hovertemplate='<b>Exit Long</b><br>Price: %{y:.8f}<br>Time: %{x}<extra></extra>'
        ))
    
    # Entry Short: oranges Dreieck nach unten (größer)
    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode="markers",
            marker=dict(
                color="#f59e0b", 
                symbol="triangle-down", 
                size=18,
                line=dict(width=2.5, color="#92400e")
            ),
            name="Entry Short",
            showlegend=True,
            hovertemplate='<b>Entry Short</b><br>Price: %{y:.8f}<br>Time: %{x}<extra></extra>'
        ))
    
    # Exit Short: rotes Diamant (größer)
    if exit_short_x:
        fig.add_trace(go.Scatter(
            x=exit_short_x, y=exit_short_y, mode="markers",
            marker=dict(
                color="#ef4444", 
                symbol="diamond", 
                size=16,
                line=dict(width=2.2, color="#7f1d1d")
            ),
            name="Exit Short",
            showlegend=True,
            hovertemplate='<b>Exit Short</b><br>Price: %{y:.8f}<br>Time: %{x}<extra></extra>'
        ))
    
    # ===== 4. EQUITY CURVE (sekundäre Y-Achse rechts) =====
    if backtest_result and 'equity_curve' in backtest_result:
        equity_data = backtest_result['equity_curve']
        if equity_data and len(equity_data) > 0:
            equity_df = pd.DataFrame(equity_data)
            if 'timestamp' in equity_df.columns and 'equity' in equity_df.columns:
                fig.add_trace(go.Scatter(
                    x=equity_df['timestamp'],
                    y=equity_df['equity'],
                    mode='lines',
                    name='Kontostand',
                    line=dict(color='#2563eb', width=2),
                    yaxis='y2',
                    showlegend=True,
                    hovertemplate='<b>Kontostand</b><br>%{y:,.2f} USDT<br>%{x}<extra></extra>'
                ))
    
    # ===== 5. TITEL MIT METRIKEN =====
    title = f"{symbol} {timeframe} - LTBBot"
    subtitle = ""
    
    if backtest_result:
        start_capital = backtest_result.get('start_capital', 'N/A')
        end_capital = backtest_result.get('end_capital', 'N/A')
        total_pnl_pct = backtest_result.get('total_pnl_pct', 0)
        max_dd_pct = backtest_result.get('max_drawdown_pct', 0)
        trades_count = backtest_result.get('trades_count', 0)
        win_rate = backtest_result.get('win_rate', 0)
        status = backtest_result.get('status', 'Unknown')
        
        # Formatiere Metriken
        if isinstance(start_capital, (int, float)):
            start_cap_str = f"${start_capital:,.2f}"
        else:
            start_cap_str = str(start_capital)
        
        if isinstance(end_capital, (int, float)):
            end_cap_str = f"${end_capital:,.2f}"
        else:
            end_cap_str = str(end_capital)
        
        pnl_color = '#16a34a' if total_pnl_pct >= 0 else '#dc2626'
        
        subtitle = (
            f"<sub>"
            f"<b>Start Capital:</b> {start_cap_str} | "
            f"<b>End Capital:</b> {end_cap_str} | "
            f"<b style='color:{pnl_color}'>PnL:</b> <b style='color:{pnl_color}'>{total_pnl_pct:+.2f}%</b> | "
            f"<b>Max DD:</b> {max_dd_pct:.2f}% | "
            f"<b>Trades:</b> {trades_count} | "
            f"<b>Win Rate:</b> {win_rate:.1f}% | "
            f"<b>Status:</b> {status}"
            f"</sub>"
        )
    
    title_text = title + subtitle if subtitle else title
    
    fig.update_layout(
        title=dict(
            text=title_text,
            x=0.5,
            xanchor='center',
            font=dict(size=14)
        ),
        height=700,
        hovermode='x unified',
        template='plotly_white',
        dragmode='zoom',
        xaxis=dict(
            rangeslider=dict(visible=False),
            fixedrange=False
        ),
        yaxis=dict(fixedrange=False, title_text="Preis (USDT)"),
        yaxis2=dict(
            title_text="Kontostand (USDT)",
            overlaying="y",
            side="right",
            fixedrange=False
        ),
        legend=dict(
            orientation="h", 
            yanchor="bottom", 
            y=1.12,
            xanchor="right", 
            x=1
        ),
        showlegend=True,
        margin=dict(t=150, b=60)
    )
    
    fig.update_xaxes(fixedrange=False, title_text="Zeit")
    
    return fig

def extract_trades_from_backtest(df, config):
    """
    Extrahiere Trade-Signale basierend auf Envelope-Bändern.
    Entry: wenn Preis touch/cross band (high >= upper or low <= lower)
    Exit: Signalwechsel oder Pullback
    """
    try:
        # Berechne Indikatoren und Signale
        df_with_indicators, band_prices = calculate_indicators_and_signals(df.copy(), config)
        
        # Falls keine Envelopes berechnet wurden, return leere Liste
        if df_with_indicators.empty:
            return [], df_with_indicators
        
        # Extrahiere die Bandnamen
        envelopes = config['strategy']['envelopes']
        trades = []
        
        # Wir nutzen die erste (wichtigste) Envelope für Signale
        high_col = 'band_high_1'
        low_col = 'band_low_1'
        
        if high_col not in df_with_indicators.columns or low_col not in df_with_indicators.columns:
            logger.warning(f"Envelope-Bänder nicht gefunden. Verfügbare Spalten: {list(df_with_indicators.columns)}")
            return [], df_with_indicators
        
        # Für die Anzeige: erstelle Upper/Lower Band Spalten für den Chart
        df_with_indicators['upper_band'] = df_with_indicators[high_col]
        df_with_indicators['lower_band'] = df_with_indicators[low_col]
        
        # Extrahiere Signale basierend auf Band-Crosses
        current_long_entry = None
        current_short_entry = None
        
        for i in range(1, len(df_with_indicators)):
            row = df_with_indicators.iloc[i]
            prev_row = df_with_indicators.iloc[i-1]
            timestamp = row.name
            close_price = row['close']
            high_price = row['high']
            low_price = row['low']
            
            upper_band = row[high_col]
            lower_band = row[low_col]
            prev_upper = prev_row[high_col]
            prev_lower = prev_row[low_col]
            
            # Entry Long: Wenn Preis von oben die untere Band kreuzt (nach oben)
            if not current_long_entry and low_price <= lower_band:
                current_long_entry = {
                    'time': timestamp,
                    'price': close_price
                }
            
            # Entry Short: Wenn Preis von unten die obere Band kreuzt (nach unten)
            if not current_short_entry and high_price >= upper_band:
                current_short_entry = {
                    'time': timestamp,
                    'price': close_price
                }
            
            # Exit Long: wenn Preis die obere Band nach oben kreuzt oder zurückgeht
            if current_long_entry:
                if high_price >= upper_band:
                    trade = {
                        'entry_long': current_long_entry, 
                        'exit_long': {'time': timestamp, 'price': close_price}
                    }
                    trades.append(trade)
                    current_long_entry = None
            
            # Exit Short: wenn Preis die untere Band nach unten kreuzt oder zurückgeht
            if current_short_entry:
                if low_price <= lower_band:
                    trade = {
                        'entry_short': current_short_entry,
                        'exit_short': {'time': timestamp, 'price': close_price}
                    }
                    trades.append(trade)
                    current_short_entry = None
        
        return trades, df_with_indicators
    except Exception as e:
        logger.warning(f"Fehler beim Extrahieren von Trades: {e}", exc_info=True)
        return [], df

def main():
    selected_configs = select_configs()
    
    print("\n" + "="*60)
    print("Chart-Optionen:")
    print("="*60)
    
    start_date = input("Startdatum (YYYY-MM-DD) [leer=beliebig]: ").strip() or None
    end_date = input("Enddatum (YYYY-MM-DD) [leer=heute]: ").strip() or None
    window_input = input("Letzten N Tage anzeigen [leer=alle]: ").strip()
    window = int(window_input) if window_input.isdigit() else None
    start_capital_input = input("Startkapital für Backtest (USDT) [leer=1000]: ").strip()
    start_capital = int(start_capital_input) if start_capital_input.isdigit() else 1000
    send_telegram = input("Telegram versenden? (j/n) [Standard: n]: ").strip().lower() in ['j', 'y', 'yes']
    
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
            secrets = json.load(f)
    except Exception as e:
        logger.error(f"Fehler beim Laden von secret.json: {e}")
        sys.exit(1)
    
    account = secrets.get('ltbbot', [None])[0]
    if not account:
        logger.error("Keine LTBBot-Accountkonfiguration gefunden")
        sys.exit(1)
    
    exchange = Exchange(account)
    telegram_config = secrets.get('telegram', {})
    
    for filename, filepath in selected_configs:
        try:
            logger.info(f"\nVerarbeite {filename}...")
            
            config = load_config(filepath)
            symbol = config.get('symbol') or config.get('market', {}).get('symbol')
            timeframe = config.get('timeframe') or config.get('market', {}).get('timeframe')
            
            if not symbol or not timeframe:
                logger.warning(f"Keine Symbol/Timeframe in {filename}")
                continue
            
            logger.info(f"Lade OHLCV-Daten für {symbol} {timeframe}...")
            
            if not start_date:
                start_date_for_load = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            else:
                start_date_for_load = start_date
            
            if not end_date:
                end_date_for_load = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            else:
                end_date_for_load = end_date
            
            df = exchange.fetch_historical_ohlcv(symbol, timeframe, start_date_for_load, end_date_for_load)
            
            if df is None or len(df) == 0:
                logger.warning(f"Keine Daten für {symbol} {timeframe}")
                continue
            
            # ===== BERECHNE INDIKATOREN & ENVELOPE-BÄNDER =====
            logger.info("Berechne Envelope-Bänder und Signale...")
            trades, df_with_indicators = extract_trades_from_backtest(df.copy(), config)
            logger.info(f"Gefundene Trade-Signale: {len(trades)}")
            
            # ===== BACKTEST AUSFÜHREN =====
            logger.info("Führe Backtest durch...")
            backtest_result = None
            try:
                backtest_result = run_envelope_backtest(df, config, start_capital=start_capital)
                logger.info(f"Backtest abgeschlossen: PnL={backtest_result.get('total_pnl_pct', 0):.2f}%, "
                           f"Trades={backtest_result.get('trades_count', 0)}, "
                           f"Win Rate={backtest_result.get('win_rate', 0):.1f}%")
            except Exception as e:
                logger.warning(f"Konnte Backtest nicht ausführen: {e}. Zeige Chart ohne Metriken...")
            
            logger.info("Erstelle Chart...")
            fig = create_interactive_chart(
                symbol,
                timeframe,
                df_with_indicators,  # Nutze df mit berechneten Indikatoren
                trades,  # Übergebe extrahierte Trades
                config,
                backtest_result=backtest_result,
                start_date=start_date,
                end_date=end_date,
                window=window
            )
            
            safe_name = f"{symbol.replace('/', '_')}_{timeframe}"
            output_file = f"/tmp/ltbbot_{safe_name}.html"
            fig.write_html(output_file)
            logger.info(f"✅ Chart gespeichert: {output_file}")
            
            if send_telegram and telegram_config:
                try:
                    logger.info(f"Sende Chart via Telegram...")
                    from ltbbot.utils.telegram import send_document
                    bot_token = telegram_config.get('bot_token')
                    chat_id = telegram_config.get('chat_id')
                    if bot_token and chat_id:
                        send_document(bot_token, chat_id, output_file, caption=f"Chart: {symbol} {timeframe}")
                except Exception as e:
                    logger.warning(f"Konnte Chart nicht via Telegram versenden: {e}")
        
        except Exception as e:
            logger.error(f"Fehler bei {filename}: {e}", exc_info=True)
            continue
    
    logger.info("\n✅ Alle Charts generiert!")

if __name__ == '__main__':
    main()
