import os, pandas as pd, yfinance as yf
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

def load_btc(path="data/processed/BTC_USD_1m_feat.parquet"):
    return pd.read_parquet(path)

def load_macro():
    tickers = {"DXY":"DX-Y.NYB","ES":"ES=F","NQ":"NQ=F","VIX":"^VIX"}
    dfs=[]
    for name,tkr in tickers.items():
        try:
            d = yf.download(tkr, period="6mo", interval="1d", progress=False)[["Close"]].rename(columns={"Close":name})
            dfs.append(d)
        except Exception:
            pass
    macro = pd.concat(dfs, axis=1).ffill()
    macro.index = pd.to_datetime(macro.index, utc=True)
    return macro

def join_and_expand(btc_df, macro_df):
    out = btc_df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.set_index("timestamp")
    macro = macro_df.reindex(out.index, method="ffill")
    out = pd.concat([out, macro], axis=1).ffill()
    for col in ["DXY","ES","NQ","VIX"]:
        if col in out: out[f"{col}_ret"] = out[col].pct_change().fillna(0)
    out["ema_fast"] = EMAIndicator(out["close"], window=20).ema_indicator()
    out["ema_slow"] = EMAIndicator(out["close"], window=50).ema_indicator()
    out["rsi"]      = RSIIndicator(out["close"], window=14).rsi()
    out["atr"]      = AverageTrueRange(out["high"], out["low"], out["close"], window=14).average_true_range()
    return out.dropna().reset_index()

if __name__ == "__main__":
    os.makedirs("data/processed_plus", exist_ok=True)
    btc = load_btc()
    macro = load_macro()
    out = join_and_expand(btc, macro)
    out.to_parquet("data/processed_plus/BTC_USD_1m_feat_plus.parquet", index=False)
    print("wrote data/processed_plus/BTC_USD_1m_feat_plus.parquet", len(out))
