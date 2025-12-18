# src/ltbbot/analysis/evaluator.py
import pandas as pd
import numpy as np
import ta # Make sure 'ta' is installed
import sys
import os
import logging # Added logging

logger = logging.getLogger(__name__)

# Pfad anpassen, falls nötig (sollte aber stimmen)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
# sys.path.append(os.path.join(PROJECT_ROOT, 'src')) # Nicht nötig, wenn korrekt aufgerufen

# ANN-spezifische Imports entfernt

def evaluate_dataset(data: pd.DataFrame, timeframe: str):
    """
    Bewertet einen Datensatz für die Optimierung (angepasst für generelle Nutzung).
    Gibt eine Note von 0-10, eine Begründung und Phasenverteilung zurück.
    """
    MIN_CANDLES = 200 # Mindestanzahl Kerzen für eine Bewertung

    if data is None or data.empty or len(data) < MIN_CANDLES:
        return {
            "score": 0,
            "justification": [
                f"- Phasen-Verteilung (0/4): Nicht bewertbar. Zu wenig Daten (< {MIN_CANDLES}).",
                f"- Volatilität (0/4): Nicht bewertbar. Zu wenig Daten.",
                f"- Datenmenge (0/2): Mangelhaft. Weniger als {MIN_CANDLES} Kerzen."
            ],
            "phase_dist": {}
        }

    try:
        # Stelle sicher, dass der Index ein DatetimeIndex ist
        if not isinstance(data.index, pd.DatetimeIndex):
             data.index = pd.to_datetime(data.index)

        # --- Metrik 1: Phasen-Verteilung (max. 4 Punkte) ---
        # Verwendet EMAs zur Trendbestimmung
        data['ema_50'] = ta.trend.ema_indicator(data['close'], window=50)
        data['ema_200'] = ta.trend.ema_indicator(data['close'], window=200)
        data.dropna(subset=['ema_50', 'ema_200'], inplace=True) # Zeilen ohne EMAs entfernen

        if len(data) < MIN_CANDLES / 2: # Erneute Prüfung nach dropna
             raise ValueError("Zu wenig Daten nach EMA-Berechnung.")

        conditions = [
            (data['close'] > data['ema_50']) & (data['ema_50'] > data['ema_200']), # Klarer Aufwärtstrend
            (data['close'] < data['ema_50']) & (data['ema_50'] < data['ema_200'])  # Klarer Abwärtstrend
        ]
        choices = ['Aufwärts', 'Abwärts']
        data['phase'] = np.select(conditions, choices, default='Seitwärts/Unklar')

        phase_dist = data['phase'].value_counts(normalize=True)
        max_phase_pct = phase_dist.max() if not phase_dist.empty else 1.0

        # Bewertung der Verteilung
        if max_phase_pct > 0.8: score1 = 0 # Sehr einseitig
        elif max_phase_pct > 0.7: score1 = 1
        elif max_phase_pct > 0.6: score1 = 2
        elif max_phase_pct > 0.5: score1 = 3
        else: score1 = 4 # Gut verteilt

        dist_text = ", ".join([f"{name}: {pct:.0%}" for name, pct in phase_dist.items()])
        just1 = f"- Phasen-Verteilung ({score1}/4): {'Exzellent' if score1==4 else 'Gut' if score1==3 else 'Mäßig' if score1==2 else 'Einseitig'}. ({dist_text})"

        # --- Metrik 2: Volatilität / Handelbarkeit (max. 4 Punkte) ---
        # Verwendet ATR als Maß für Volatilität
        data['atr'] = ta.volatility.average_true_range(data['high'], data['low'], data['close'], window=14)
        data['atr_normalized'] = (data['atr'] / data['close']) * 100 # ATR in % des Preises
        data.dropna(subset=['atr_normalized'], inplace=True)

        if len(data) < MIN_CANDLES / 3: # Erneute Prüfung
             raise ValueError("Zu wenig Daten nach ATR-Berechnung.")

        median_atr_pct = data['atr_normalized'].median() # Median ist robuster gegen Ausreißer

        # Bewertung der Volatilität (Beispielhafte Schwellenwerte, anpassbar!)
        if median_atr_pct < 0.2: score2 = 0 # Zu niedrig, kaum Bewegung
        elif median_atr_pct < 0.5: score2 = 1
        elif median_atr_pct < 1.5: score2 = 3 # Guter Bereich
        elif median_atr_pct < 3.0: score2 = 4 # Hohe Volatilität, gut für Strategie?
        else: score2 = 2 # Sehr hohe Volatilität, evtl. zu riskant?

        just2 = f"- Volatilität ({score2}/4): {'Sehr Hoch' if score2==2 else 'Hoch' if score2==4 else 'Moderat' if score2==3 else 'Gering' if score2==1 else 'Sehr Gering'}. Median ATR: {median_atr_pct:.2f}%."

        # --- Metrik 3: Datenmenge (max. 2 Punkte) ---
        num_candles = len(data) # Anzahl nach dropna
        if num_candles < 1000: score3 = 0
        elif num_candles < 5000: score3 = 1
        else: score3 = 2
        just3 = f"- Datenmenge ({score3}/2): {'Exzellent (>=5k)' if score3==2 else 'Ausreichend (>=1k)' if score3==1 else 'Gering (<1k)'}. {num_candles:,} Kerzen nach Filterung."

        # --- Gesamtergebnis ---
        total_score = score1 + score2 + score3
        return {
            "score": total_score,
            "justification": [just1, just2, just3],
            "phase_dist": phase_dist.to_dict()
        }

    except Exception as e:
         logger.error(f"Fehler bei der Datensatzbewertung: {e}", exc_info=True)
         # Gib einen Fehler-Score zurück
         return {
             "score": 0,
             "justification": [
                 f"- Phasen-Verteilung (0/4): Fehler bei Berechnung ({e})",
                 f"- Volatilität (0/4): Fehler bei Berechnung ({e})",
                 f"- Datenmenge (0/2): Fehler bei Berechnung ({e})"
             ],
             "phase_dist": {}
         }
