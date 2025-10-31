# exec/live.py
import os, time, csv
from datetime import datetime, timezone
import ccxt
import pandas as pd
from dotenv import load_dotenv
from strategies.rules import EmaAtrStrategy

load_dotenv()

# ---------- ENV / CONFIG ----------
EXCHANGE   = os.getenv("EXCHANGE", "kraken").strip()
SYMBOL     = os.getenv("SYMBOLS", "DOGE/USD").split(",")[0].strip()
TF         = os.getenv("TIMEFRAME", "15m").strip()
API_KEY    = os.getenv("API_KEY", "") or ""
API_SECRET = os.getenv("API_SECRET", "") or ""

# sizing & safety (QUOTE = USD)
FIXED_SPEND_QUOTE  = float(os.getenv("FIXED_SPEND_QUOTE", "8"))    # ~USD spent per entry
MIN_NOTIONAL_QUOTE = float(os.getenv("MIN_NOTIONAL_QUOTE", "5"))   # Kraken min ~$5
DAILY_LOSS_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT", "0.06"))  # -6% halt for the day
POLL_SEC           = int(os.getenv("POLL_SEC", "5"))
FEE_SLIP_BUFFER    = float(os.getenv("FEE_SLIP_BUFFER", "0.01"))   # 1% cushion

# order routing
USE_MAKER               = (os.getenv("USE_MAKER", "false").lower() == "true")  # maker entries?
MAKER_ENTRY_OFFSET_BPS  = float(os.getenv("MAKER_ENTRY_OFFSET_BPS", "5"))      # 0.05% below last
MAKER_TP_POST           = (os.getenv("MAKER_TP_POST", "true").lower() == "true")
MAKER_STALE_SEC         = int(os.getenv("MAKER_STALE_SEC", "20"))

# fee assumptions for edge check
TAKER_FEE_RATE   = float(os.getenv("TAKER_FEE_RATE", "0.004"))   # 0.40% per side
MAKER_FEE_RATE   = float(os.getenv("MAKER_FEE_RATE", "0.0016"))  # 0.16% per side (adjust to your tier)
FEE_EDGE_BUFFER  = float(os.getenv("FEE_EDGE_BUFFER", "1.2"))    # require TP >= fees * buffer

# ---------- EXCHANGE ----------
ex = getattr(ccxt, EXCHANGE)({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
ex.load_markets()
mkt       = ex.markets.get(SYMBOL, {}) or {}
precision = mkt.get("precision") or {}
limits    = mkt.get("limits") or {}
prec_amt  = precision.get("amount")
amt_min   = ((limits.get("amount") or {}).get("min")) or 0.0
cost_min  = ((limits.get("cost")   or {}).get("min")) or 0.0
if cost_min:
    MIN_NOTIONAL_QUOTE = max(MIN_NOTIONAL_QUOTE, float(cost_min))

BASE, QUOTE = SYMBOL.split("/")

# ---------- HELPERS ----------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def latest_candles(n=200) -> pd.DataFrame:
    cols = ["timestamp","open","high","low","close","volume"]
    raw = ex.fetch_ohlcv(SYMBOL, timeframe=TF, limit=n)
    df = pd.DataFrame(raw, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

def fetch_total_equity_quote() -> float:
    """Total equity valued in quote currency (USD)."""
    bal   = ex.fetch_balance()
    total = bal.get("total", {}) or {}
    q_amt = float(total.get(QUOTE, 0.0) or 0.0)
    b_amt = float(total.get(BASE, 0.0)  or 0.0)
    try:
        t = ex.fetch_ticker(SYMBOL)
        px = t.get("last") or t.get("close")
        price = float(px or 0.0)
    except Exception:
        price = 0.0
    return q_amt + b_amt * max(price, 0.0)

def fetch_free_quote() -> float:
    """Free (available) USD for sizing."""
    bal = ex.fetch_balance()
    free = bal.get("free", {}) or {}
    return float(free.get(QUOTE, 0.0) or 0.0)

def spread_ok(threshold_bps=20) -> bool:
    """Skip trading when spread > ~0.20%."""
    ob = ex.fetch_order_book(SYMBOL, limit=15)
    bid = ob["bids"][0][0] if ob["bids"] else None
    ask = ob["asks"][0][0] if ob["asks"] else None
    if not bid or not ask:
        return False
    spr = (ask - bid) / ((ask + bid) / 2.0)
    return spr <= threshold_bps / 10000.0

def floor_qty_to_precision(q: float) -> float:
    if isinstance(prec_amt, (int, float)):
        # amount precision is decimal places count for Kraken in ccxt
        step = 10 ** (-int(prec_amt))
        return (int(q / step)) * step
    return q

def bps(x: float, bps_val: float) -> float:
    return x * (bps_val / 10000.0)

def post_only_limit(side: str, qty: float, limit_px: float):
    """Post-only maker order."""
    return ex.create_order(SYMBOL, "limit", side, qty, limit_px, {"postOnly": True})

def wait_fill_or_cancel(order_id: str, stale_sec: int) -> bool:
    """Poll until filled or timeout; cancel if still open."""
    t0 = time.time()
    while time.time() - t0 < stale_sec:
        try:
            o = ex.fetch_order(order_id, SYMBOL)
            status = (o.get("status") or "").lower()
            if status in ("closed", "filled"):
                return True
            if status in ("canceled", "cancelled", "rejected", "expired"):
                return False
        except Exception:
            pass
        time.sleep(1.0)
    try:
        ex.cancel_order(order_id, SYMBOL)
    except Exception:
        pass
    return False

# ---------- LOGGING ----------
os.makedirs("logs", exist_ok=True)
TRADES_CSV = "logs/live_trades.csv"
if not os.path.exists(TRADES_CSV):
    with open(TRADES_CSV, "w", newline="") as f:
        csv.writer(f).writerow(["ts","symbol","side","entry","exit","pnl","balance","reason"])

# ---------- STATE ----------
in_pos = False
entry = sl = tp = qty = 0.0
equity_at_entry = None

def run():
    strat = EmaAtrStrategy()
    start_equity = fetch_total_equity_quote()
    print(f"[BOOT] {EXCHANGE} {SYMBOL} tf={TF} capital=${start_equity:.2f} "
          f"fixedSpend=${FIXED_SPEND_QUOTE:.2f} minNotional=${MIN_NOTIONAL_QUOTE:.2f} "
          f"useMaker={USE_MAKER} makerOffsetBps={MAKER_ENTRY_OFFSET_BPS}")

    last_day = datetime.now(timezone.utc).date()
    global in_pos, entry, sl, tp, qty, equity_at_entry

    while True:
        try:
            # daily reset baseline
            today = datetime.now(timezone.utc).date()
            if today != last_day:
                last_day = today
                start_equity = fetch_total_equity_quote()
                print("[ROLL] New UTC day baseline set.")

            cur_equity = fetch_total_equity_quote()
            if start_equity > 0 and (cur_equity - start_equity) / start_equity <= -DAILY_LOSS_LIMIT:
                print("[HALT] Daily loss cap reached; pausing until next UTC day.")
                time.sleep(30)
                continue

            # features / signals
            df = latest_candles()
            df["ema_fast"] = df["close"].ewm(span=20, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=50, adjust=False).mean()
            df["atr"]      = df["close"].diff().abs().rolling(14).mean()
            df.dropna(inplace=True)

            sigs  = strat.generate_signals(df)
            price = float(df["close"].iloc[-1])

            if not spread_ok():
                time.sleep(POLL_SEC)
                continue

            # -------- ENTRY --------
            if not in_pos and bool(sigs["long_signal"].iloc[-1]):
                atr = float(df["atr"].iloc[-1] or 0.0)
                if atr <= 0:
                    time.sleep(POLL_SEC); continue

                free_q = fetch_free_quote()
                if free_q < MIN_NOTIONAL_QUOTE:
                    print(f"[SKIP] free {QUOTE}={free_q:.2f} < minNotional {MIN_NOTIONAL_QUOTE:.2f}")
                    time.sleep(POLL_SEC); continue

                # size in QUOTE with headroom
                target_spend = FIXED_SPEND_QUOTE
                max_spend = free_q * (1.0 - FEE_SLIP_BUFFER)
                spend_quote = max(MIN_NOTIONAL_QUOTE, min(target_spend, max_spend))
                if spend_quote < MIN_NOTIONAL_QUOTE:
                    print(f"[SKIP] spend {spend_quote:.2f} < minNotional {MIN_NOTIONAL_QUOTE:.2f} "
                          f"after headroom; free={free_q:.2f}")
                    time.sleep(POLL_SEC); continue

                # compute SL/TP *before* fee-edge check (TP distance = 1.5 * ATR)
                sl = price - 1.0 * atr
                tp = price + 1.5 * atr

                # ---- FEE-EDGE CHECK: skip if expected TP gain can't beat round-trip fees ----
                est_qty  = spend_quote / max(price, 1e-12)
                gross_tp = est_qty * (tp - price)  # == est_qty * (1.5 * atr)

                entry_fee_rate = MAKER_FEE_RATE if USE_MAKER else TAKER_FEE_RATE
                # assume taker on SL; TP may be maker if MAKER_TP_POST
                exit_fee_rate  = MAKER_FEE_RATE if (USE_MAKER and MAKER_TP_POST) else TAKER_FEE_RATE
                est_fees = (entry_fee_rate + exit_fee_rate) * spend_quote

                if gross_tp <= est_fees * FEE_EDGE_BUFFER:
                    print(f"[SKIP] fee-edge: TP {gross_tp:.4f} < fees {est_fees:.4f} × {FEE_EDGE_BUFFER}")
                    time.sleep(POLL_SEC); continue
                # ---------------------------------------------------------------------------

                # qty (BASE) with headroom and precision
                qty_raw = (spend_quote / max(price, 1e-12)) * (1.0 - FEE_SLIP_BUFFER)
                qty = floor_qty_to_precision(qty_raw)
                if amt_min and qty < amt_min:
                    print(f"[SKIP] qty {qty:.8f} < min {amt_min} for {SYMBOL}. "
                          f"Increase FIXED_SPEND_QUOTE or balance.")
                    time.sleep(POLL_SEC); continue

                # place entry
                if USE_MAKER:
                    limit_px = price - bps(price, MAKER_ENTRY_OFFSET_BPS)
                    print(f"[LIVE] BUY (maker) qty≈{qty:.6f} @ {limit_px:.6f} (last {price:.6f}) "
                          f"(free {QUOTE}={free_q:.2f}) SL {sl:.6f} TP {tp:.6f}")
                    try:
                        o = post_only_limit("buy", qty, limit_px)
                        oid = o.get("id") or o.get("order", {}).get("id")
                        filled = wait_fill_or_cancel(oid, MAKER_STALE_SEC)
                        if not filled:
                            print("[SKIP] entry maker stale/canceled; waiting next signal")
                            time.sleep(POLL_SEC); continue
                        of = ex.fetch_order(oid, SYMBOL)
                        entry = float(of.get("average") or of.get("price") or limit_px)
                        in_pos = True
                        equity_at_entry = fetch_total_equity_quote()
                        print(f"[FILL] entry≈{entry:.6f} qty≈{qty:.6f} id={oid}")
                    except ccxt.InvalidOrder as e:
                        print("[ERR ] maker InvalidOrder:", e); time.sleep(5); continue
                    except ccxt.InsufficientFunds as e:
                        print("[ERR ] maker InsufficientFunds:", e); time.sleep(8); continue
                else:
                    print(f"[LIVE] BUY (taker) qty≈{qty:.6f} @ ~{price:.6f} "
                          f"(free {QUOTE}={free_q:.2f}) SL {sl:.6f} TP {tp:.6f}")
                    try:
                        o = ex.create_order(SYMBOL, "market", "buy", qty)
                        entry = float(o.get("price") or price)
                        in_pos = True
                        equity_at_entry = fetch_total_equity_quote()
                        print(f"[FILL] entry≈{entry:.6f} qty≈{qty:.6f} id={o.get('id')}")
                    except ccxt.InsufficientFunds as e:
                        print("[ERR ] taker InsufficientFunds:", e); time.sleep(8); continue
                    except ccxt.InvalidOrder as e:
                        print("[ERR ] taker InvalidOrder:", e); time.sleep(5); continue

            # -------- EXIT --------
            if in_pos:
                price = float(df["close"].iloc[-1])
                exit_reason = None
                exit_px = None

                if price <= sl:
                    exit_reason = "SL"; exit_px = price
                elif price >= tp:
                    exit_reason = "TP"; exit_px = price

                if exit_reason:
                    try:
                        if exit_reason == "TP" and USE_MAKER and MAKER_TP_POST:
                            tp_px = tp
                            print(f"[LIVE] SELL TP (maker) qty={qty:.6f} @ {tp_px:.6f}")
                            o = post_only_limit("sell", qty, tp_px)
                            oid = o.get("id") or o.get("order", {}).get("id")
                            filled = wait_fill_or_cancel(oid, MAKER_STALE_SEC)
                            if not filled:
                                print("[FALLBACK] TP maker not filled; selling taker")
                                ex.create_order(SYMBOL, "market", "sell", qty)
                        else:
                            ex.create_order(SYMBOL, "market", "sell", qty)
                    except ccxt.RateLimitExceeded as e:
                        print("[NET ] rate limited on sell:", e); time.sleep(10)
                    except Exception as e:
                        print("[ERR ] sell failed:", e); time.sleep(5)

                    cur_equity = fetch_total_equity_quote()
                    pnl = 0.0
                    if equity_at_entry is not None:
                        pnl = cur_equity - equity_at_entry  # realized incl. fees

                    in_pos = False
                    equity_at_entry = None
                    with open(TRADES_CSV, "a", newline="") as f:
                        csv.writer(f).writerow([
                            now_utc_iso(), SYMBOL, "LONG",
                            f"{entry:.6f}", f"{exit_px:.6f}",
                            f"{pnl:.4f}", f"{cur_equity:.4f}", exit_reason
                        ])
                    print(f"[LIVE] {exit_reason} @ {exit_px:.6f} | pnl={pnl:.4f} {QUOTE} | equity={cur_equity:.4f} {QUOTE}")

            time.sleep(POLL_SEC)

        except ccxt.RateLimitExceeded as e:
            print("[NET ] sleeping:", e); time.sleep(10)
        except ccxt.NetworkError as e:
            print("[NET ] network issue:", e); time.sleep(8)
        except Exception as e:
            print("[ERR ] loop:", e); time.sleep(10)

if __name__ == "__main__":
    run()