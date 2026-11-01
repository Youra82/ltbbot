"""Laedt 1m-OHLCV (Bitget, public) fuer den ehrlichen Live-Mechanik-Simulator.

Cache: data/fine_1m/<SYMBOL>_1m.parquet -- inkrementell, ein erneuter Aufruf
laedt nur fehlende Kerzen nach.

Aufruf: python download_fine_1m.py --days 365 --symbols BTC,ETH,...
"""
import argparse, os, time
from concurrent.futures import ThreadPoolExecutor
import ccxt
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, 'data', 'fine_1m')


def cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, symbol.replace('/', '-').replace(':', '-') + '_1m.parquet')


def load_fine_1m(symbol: str) -> pd.DataFrame:
    p = cache_path(symbol)
    if not os.path.exists(p):
        return pd.DataFrame()
    return pd.read_parquet(p)


def download(symbol: str, days: int) -> str:
    ex = ccxt.bitget({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    p = cache_path(symbol)
    old = load_fine_1m(symbol)
    now_ms = ex.milliseconds()
    start_ms = now_ms - days * 86400_000
    if not old.empty:
        start_ms = max(start_ms, int(old.index.max().timestamp() * 1000) + 1)
    rows, cur, fails = [], start_ms, 0
    while cur < now_ms - 60_000 and fails < 8:
        try:
            chunk = ex.fetch_ohlcv(symbol, '1m', since=cur, limit=1000)
        except Exception:
            fails += 1; time.sleep(3); continue
        if not chunk:
            # leere Zwischenantwort (Luecke) -> ein Stueck weiterspringen statt abbrechen
            cur += 1000 * 60_000; fails += 1; continue
        fails = 0
        rows.extend(chunk)
        cur = chunk[-1][0] + 1
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    if not df.empty:
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df = df.set_index('timestamp')
    if not old.empty:
        df = pd.concat([old, df])
    df = df[~df.index.duplicated(keep='last')].sort_index()
    # Kerze der aktuell laufenden Minute nie speichern
    df = df[df.index < pd.Timestamp.now(tz='UTC').floor('min')]
    df.to_parquet(p)
    return f"{symbol}: {len(df)} Kerzen {df.index.min()} .. {df.index.max()}"


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--symbols', required=True, help='Komma-getrennt, z.B. BTC,ETH')
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()
    syms = [s.strip() for s in a.symbols.split(',') if s.strip()]
    if not syms:
        raise SystemExit('Keine Symbole angegeben.')
    syms = [s if '/' in s else f'{s}/USDT:USDT' for s in syms]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with ThreadPoolExecutor(a.jobs) as pool:
        for msg in pool.map(lambda s: download(s, a.days), syms):
            print(msg, flush=True)
