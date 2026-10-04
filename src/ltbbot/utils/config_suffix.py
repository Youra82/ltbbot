# src/ltbbot/utils/config_suffix.py
"""Welche Config-Dateien aktiv sind (2026-10-04): settings.json optimization_settings.config_suffix,
z.B. '_envelope' (alte Umkehr-Strategie) oder '_bo' (Band-Durchbruch). Eine Quelle fuer Live-Bot
(run.py), Portfolio-Optimierung, Scheduler, sync_confirmed_flags und Tages-Check."""
import json
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def get_config_suffix(default='_envelope'):
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json'), encoding='utf-8') as f:
            return json.load(f).get('optimization_settings', {}).get('config_suffix', default) or default
    except Exception:
        return default
