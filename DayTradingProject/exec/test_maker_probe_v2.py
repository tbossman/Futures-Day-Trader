# test_maker_probe_v2.py
"""
Robust maker BUY probe that respects exchange precision and limits.
- Reuses your live3.py exchange client and helpers.
- Derives price/amount tick sizes and minimums from `ex.market(SYMBOL)`.
- Prevents price rounding to 0 by clamping to min tick/min price.
- Ensures amount satisfies min amount and/or min cost if specified.
"""

import os, time, math, argparse
from datetime import datetime, timezone
import importlib.util, pathlib

# --- Import user's live module to reuse client & helpers ---
live_path = pathlib.Path(__file__).with_name("live3.py")
spec = importlib.util.spec_from_file_location("live3", live_path)
live3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live3)

ex       = live3.ex
SYMBOL   = live3.SYMBOL
QUOTE    = getattr(live3, "QUOTE", "USDT")
PRICE_IMPROVE_PCT = float(getattr(live3, "PRICE_IMPROVE_PCT", 0.0002))

best_bid_ask = getattr(live3, "best_bid_ask")
maker_limit  = getattr(live3, "maker_limit")
cancel_all   = getattr(live3, "cancel_all", None)

# Fallbacks if user's helpers are missing
def _fallback_round_amt(x): return float(f"{float(x):.8f}")
def _fallback_round_price(x): return float(f"{float(x):.8f}")
round_amt   = getattr(live3, "round_amt", _fallback_round_amt)
round_price = getattr(live3, "round_price", _fallback_round_price)

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def load_market_info(sym):
    ex.load_markets()
    m = ex.market(sym)
    # Precision may appear as decimals (precision.price/amount) OR explicit steps in limits.*.min
    prec_price = None
    prec_amount = None
    price_step = None
    amt_step = None
    min_price = None
    min_amt = None
    min_cost = None

    prec = m.get("precision") or {}
    limits = m.get("limits") or {}
    info = m.get("info") or {}

    # Precision in decimals -> convert to step = 10^-dec
    if isinstance(prec.get("price"), (int, float)):
        prec_price = int(prec["price"])
        price_step = 10.0 ** (-prec_price)
    if isinstance(prec.get("amount"), (int, float)):
        prec_amount = int(prec["amount"])
        amt_step = 10.0 ** (-prec_amount)

    # Kraken often exposes minimums here
    if isinstance(limits.get("price") or {}, dict):
        min_price = limits["price"].get("min") or min_price
    if isinstance(limits.get("amount") or {}, dict):
        min_amt = limits["amount"].get("min") or min_amt
    if isinstance(limits.get("cost") or {}, dict):
        min_cost = limits["cost"].get("min") or min_cost

    # Some exchanges use tickSize
    tick_size = None
    for k in ("tickSize", "tick_size", "price_tick"):
        v = info.get(k)
        if v:
            try:
                tick_size = float(v)
            except Exception:
                pass
    if tick_size and (not price_step or tick_size > 0):
        price_step = tick_size

    return {
        "prec_price": prec_price,
        "prec_amount": prec_amount,
        "price_step": price_step,
        "amt_step": amt_step,
        "min_price": min_price,
        "min_amt": min_amt,
        "min_cost": min_cost,
    }

def step_floor(x: float, step: float) -> float:
    if step and step > 0:
        return math.floor(x / step) * step
    return x

def safe_round_price(p: float, mi):
    # Use step if available; else fallback to live3.round_price; clamp to min_price
    px = p
    if mi["price_step"]:
        px = step_floor(px, mi["price_step"])
    else:
        px = round_price(px)
    if mi["min_price"] and px < mi["min_price"]:
        px = mi["min_price"]
    # As absolute last resort, guard against zero
    if px <= 0:
        px = max(mi["min_price"] or 1e-12, 1e-12)
    return float(px)

def safe_round_amount(a: float, px: float, mi):
    amt = a
    if mi["amt_step"]:
        amt = step_floor(amt, mi["amt_step"])
    else:
        amt = round_amt(amt)

    if mi["min_amt"] and amt < mi["min_amt"]:
        amt = mi["min_amt"]

    # Respect min cost if present
    if mi["min_cost"] and px * amt < mi["min_cost"]:
        # raise amt to satisfy min cost on the given px
        needed = mi["min_cost"] / px
        if mi["amt_step"]:
            needed = step_floor(needed + mi["amt_step"], mi["amt_step"])
        amt = max(amt, needed)

    return float(amt)

def place_probe_maker_buys(n:int, per_order_usd:float, wait_sec:int, ladder_ticks:int=2):
    dry_run = os.getenv("DRY_RUN","false").lower()=="true"
    ex.load_markets()
    mi = load_market_info(SYMBOL)

    bid, ask = best_bid_ask()
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad market data for {SYMBOL}: bid={bid} ask={ask}")

    # Base maker price slightly below bid
    base_px = bid * (1.0 - PRICE_IMPROVE_PCT)
    # Ladder by explicit ticks if available; otherwise bps equivalent
    if mi["price_step"] and mi["price_step"] > 0:
        ladder_step = mi["price_step"]
    else:
        ladder_step = bid * (1.0/10000.0)  # 1 bp fallback

    print(f"[PROBE] {SYMBOL} bid={bid:.10f} ask={ask:.10f} | base maker px ~{base_px:.10f}")
    print(f"[INFO] market: step_price={mi['price_step']} min_price={mi['min_price']} step_amt={mi['amt_step']} min_amt={mi['min_amt']} min_cost={mi['min_cost']}")

    order_ids = []
    for i in range(n):
        raw_px = base_px - i * ladder_ticks * ladder_step
        px = safe_round_price(raw_px, mi)
        if px <= 0:
            print(f"[WARN] bad price ({px}); skipping order {i+1}/{n}")
            continue

        amt = per_order_usd / px
        amt = safe_round_amount(amt, px, mi)

        # Final sanity checks
        if amt <= 0 or px <= 0:
            print(f"[WARN] invalid amt/px after rounding (amt={amt}, px={px}); skip {i+1}/{n}")
            continue

        if dry_run:
            print(f"[DRY] would place BUY maker {SYMBOL} amt={amt} px={px}")
            continue

        try:
            o = maker_limit("buy", amt, px, reduce_only=False)
            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            order_ids.append(oid)
            print(f"[LIVE] placed BUY maker {i+1}/{n}: id={oid} amt={amt} px={px}")
            time.sleep(0.25)
        except Exception as e:
            print(f"[ERR] creating order {i+1}/{n}: {e}")

    # Poll for fills
    if os.getenv("DRY_RUN","false").lower()=="true":
        return order_ids, []

    print(f"[PROBE] polling for fills up to {wait_sec}s…")
    t0 = time.time()
    filled = []
    seen_trade_ids = set()
    since_ms = now_ms() - 5*60*1000

    while time.time() - t0 < wait_sec:
        try:
            trades = ex.fetch_my_trades(SYMBOL, since=since_ms)
            for tr in trades:
                tid = tr.get("id")
                if tid in seen_trade_ids: continue
                seen_trade_ids.add(tid)
                if tr.get("side") == "buy":
                    filled.append({
                        "trade_id": tid,
                        "price": float(tr.get("price", 0)),
                        "amount": float(tr.get("amount", 0)),
                        "cost": float(tr.get("cost", 0)),
                        "timestamp": tr.get("timestamp"),
                    })
            if filled:
                break
        except Exception as e:
            print("[WARN] fetch_my_trades failed:", e)

        try:
            opens = ex.fetch_open_orders(SYMBOL)
            print(f"[DBG] open_orders={len(opens)}")
        except Exception as e:
            print("[WARN] fetch_open_orders failed:", e)

        time.sleep(2.0)

    return order_ids, filled

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=int(os.getenv("N","5")))
    ap.add_argument("--usd", type=float, default=float(os.getenv("PER_ORDER_USD","10")))
    ap.add_argument("--wait", type=int, default=int(os.getenv("WAIT_SEC","45")))
    ap.add_argument("--ladder-ticks", type=int, default=2, help="tick steps between each probe order")
    ap.add_argument("--cancel-after", action="store_true")
    args = ap.parse_args()

    order_ids, filled = place_probe_maker_buys(args.n, args.usd, args.wait, args.ladder_ticks)

    print("\n=== PROBE SUMMARY ===")
    print(f"Symbol: {SYMBOL}")
    print(f"Orders attempted: {args.n}")
    print(f"Orders placed:    {len(order_ids)} (dry_run={os.getenv('DRY_RUN','false')})")
    print(f"Fills observed:   {len(filled)}")
    if filled:
        total_cost = sum(f['cost'] for f in filled)
        total_amt  = sum(f['amount'] for f in filled)
        vwap = total_cost/total_amt if total_amt>0 else 0.0
        print(f"Filled amount:    {total_amt:.8f}")
        print(f"Filled cost:      {total_cost:.4f} {QUOTE}")
        print(f"VWAP:             {vwap:.10f}")
    print("=====================\n")

    if args.cancel_after and order_ids and os.getenv("DRY_RUN","false").lower()!="true":
        if callable(cancel_all):
            try:
                print("[CLEANUP] cancel_all()…")
                cancel_all()
            except Exception as e:
                print("[CLEANUP WARN] cancel_all failed:", e)
        else:
            for oid in order_ids:
                if not oid: continue
                try:
                    ex.cancel_order(oid, SYMBOL)
                except Exception as e:
                    print(f"[CANCEL WARN] {oid}: {e}")

if __name__ == "__main__":
    main()
