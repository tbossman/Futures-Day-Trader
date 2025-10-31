
# strategies/ema_atr.py — emit signals but TP% is always 4.9 here for consistency
import pandas as pd

def ema(x: pd.Series, n: int):
    return x.ewm(span=n, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()

def add_indicators(
    df: pd.DataFrame,
    min_atr_bps: float = 3.0,
    sl_pct: float = 13.44,
    tp_pct: float = 4.9,          # <- ALWAYS 4.9 to match predictor
    enable_short: bool = False,
) -> pd.DataFrame:
    """
    Returns a copy with:
      ema_fast, ema_slow, atr, atr_bps,
      long_signal, short_signal,
      sl_pct, tp_pct,
      tp_price_long, sl_price_long, tp_price_short, sl_price_short
    """
    out = df.copy()
    out["ema_fast"] = ema(out["close"], 12)
    out["ema_slow"] = ema(out["close"], 26)
    out["atr"] = atr(out, 14)
    out["atr_bps"] = (out["atr"] / out["close"]).fillna(0) * 10_000

    out["long_signal"] = (
        (out["ema_fast"] > out["ema_slow"]) &
        (out["ema_fast"].shift(1) <= out["ema_slow"].shift(1)) &
        (out["atr_bps"] >= float(min_atr_bps))
    )
    if enable_short:
        out["short_signal"] = (
            (out["ema_fast"] < out["ema_slow"]) &
            (out["ema_fast"].shift(1) >= out["ema_slow"].shift(1)) &
            (out["atr_bps"] >= float(min_atr_bps))
        )
    else:
        out["short_signal"] = False

    out["sl_pct"] = float(sl_pct)
    out["tp_pct"] = float(tp_pct)

    entry = out["close"]
    out["tp_price_long"]   = entry * (1.0 + out["tp_pct"]/100.0)
    out["sl_price_long"]   = entry * (1.0 - out["sl_pct"]/100.0)
    out["tp_price_short"]  = entry * (1.0 - out["tp_pct"]/100.0)
    out["sl_price_short"]  = entry * (1.0 + out["sl_pct"]/100.0)
    return out
