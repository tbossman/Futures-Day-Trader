import glob, os
import pandas as pd

# TA indicators from the 'ta' library (stable with modern numpy/pandas)
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # Basic returns
    out["ret_1"] = out["close"].pct_change()

    # EMAs
    ema_fast = EMAIndicator(close=out["close"], window=20, fillna=False)
    ema_slow = EMAIndicator(close=out["close"], window=50, fillna=False)
    out["ema_fast"] = ema_fast.ema_indicator()
    out["ema_slow"] = ema_slow.ema_indicator()

    # RSI
    rsi = RSIIndicator(close=out["close"], window=14, fillna=False)
    out["rsi"] = rsi.rsi()

    # True ATR (uses high/low/close)
    atr = AverageTrueRange(high=out["high"], low=out["low"], close=out["close"], window=14, fillna=False)
    out["atr"] = atr.average_true_range()

    out.dropna(inplace=True)
    return out

if __name__ == "__main__":
    os.makedirs("data/processed", exist_ok=True)
    for p in glob.glob("data/raw/*.parquet"):
        df = pd.read_parquet(p)
        df_feat = build_features(df)
        name = os.path.basename(p).replace(".parquet", "_feat.parquet")
        outp = f"data/processed/{name}"
        df_feat.to_parquet(outp, index=False)
        print("features ->", outp, len(df_feat))