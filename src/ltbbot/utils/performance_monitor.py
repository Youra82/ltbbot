# src/ltbbot/utils/performance_monitor.py
"""
Performance Monitoring und Auto-Deactivation System.
Überwacht Live-Performance und deaktiviert Strategien bei schlechter Performance.
"""

import json
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')

def check_strategy_health(tracker_file_path, symbol, timeframe):
    """
    Überprüft die Gesundheit einer Strategie und entscheidet über Deaktivierung.
    
    Returns:
        tuple: (should_continue: bool, reason: str)
    """
    try:
        if not os.path.exists(tracker_file_path):
            return True, "Keine Performance-Daten vorhanden"
        
        with open(tracker_file_path, 'r') as f:
            tracker_info = json.load(f)
        
        perf = tracker_info.get('performance', {})
        total_trades = perf.get('total_trades', 0)
        
        # Zu wenig Trades für valide Bewertung
        if total_trades < 30:
            return True, f"Zu wenig Trades ({total_trades}) für Bewertung"
        
        win_rate = perf.get('win_rate', 0)
        consecutive_losses = perf.get('consecutive_losses', 0)
        max_consecutive_losses = perf.get('max_consecutive_losses', 0)
        
        # Kritische Bedingungen für Auto-Deactivation
        critical_conditions = []
        
        # 1. Win-Rate unter 25% nach 30+ Trades
        if win_rate < 25:
            critical_conditions.append(f"Win-Rate zu niedrig: {win_rate:.1f}%")
        
        # 2. 8+ aufeinanderfolgende Verluste
        if consecutive_losses >= 8:
            critical_conditions.append(f"{consecutive_losses} aufeinanderfolgende Verluste")
        
        # 3. Max consecutive losses > 12
        if max_consecutive_losses > 12:
            critical_conditions.append(f"Max. {max_consecutive_losses} Verluste in Folge")
        
        # 4. Win-Rate unter 20% nach 50+ Trades (extrem kritisch)
        if total_trades >= 50 and win_rate < 20:
            critical_conditions.append(f"KRITISCH: {win_rate:.1f}% WR nach {total_trades} Trades")
        
        if critical_conditions:
            reason = " | ".join(critical_conditions)
            logger.critical(f"🚨 AUTO-DEACTIVATION: {symbol} ({timeframe}) - {reason}")
            return False, reason
        
        # Warnung bei mittelmäßiger Performance
        if win_rate < 35 and total_trades >= 30:
            logger.warning(f"⚠️ SCHWACHE PERFORMANCE: {symbol} ({timeframe}) - WR: {win_rate:.1f}%")
        
        return True, f"Performance OK (WR: {win_rate:.1f}% über {total_trades} Trades)"
        
    except Exception as e:
        logger.error(f"Fehler bei Strategy Health Check für {symbol}: {e}")
        return True, f"Fehler: {e}"

def deactivate_strategy_in_settings(symbol, timeframe, reason):
    """
    Deaktiviert eine Strategie in settings.json.
    
    Args:
        symbol: Trading-Paar (z.B. "BTC/USDT:USDT")
        timeframe: Zeitrahmen (z.B. "4h")
        reason: Grund für Deaktivierung
    
    Returns:
        bool: Erfolg der Deaktivierung
    """
    try:
        # Lade settings.json
        if not os.path.exists(SETTINGS_FILE):
            logger.error(f"settings.json nicht gefunden: {SETTINGS_FILE}")
            return False
        
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
        
        # Finde und deaktiviere die Strategie
        strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
        strategy_found = False
        
        for strategy in strategies:
            if strategy.get('symbol') == symbol and strategy.get('timeframe') == timeframe:
                strategy['active'] = False
                strategy['_deactivation_reason'] = reason
                strategy['_deactivation_date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                strategy_found = True
                logger.info(f"✅ Strategie {symbol} ({timeframe}) in settings.json deaktiviert")
                break
        
        if not strategy_found:
            logger.warning(f"Strategie {symbol} ({timeframe}) nicht in settings.json gefunden")
            return False
        
        # Speichere geänderte settings.json
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
        
        return True
        
    except Exception as e:
        logger.error(f"Fehler beim Deaktivieren von {symbol} in settings.json: {e}")
        return False

def generate_performance_report(tracker_file_path, symbol):
    """
    Generiert einen detaillierten Performance-Report.
    
    Returns:
        str: Formatierter Report
    """
    try:
        if not os.path.exists(tracker_file_path):
            return f"Keine Performance-Daten für {symbol}"
        
        with open(tracker_file_path, 'r') as f:
            tracker_info = json.load(f)
        
        perf = tracker_info.get('performance', {})
        
        if not perf or perf.get('total_trades', 0) == 0:
            return f"Keine Trades für {symbol} aufgezeichnet"
        
        total = perf.get('total_trades', 0)
        wins = perf.get('winning_trades', 0)
        losses = perf.get('losing_trades', 0)
        win_rate = perf.get('win_rate', 0)
        consec_losses = perf.get('consecutive_losses', 0)
        max_consec_losses = perf.get('max_consecutive_losses', 0)
        
        report = f"""
╔══════════════════════════════════════════════════════════════╗
║  PERFORMANCE REPORT: {symbol:40s}  ║
╚══════════════════════════════════════════════════════════════╝

📊 Gesamt-Statistiken:
   Total Trades:              {total:>5d}
   Gewinner:                  {wins:>5d}
   Verlierer:                 {losses:>5d}
   Win-Rate:                  {win_rate:>5.1f}%

⚠️  Verlust-Serien:
   Aktuell aufeinanderfolgend: {consec_losses:>5d}
   Maximum aufeinanderfolgend: {max_consec_losses:>5d}

💡 Bewertung:
"""
        
        # Bewertung hinzufügen
        if win_rate >= 45:
            report += "   ✅ AUSGEZEICHNET - Strategie läuft sehr gut\n"
        elif win_rate >= 40:
            report += "   ✅ GUT - Strategie profitabel\n"
        elif win_rate >= 35:
            report += "   ⚠️  BEFRIEDIGEND - Verbesserungspotential\n"
        elif win_rate >= 30:
            report += "   ⚠️  SCHWACH - Überwachung erforderlich\n"
        elif win_rate >= 25:
            report += "   🚨 KRITISCH - Deaktivierung empfohlen\n"
        else:
            report += "   🚨 INAKZEPTABEL - Sofortige Deaktivierung!\n"
        
        if consec_losses >= 5:
            report += f"   🚨 WARNUNG: {consec_losses} Verluste in Folge!\n"
        
        report += "\n" + "="*64 + "\n"
        
        return report
        
    except Exception as e:
        logger.error(f"Fehler beim Generieren des Reports für {symbol}: {e}")
        return f"Fehler beim Report-Generieren: {e}"
