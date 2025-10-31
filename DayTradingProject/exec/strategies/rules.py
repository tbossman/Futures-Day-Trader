# rules.py
import os
import pandas as pd

try:
    # If live3.py already called load_dotenv(), this is harmless.
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass


class EmaAtrStrategy:
    """
    EMA cross-up + ATR (in bps) filter controlled via environment variables.

    Env variables (all optional):
      - RISK_LEVEL: 3..8   (3 = riskier/more trades, 8 = most conservative)
      - MIN_ATR_BPS: float (override RISK_LEVEL mapping; e.g., 12.0 = 0.12%)
      - EMA_FAST_N: int    (default 12)
      - EMA_SLOW_N: int    (default 26)
      - ATR_N: int         (default 14)
      - ATR_FILTER: 'true'/'false' (default 'true') to disable the ATR gate quickly
      - TP_MULT: float     (default 1.5)  → optional reference TP = close + TP_MULT*ATR
      - SL_MULT: float     (default 1.0)  → optional reference SL = close - SL_MULT*ATR
    """

    def __init__(self):
        # Read knobs from the environment
        self.tp_mult = float(os.getenv("TP_MULT", "1.5"))
        self.sl_mult = float(os.getenv("SL_MULT", "1.0"))

        self.ema_fast_n = int(os.getenv("EMA_FAST_N", "12"))
        self.ema_slow_n = int(os.getenv("EMA_SLOW_N", "26"))
        self.atr_n      = int(os.getenv("ATR_N", "14"))

        self.atr_filter_enabled = os.getenv("ATR_FILTER", "true").lower() == "true"

        # Priority: explicit MIN_ATR_BPS overrides RISK_LEVEL mapping
        min_atr_bps_env = os.getenv("MIN_ATR_BPS")
        if min_atr_bps_env is not None and min_atr_bps_env != "":
            self.min_atr_bps = float(min_atr_bps_env)
        else:
            risk_level = int(os.getenv("RISK_LEVEL", "5"))
            self.min_atr_bps = self._risk_to_min_atr_bps(risk_level)

    # ---------- helpers ----------
    @staticmethod
    def _risk_to_min_atr_bps(level: int) -> float:
        """
        Heuristic mapping for short-interval crypto bars; adjust to your pair/timeframe.
        Lower threshold → more trades. Higher threshold → fewer trades (more conservative).
        """
        table = {
            3: 8.0,   # 0.08%
            4: 10.0,  # 0.10%
            5: 12.0,  # 0.12%
            6: 15.0,  # 0.15%
            7: 18.0,  # 0.18%
            8: 22.0,  # 0.22%
        }
        # clamp
        if level < 3: level = 3
        if level > 8: level = 8
        return table[level]

    @staticmethod
    def _ema(x: pd.Series, n: int) -> pd.Series:
        return x.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _atr(df: pd.DataFrame, n: int) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high - low).abs(),
             (high - prev_close).abs(),
             (low - prev_close).abs()],
            axis=1
        ).max(axis=1)
        return tr.rolling(n, min_periods=1).mean()

    def _ensure_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "ema_fast" not in out.columns:
            out["ema_fast"] = self._ema(out["close"], self.ema_fast_n)
        if "ema_slow" not in out.columns:
            out["ema_slow"] = self._ema(out["close"], self.ema_slow_n)
        if "atr" not in out.columns:
            out["atr"] = self._atr(out, self.atr_n)
        out["atr_bps"] = (out["atr"] / out["close"]).fillna(0) * 10_000
        return out

    # ---------- main signal ----------
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._ensure_indicators(df)

        # Cross-up: today's fast > slow AND yesterday fast <= slow
        out["fast_above"] = out["ema_fast"] > out["ema_slow"]
        out["cross_up"] = out["fast_above"] & (~out["fast_above"].shift(1).fillna(False))

        if self.atr_filter_enabled:
            out["atr_ok"] = out["atr_bps"] >= self.min_atr_bps
        else:
            out["atr_ok"] = True

        out["long_signal"] = out["cross_up"] & out["atr_ok"]

        # Optional ATR-based reference levels (use your actual fill price in live runner)
        out["tp_price_ref"] = out["close"] + self.tp_mult * out["atr"]
        out["sl_price_ref"] = out["close"] - self.sl_mult * out["atr"]

        return out