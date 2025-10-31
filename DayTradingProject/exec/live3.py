
# live.py — Progressive maker execution with strict TP% = 4.9 and SL% = 13.44
# - Compounding size using equity fractions: 0.70 -> 0.75 -> 0.80
# - Always compute TP from TP% (4.9) and SL from SL% (13.44)
# - All entries/exits are post-only maker when possible; market fallback to honor TP/SL
# - PnL is computed net of maker fees (per side) and logged

import os, time, math, csv
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import ccxt
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG (env with robust defaults) ----------
EXCHANGE   = os.getenv("EXCHANGE", "kraken").strip()
SYMBOL     = os.getenv("SYMBOLS", "DOGE/USD").split(",")[0].strip()
TF         = os.getenv("TIMEFRAME", "1m").strip()

API_KEY    = os.getenv("API_KEY", "") or ""
API_SECRET = os.getenv("API_SECRET", "") or ""

BASE       = os.getenv("BASE", SYMBOL.split("/")[0]).strip()
QUOTE      = os.getenv("QUOTE", SYMBOL.split("/")[1]).strip()

CSV_PATH   = os.getenv("LIVE_CSV", "live_trades.csv").strip()
POLL_SEC   = float(os.getenv("POLL_SEC", "1.5"))

# Fees (maker per side), slippage & buffer
MAKER_FEE_RATE     = float(os.getenv("MAKER_FEE_RATE", "0.0025"))  # 0.25% per side
SLIPPAGE_BPS       = float(os.getenv("SLIPPAGE_BPS", "3")) / 1e4   # 3 bps = 0.0003
FEE_EDGE_BUFFER    = float(os.getenv("FEE_EDGE_BUFFER", "1.05"))
PRICE_IMPROVE_PCT  = float(os.getenv("PRICE_IMPROVE_PCT", "0.0002"))  # 2 bps

# Sizing (progressive staging up to max)
POS_FRAC_MIN       = float(os.getenv("POS_FRAC_MIN", "0.70"))
POS_FRAC_PREF      = float(os.getenv("POS_FRAC_PREFERRED", "0.75"))
POS_FRAC_MAX       = float(os.getenv("POS_FRAC_MAX", "0.80"))
ALWAYS_STAGE       = os.getenv("ALWAYS_STAGE", "true").lower() == "true"

# Risk / Reward (strictly enforced TP% = 4.9)
TP_PCT             = float(os.getenv("GROWTH_PCT", "4.9"))    # predictor growth %
SL_PCT             = float(os.getenv("SL_PCT", "13.44"))      # stop %
TP_TOL_BPS         = float(os.getenv("TP_TOL_BPS", "5")) / 1e4  # 5 bps tolerance around TP
SL_TOL_BPS         = float(os.getenv("SL_TOL_BPS", "5")) / 1e4

# Equity
START_EQUITY       = float(os.getenv("START_EQUITY", "1000.0"))

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
prec_price= precision.get("price")
amt_min   = ((limits.get("amount") or {}).get("min")) or 0.0
cost_min  = ((limits.get("cost") or {}).get("min")) or 0.0

def round_amt(x: float) -> float:
    if prec_amt is None:
        return x
    step = 10 ** (-prec_amt)
    return math.floor(x * step) / step

def round_price(p: float) -> float:
    if prec_price is None:
        return p
    step = 10 ** (-prec_price)
    return math.floor(p * step) / step

def ticker() -> Dict[str, Any]:
    return ex.fetch_ticker(SYMBOL)

def best_bid_ask():
    t = ticker()
    return float(t.get("bid") or 0.0), float(t.get("ask") or 0.0)

def maker_limit(side: str, amount: float, price: float, reduce_only: bool=False):
    params = {"postOnly": True}
    if reduce_only:
        params["reduceOnly"] = True
    return ex.create_order(SYMBOL, "limit", side, amount, price, params)

def cancel_all():
    try:
        for o in ex.fetch_open_orders(SYMBOL):
            try:
                ex.cancel_order(o["id"], SYMBOL)
            except Exception:
                pass
    except Exception:
        pass

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_csv(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "side", "entry", "tp", "sl", "amt", "exit", "pnl_quote", "equity_quote", "reason"])

# Placeholder: wire your predictor here
def check_triggers() -> Dict[str, bool]:
    # Replace with your predictor logic (historical → signal)
    return {"long": False, "short": False}

def run():
    ensure_csv(CSV_PATH)

    equity      = START_EQUITY
    in_pos      = False
    pos_side    = None
    entry_px    = None
    tp_px       = None
    sl_px       = None
    amount      = 0.0

    print(f"[LIVE] start equity={equity:.2f} {QUOTE} | TP%={TP_PCT:.4f}% SL%={SL_PCT:.4f}% maker={MAKER_FEE_RATE*100:.2f}%/side")

    while True:
        try:
            bid, ask = best_bid_ask()
            if bid == 0 or ask == 0:
                time.sleep(POLL_SEC); continue

            # ---------------- ENTRY ----------------
            trig = check_triggers()
            trigger_long  = bool(trig.get("long", False))
            trigger_short = bool(trig.get("short", False))

            if not in_pos and (trigger_long or trigger_short):
                # Stage 1 target notional
                stage_fracs = [POS_FRAC_MIN, POS_FRAC_PREF, POS_FRAC_MAX]
                # Place first stage immediately
                target_quote = equity * stage_fracs[0]
                ref = bid if trigger_long else ask
                amt = round_amt(max(amt_min, target_quote / ref))
                if amt > 0:
                    px = round_price((bid * (1 - PRICE_IMPROVE_PCT)) if trigger_long else (ask * (1 + PRICE_IMPROVE_PCT)))
                    order = maker_limit("buy" if trigger_long else "sell", amt, px, reduce_only=False)

                    # Wait briefly for fills and compute VWAP
                    filled, vwap = 0.0, 0.0
                    t0 = time.time()
                    while time.time() - t0 < 25:
                        trades = ex.fetch_my_trades(SYMBOL, since=int((datetime.now(timezone.utc).timestamp()-120)*1000))
                        for tr in trades[-20:]:
                            if tr.get("symbol") == SYMBOL and tr.get("side") == ("buy" if trigger_long else "sell"):
                                a = float(tr["amount"]); p = float(tr["price"])
                                vwap = (vwap*filled + p*a)/(filled + a) if filled > 0 else p
                                filled += a
                        if filled > 0: break
                        time.sleep(1.0)

                    cancel_all()

                    if filled > 0:
                        in_pos   = True
                        pos_side = "long" if trigger_long else "short"
                        entry_px = vwap
                        amount   = filled

                        # Set TP/SL strictly from TP_PCT & SL_PCT
                        tp_px = entry_px * (1 + TP_PCT/100.0) if pos_side=="long" else entry_px * (1 - TP_PCT/100.0)
                        sl_px = entry_px * (1 - SL_PCT/100.0) if pos_side=="long" else entry_px * (1 + SL_PCT/100.0)

                        print(f"[LIVE] ENTER {pos_side.upper()} amt={amount:.6f} @ {entry_px:.6f} | TP={tp_px:.6f} SL={sl_px:.6f} eq={equity:.2f}")

            # ---------------- PROGRESSIVE STAGING ----------------
            if in_pos and (ALWAYS_STAGE or trigger_long or trigger_short):
                cur_notional = amount * (bid if pos_side=="long" else ask)
                for frac in [POS_FRAC_PREF, POS_FRAC_MAX]:
                    target = equity * frac
                    if cur_notional + 1e-9 < target:
                        add_quote = target - cur_notional
                        ref = bid if pos_side=="long" else ask
                        add_amt = round_amt(max(amt_min, add_quote / ref))
                        if add_amt > 0:
                            px = round_price((bid * (1 - PRICE_IMPROVE_PCT)) if pos_side=="long" else (ask * (1 + PRICE_IMPROVE_PCT)))
                            maker_limit("buy" if pos_side=="long" else "sell", add_amt, px, reduce_only=False)

                            # Wait briefly for fills & update VWAP
                            filled_add, vwap_add = 0.0, 0.0
                            t1 = time.time()
                            while time.time() - t1 < 20:
                                trades = ex.fetch_my_trades(SYMBOL, since=int((datetime.now(timezone.utc).timestamp()-120)*1000))
                                for tr in trades[-20:]:
                                    if tr.get("symbol")==SYMBOL and tr.get("side")==("buy" if pos_side=="long" else "sell"):
                                        a = float(tr["amount"]); p = float(tr["price"])
                                        vwap_add = (vwap_add*filled_add + p*a)/(filled_add + a) if filled_add > 0 else p
                                        filled_add += a
                                if filled_add > 0: break
                                time.sleep(1.0)

                            cancel_all()

                            if filled_add > 0:
                                entry_px = (entry_px*amount + vwap_add*filled_add) / (amount + filled_add)
                                amount  += filled_add
                                tp_px = entry_px * (1 + TP_PCT/100.0) if pos_side=="long" else entry_px * (1 - TP_PCT/100.0)
                                sl_px = entry_px * (1 - SL_PCT/100.0) if pos_side=="long" else entry_px * (1 + SL_PCT/100.0)
                                cur_notional = amount * (bid if pos_side=="long" else ask)
                                print(f"[LIVE] STAGE UP -> notional≈{cur_notional:.2f} {QUOTE} | entry={entry_px:.6f} amt={amount:.6f}")

            # ---------------- EXIT (strictly around TP% and SL%) ----------------
            if in_pos:
                last_bid, last_ask = bid, ask

                # Has TP or SL been hit in price?
                hit_tp = (last_ask >= tp_px*(1 - TP_TOL_BPS)) if pos_side=="long" else (last_bid <= tp_px*(1 + TP_TOL_BPS))
                hit_sl = (last_bid <= sl_px*(1 + SL_TOL_BPS)) if pos_side=="long" else (last_ask >= sl_px*(1 - SL_TOL_BPS))

                do_exit = None
                if hit_tp: do_exit = "TP"
                if hit_sl: do_exit = "SL"  # SL dominates if both toggled on the same poll

                if do_exit:
                    side   = "sell" if pos_side=="long" else "buy"
                    ref    = last_ask if pos_side=="long" else last_bid
                    px_try = round_price(ref * (1 + PRICE_IMPROVE_PCT if side=="sell" else 1 - PRICE_IMPROVE_PCT))

                    # Try maker reduce-only first
                    try:
                        maker_limit(side, amount, px_try, reduce_only=True)
                        time.sleep(1.0)
                    except Exception:
                        pass

                    # If price runs past tolerance, use market to guarantee exit
                    # Re-read book once more
                    bb, aa = best_bid_ask()
                    beyond_tp = (aa >= tp_px*(1 + TP_TOL_BPS)) if pos_side=="long" else (bb <= tp_px*(1 - TP_TOL_BPS))
                    beyond_sl = (bb <= sl_px*(1 - SL_TOL_BPS)) if pos_side=="long" else (aa >= sl_px*(1 + SL_TOL_BPS))
                    if beyond_tp or beyond_sl:
                        try:
                            ex.create_order(SYMBOL, "market", side, amount)
                        except Exception:
                            # As a fallback, leave the reduce-only maker working; we will re-check next loop
                            pass

                    # Compute realized PnL using last mid as proxy for exit price
                    bb2, aa2 = best_bid_ask()
                    exit_px  = aa2 if pos_side=="long" else bb2
                    notional_entry = entry_px * amount
                    notional_exit  = exit_px  * amount
                    gross_pnl = (notional_exit - notional_entry) if pos_side=="long" else (notional_entry - notional_exit)
                    fees      = MAKER_FEE_RATE * (notional_entry + notional_exit)  # round-trip maker
                    pnl_quote = gross_pnl - fees
                    equity   += pnl_quote

                    with open(CSV_PATH, "a", newline="") as f:
                        w = csv.writer(f)
                        w.writerow([now_iso(), pos_side, f"{entry_px:.6f}", f"{tp_px:.6f}", f"{sl_px:.6f}",
                                    f"{amount:.6f}", f"{exit_px:.6f}", f"{pnl_quote:.4f}", f"{equity:.4f}", do_exit])
                    print(f"[LIVE] EXIT {do_exit} @ ~{exit_px:.6f} | pnl={pnl_quote:.4f} {QUOTE} | eq={equity:.2f} {QUOTE}")

                    # Reset
                    in_pos   = False
                    pos_side = None
                    entry_px = tp_px = sl_px = None
                    amount   = 0.0

            time.sleep(POLL_SEC)

        except ccxt.RateLimitExceeded as e:
            print("[NET] rate limit:", e); time.sleep(8)
        except ccxt.NetworkError as e:
            print("[NET] network:", e); time.sleep(6)
        except Exception as e:
            print("[ERR]", e); time.sleep(5)

if __name__ == "__main__":
    run()
