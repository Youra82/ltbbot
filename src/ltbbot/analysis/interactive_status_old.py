#!/usr/bin/env python3
"""
Interactive Status für LTBBot - Envelope Strategy
Zeigt Candlestick-Chart mit Envelopes und simulierten Trades
Nutzt durchnummerierte Konfigurationsdateien zum Auswählen
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
import logging

import pandas as pd
import numpy as np
import plotly.graph_objects as go
import ta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

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
        # Extrahiere Symbol/Timeframe aus Dateiname
        clean_name = filename.replace('config_', '').replace('.json', '')
        print(f"{idx:2d}) {clean_name}")
    print("="*60)
    
    print("\nWähle Konfiguration(en) zum Anzeigen:")
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    
    selection = input("\nAuswahl: ").strip()
    
    # Parse Eingabe
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

def add_ltbbot_indicators(df):
    """Fügt LTBBot-spezifische Indikatoren (Envelopes) hinzu"""
    # SMA für Envelopes
    df['sma_fast'] = ta.trend.sma_indicator(df['close'], window=20)
    df['sma_slow'] = ta.trend.sma_indicator(df['close'], window=50)
    
    # ATR für Envelope-Breite
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    
    # Berechne Envelopes (vereinacht: 2 Ebenen)
    envelope_offset_1 = 0.5 * df['atr']  # Inneres Envelope
    envelope_offset_2 = 1.0 * df['atr']  # Äußeres Envelope
    
    df['envelope_long_1_upper'] = df['sma_fast'] + envelope_offset_1
    df['envelope_long_1_lower'] = df['sma_fast'] - envelope_offset_1
    
    df['envelope_long_2_upper'] = df['sma_fast'] + envelope_offset_2
    df['envelope_long_2_lower'] = df['sma_fast'] - envelope_offset_2
    
    return df

def create_interactive_chart(symbol, timeframe, df, trades, start_date, end_date, window=None):
    """Erstellt interaktiven Chart mit Envelopes und Trades"""
    
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
    
    # Candlestick
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='OHLC',
            showlegend=True
        )
    )
    
    # SMAs
    fig.add_trace(
        go.Scatter(x=df.index, y=df['sma_fast'], name='SMA 20', line=dict(color='orange', width=1.5))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['sma_slow'], name='SMA 50', line=dict(color='blue', width=1.5))
    )
    
    # Envelopes (Innere)
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_1_upper'], 
                   name='Envelope 1 Upper', line=dict(color='green', width=1, dash='dash'))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_1_lower'], 
                   name='Envelope 1 Lower', line=dict(color='green', width=1, dash='dash'),
                   fill='tonexty', fillcolor='rgba(0,255,0,0.1)')
    )
    
    # Envelopes (Äußere)
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_2_upper'], 
                   name='Envelope 2 Upper', line=dict(color='red', width=1, dash='dot'))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_2_lower'], 
                   name='Envelope 2 Lower', line=dict(color='red', width=1, dash='dot'))
    )
    
    # Trade Marker
    for trade in trades:
        entry_time = trade['entry_time']
        entry_price = trade['entry_price']
        exit_time = trade['exit_time']
        exit_price = trade['exit_price']
        profit = trade['profit']
        
        color = 'green' if profit > 0 else 'red'
        
        # Entry
        fig.add_trace(
            go.Scatter(
                x=[entry_time],
                y=[entry_price],
                mode='markers',
                marker=dict(size=10, color='green', symbol='triangle-up'),
                name=f'Entry ({entry_price:.2f})',
                showlegend=False
            )
        )
        
        # Exit
        fig.add_trace(
            go.Scatter(
                x=[exit_time],
                y=[exit_price],
                mode='markers',
                marker=dict(size=10, color=color, symbol='triangle-down'),
                name=f'Exit ({exit_price:.2f})',
                showlegend=False
            )
        )
        
        # Verbindungslinie
        fig.add_trace(
            go.Scatter(
                x=[entry_time, exit_time],
                y=[entry_price, exit_price],
                mode='lines',
                line=dict(color=color, width=1, dash='dash'),
                showlegend=False
            )
        )
    
    title = f"{symbol} {timeframe} - LTBBot (Envelope Strategy)"
    fig.update_layout(
        title=title,
        height=800,
        hovermode='x unified',
        template='plotly_dark'
    )
    
    fig.update_yaxes(title_text="Price")
    fig.update_xaxes(title_text="Time")
    
    return fig

def main():
    """Hauptfunktion: Config-basiertes interaktives Menü für Charts"""
    
    print("\n" + "="*60)
    print("LTBBot - Interactive Charts (Envelope Strategy)")
    print("="*60)
    
    try:
        # Schritt 1: Config Auswahl
        selected_configs = select_configs()
        
        # Schritt 2: Chart-Optionen
        print("\n" + "="*60)
        print("Chart-Optionen:")
        print("="*60)
        
        start_date = input("\nStart Datum (YYYY-MM-DD, leer = auto): ").strip()
        end_date = input("End Datum (YYYY-MM-DD, leer = auto): ").strip()
        window = input("Letzten N Tage anzeigen (leer = alle): ").strip()
        send_telegram = input("Via Telegram versenden? (j/n, leer = nein): ").strip().lower() == 'j'
        
        window = int(window) if window else None
        
        # Schritt 3: Load secrets
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
            secrets = json.load(f)
        
        account = secrets['ltbbot'][0]
        exchange = Exchange(account)
        
        # Schritt 4: Chart für jede Konfiguration erstellen
        for filename, config_path in selected_configs:
            try:
                logger.info(f"\n{'='*60}")
                logger.info(f"Verarbeite: {filename}")
                logger.info(f"{'='*60}")
                
                config = load_config(config_path)
                symbol = config.get('symbol')
                timeframe = config.get('timeframe')
                
                if not symbol or not timeframe:
                    logger.warning(f"Konfiguration fehlen Symbol oder Timeframe!")
                    continue
                
                logger.info(f"Lade Daten für {symbol} {timeframe}...")
                df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=500)
                
                logger.info("Berechne Indikatoren...")
                df = add_ltbbot_indicators(df)
                
                # Vereinachter Backtest
                trades = []
                
                logger.info("Erstelle Chart...")
                fig = create_interactive_chart(
                    symbol,
                    timeframe,
                    df,
                    trades,
                    start_date if start_date else None,
                    end_date if end_date else None,
                    window
                )
                
                # Speichere HTML
                safe_symbol = symbol.replace('/', '_').replace(':', '')
                output_file = f"/tmp/ltbbot_{safe_symbol}_{timeframe}.html"
                fig.write_html(output_file)
                logger.info(f"✅ Chart gespeichert: {output_file}")
                
                # Telegram versenden (optional)
                if send_telegram:
                    logger.info("Sende Chart via Telegram...")
                    telegram_config = secrets.get('telegram', {})
                    if telegram_config and os.path.exists(output_file):
                        from ltbbot.utils.telegram import send_document
                        bot_token = telegram_config.get('bot_token')
                        chat_id = telegram_config.get('chat_id')
                        if bot_token and chat_id:
                            send_document(bot_token, chat_id, output_file, caption=f"Chart: {symbol} {timeframe}")
                        else:
                            logger.warning("Telegram bot_token oder chat_id nicht konfiguriert")
                
            except Exception as e:
                logger.error(f"Fehler bei {filename}: {e}", exc_info=True)
                continue
        
        logger.info("\n✅ Alle Charts erstellt!")
        
    except Exception as e:
        logger.error(f"Fehler: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()
