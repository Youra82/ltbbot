#!/usr/bin/env python3
"""
Interactive Charts für LTBBot
Zeigt Candlestick-Chart mit Trade-Signalen (Entry/Exit Long/Short)
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

def create_interactive_chart(symbol, timeframe, df, trades, start_date, end_date, window=None):
    """Erstellt interaktiven Chart mit Candlesticks und Trade-Signalen (Entry/Exit)"""
    
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
    
    # Candlestick Chart
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
    
    # Trade-Signale extrahieren
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
    
    # Entry Long: grünes Dreieck nach oben
    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode="markers",
            marker=dict(color="#16a34a", symbol="triangle-up", size=14, line=dict(width=1.2, color="#0f5132")),
            name="Entry Long",
            showlegend=True
        ))
    
    # Exit Long: cyan Kreis
    if exit_long_x:
        fig.add_trace(go.Scatter(
            x=exit_long_x, y=exit_long_y, mode="markers",
            marker=dict(color="#22d3ee", symbol="circle", size=12, line=dict(width=1.1, color="#0e7490")),
            name="Exit Long",
            showlegend=True
        ))
    
    # Entry Short: oranges Dreieck nach unten
    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode="markers",
            marker=dict(color="#f59e0b", symbol="triangle-down", size=14, line=dict(width=1.2, color="#92400e")),
            name="Entry Short",
            showlegend=True
        ))
    
    # Exit Short: rotes Diamant
    if exit_short_x:
        fig.add_trace(go.Scatter(
            x=exit_short_x, y=exit_short_y, mode="markers",
            marker=dict(color="#ef4444", symbol="diamond", size=12, line=dict(width=1.1, color="#7f1d1d")),
            name="Exit Short",
            showlegend=True
        ))
    
    title = f"{symbol} {timeframe} - LTBBot"
    fig.update_layout(
        title=title,
        height=600,
        hovermode='x unified',
        template='plotly_white',
        dragmode='zoom',
        xaxis=dict(rangeslider=dict(visible=True), fixedrange=False),
        yaxis=dict(fixedrange=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        showlegend=True
    )
    
    fig.update_yaxes(title_text="Preis")
    fig.update_xaxes(fixedrange=False)
    
    return fig

def main():
    selected_configs = select_configs()
    
    print("\n" + "="*60)
    print("Chart-Optionen:")
    print("="*60)
    
    start_date = input("Startdatum (YYYY-MM-DD) [leer=beliebig]: ").strip() or None
    end_date = input("Enddatum (YYYY-MM-DD) [leer=heute]: ").strip() or None
    window_input = input("Letzten N Tage anzeigen [leer=alle]: ").strip()
    window = int(window_input) if window_input.isdigit() else None
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
            
            logger.info("Erstelle Chart...")
            fig = create_interactive_chart(
                symbol,
                timeframe,
                df,
                [],  # Keine Trades für diese Version
                start_date,
                end_date,
                window
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
