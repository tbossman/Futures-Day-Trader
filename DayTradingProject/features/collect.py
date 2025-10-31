import os
import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

EXCHANGE = os.getenv("EXCHANGE", "kraken").strip()
TF = os.getenv("TIMEFRAME", "1m").strip()
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USD").split(",")]

ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})

def fetch_ohlcv(symbol, limit=1000):
    try:
        o = ex.fetch_ohlcv(symbol, timeframe=TF, limit=limit)
    except Exception as e:
        raise RuntimeError(
            f"{ex.id}: failed to fetch {symbol} {TF}. "
            f"Try adjusting SYMBOLS in .env (BTC/USD vs BTC/USDT) or switch EXCHANGE. Error: {e}"
        )
    cols = ["timestamp","open","high","low","close","volume"]
    df = pd.DataFrame(o, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

if __name__ == "__main__":
    os.makedirs("data/raw", exist_ok=True)
    for sym in SYMBOLS:
        df = fetch_ohlcv(sym, limit=1000)
        out = f"data/raw/{sym.replace('/','_')}_{TF}.parquet"
        df.to_parquet(out, index=False)
        print("saved", out, "rows=", len(df))