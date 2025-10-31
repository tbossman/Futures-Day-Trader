import os, time, csv, datetime as dt
import ccxt
import pandas as pd
from dotenv import load_dotenv
from strategies.rules import EmaAtrStrategy

load_dotenv()

# Read config from .env (strings!)
EXCHANGE = os.getenv("EXCHANGE", "kraken").strip()         # e.g. coinbase, kraken, bitstamp
SYMBOLS = os.getenv("SYMBOLS", "BTC/USD").split(",")       # e.g. BTC/USD,ETH/USD
SYMBOL = SYMBOLS[0].strip()
TF = os.getenv("TIMEFRAME", "1m").strip()

# Create exchange
ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})

# ---------- Risk config ----------
risk_per_trade = 0.01     # 1% of equity
equity = 1000.0           # paper equity
start_equity_today = equity
daily_loss_limit = 0.03   # halt for the day at -3%
max_consec_losses = 3
consec_losses = 0

# ---------- Position state ----------
in_pos = False
entry = 0.0
sl = 0.0
tp = 0.0
qty = 0.0

# ---------- Logging ----------
os.makedirs("logs", exist_ok=True)
TRADES_CSV = "logs/trades.csv"
if not os.path.exists(TRADES_CSV):
    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts","symbol","side","entry","exit","pnl","equity","reason"])

def latest_candles(n=200):
    cols = ["timestamp","open","high","low","close","volume"]
    try:
        o = ex.fetch_ohlcv(SYMBOL, timeframe=TF, limit=n)
    except Exception as e:
        raise RuntimeError(
            f"{ex.id}: fetch_ohlcv failed for {SYMBOL} {TF}. "
            f"Try adjusting SYMBOLS in .env (BTC/USD vs BTC/USDT) or switch EXCHANGE. Error: {e}"
        )
    df = pd.DataFrame(o, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

def spread_ok(threshold_bps=5):
    # 5 bps = 0.05%
    ob = ex.fetch_order_book(SYMBOL, limit=10)
    bid = ob["bids"][0][0] if ob["bids"] else None
    ask = ob["asks"][0][0] if ob["asks"] else None
    if not bid or not ask:
        return False
    spr = (ask - bid) / ((ask + bid) / 2.0)
    return spr <= threshold_bps / 10000.0

def depth_ok(min_usd=20000):
    ob = ex.fetch_order_book(SYMBOL, limit=20)
    def side_sum(levels):
        tot = 0.0
        for p, q in levels[:5]:
            tot += p * q
        return tot
    return side_sum(ob["bids"]) >= min_usd and side_sum(ob["asks"]) >= min_usd

def log_trade(side, entry_px, exit_px, pnl, reason):
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            dt.datetime.utcnow().isoformat(), SYMBOL, side,
            f"{entry_px:.6f}", f"{exit_px:.6f}", f"{pnl:.6f}", f"{equity:.2f}", reason
        ])

if __name__ == "__main__":
    strat = EmaAtrStrategy()
    print(f"Paper trading {SYMBOL} on {EXCHANGE} {TF}")
    last_day = dt.datetime.utcnow().date()

    while True:
        try:
            # reset daily controls at UTC day change
            if dt.datetime.utcnow().date() != last_day:
                last_day = dt.datetime.utcnow().date()
                start_equity_today = equity
                consec_losses = 0

            # daily halt check
            if (equity - start_equity_today) / max(start_equity_today, 1e-9) <= -daily_loss_limit:
                print("[PAPER] Daily loss limit reached, halting until next UTC day.")
                time.sleep(10)
                continue

            df = latest_candles()
            # live features
            df["ema_fast"] = df["close"].ewm(span=20, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=50, adjust=False).mean()
            df["atr"] = df["close"].diff().abs().rolling(14).mean()
            df.dropna(inplace=True)

            sigs = strat.generate_signals(df)
            price = float(df["close"].iloc[-1])

            # quality filters
            ok_spread = spread_ok(threshold_bps=15)
            ok_depth = depth_ok(min_usd=2000)

            if not in_pos and consec_losses < max_consec_losses and ok_spread and ok_depth:
                if sigs["long_signal"].iloc[-1]:
                    risk_dollars = equity * risk_per_trade
                    atr = float(df["atr"].iloc[-1] or 0.0)
                    if atr > 0:
                        sl = price - 1.0 * atr
                        tp = price + 1.5 * atr
                        risk_per_unit = max(price - sl, 1e-9)
                        qty = risk_dollars / risk_per_unit
                        entry = price
                        in_pos = True
                        print(f"[PAPER] BUY {SYMBOL} qty={qty:.6f} at {price:.2f} sl {sl:.2f} tp {tp:.2f}")

            if in_pos:
                if price <= sl:
                    pnl = (sl - entry) * qty
                    equity += pnl
                    in_pos = False
                    consec_losses += 1
                    log_trade("LONG", entry, sl, pnl, "SL")
                    print(f"[PAPER] STOP HIT. equity={equity:.2f}")
                elif price >= tp:
                    pnl = (tp - entry) * qty
                    equity += pnl
                    in_pos = False
                    consec_losses = 0
                    log_trade("LONG", entry, tp, pnl, "TP")
                    print(f"[PAPER] TAKE PROFIT. equity={equity:.2f}")

            time.sleep(5)
        except Exception as e:
            print("loop error:", e)
            time.sleep(3)