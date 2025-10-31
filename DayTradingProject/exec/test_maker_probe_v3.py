# test_maker_probe_v3.py
"""
Maker order probe (v3) with:
- --live flag to override DRY_RUN and place real orders
- symbol override (--symbol)
- post-only retry: if exchange rejects as taker, nudge price down by one tick and retry
- explicit logging of resolved DRY_RUN, min_cost/min_amt, steps, and computed per-order amt/px

Usage examples:
  python3 test_maker_probe_v3.py --n 5 --usd 10 --wait 45 --live --cancel-after
  python3 test_maker_probe_v3.py --symbol DOGE/USD --n 3 --usd 50 --live
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
DEFAULT_SYMBOL = live3.SYMBOL
QUOTE    = getattr(live3, "QUOTE", "USDT")
PRICE_IMPROVE_PCT = float(getattr(live3, "PRICE_IMPROVE_PCT", 0.0002))

best_bid_ask = getattr(live3, "best_bid_ask")
maker_limit  = getattr(live3, "maker_limit")
cancel_all   = getattr(live3, "cancel_all", None)

def _fallback_round_amt(x): return float(f"{float(x):.8f}")
def _fallback_round_price(x): return float(f"{float(x):.8f}")
round_amt   = getattr(live3, "round_amt", _fallback_round_amt)
round_price = getattr(live3, "round_price", _fallback_round_price)

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def load_market_info(sym):
    ex.load_markets()
    m = ex.market(sym)
    prec = m.get("precision") or {}
    limits = m.get("limits") or {}
    info = m.get("info") or {}

    prec_price = prec.get("price")
    prec_amount = prec.get("amount")
    price_step = 10.0 ** (-prec_price) if isinstance(prec_price, (int, float)) else None
    amt_step = 10.0 ** (-prec_amount) if isinstance(prec_amount, (int, float)) else None

    min_price = (limits.get("price") or {}).get("min")
    min_amt = (limits.get("amount") or {}).get("min")
    min_cost = (limits.get("cost") or {}).get("min")

    tick_size = None
    for k in ("tickSize","tick_size","price_tick"):
        v = info.get(k)
        if v:
            try: tick_size = float(v)
            except: pass
    if tick_size and (not price_step or tick_size > 0):
        price_step = tick_size

    return {
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
    px = p
    if mi["price_step"]:
        px = step_floor(px, mi["price_step"])
    else:
        px = round_price(px)
    if mi["min_price"] and px < mi["min_price"]:
        px = mi["min_price"]
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
    if mi["min_cost"] and px * amt < mi["min_cost"]:
        needed = mi["min_cost"] / px
        if mi["amt_step"]:
            needed = step_floor(needed + mi["amt_step"], mi["amt_step"])
        amt = max(amt, needed)
    return float(amt)

def place_probe_maker_buys(symbol:str, n:int, per_order_usd:float, wait_sec:int, ladder_ticks:int=2, live:bool=False):
    ex.load_markets()
    mi = load_market_info(symbol)

    bid, ask = best_bid_ask()
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad market data for {symbol}: bid={bid} ask={ask}")

    base_px = bid * (1.0 - PRICE_IMPROVE_PCT)
    ladder_step = mi["price_step"] if (mi["price_step"] and mi["price_step"] > 0) else bid * (1.0/10000.0)

    print(f"[PROBE] {symbol} bid={bid:.10f} ask={ask:.10f} | base maker px ~{base_px:.10f}")
    print(f"[INFO] steps: price_step={mi['price_step']} amt_step={mi['amt_step']} min_price={mi['min_price']} min_amt={mi['min_amt']} min_cost={mi['min_cost']}")
    print(f"[INFO] resolved DRY_RUN={'false' if live else os.getenv('DRY_RUN','false')} (use --live to force live orders)")

    order_ids = []
    placed = 0
    for i in range(n):
        raw_px = base_px - i * ladder_ticks * ladder_step
        px = safe_round_price(raw_px, mi)
        amt = per_order_usd / max(px, 1e-12)
        amt = safe_round_amount(amt, px, mi)
        print(f"[DBG] order {i+1}: target px={raw_px:.12f} -> px={px}; amt={amt} (target USD={per_order_usd})")

        if not live:
            print(f"[DRY] would place BUY maker {symbol} amt={amt} px={px}")
            continue

        # Try place with post-only; if rejected as taker, nudge down by one tick and retry up to 3x
        retries = 3
        while True:
            try:
                o = maker_limit("buy", amt, px, reduce_only=False)
                oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                order_ids.append(oid)
                placed += 1
                print(f"[LIVE] placed BUY maker {i+1}/{n}: id={oid} amt={amt} px={px}")
                time.sleep(0.25)
                break
            except Exception as e:
                msg = str(e).lower()
                if retries > 0 and ("post only" in msg or "taker" in msg or "would be taker" in msg):
                    px = safe_round_price(px - ladder_step, mi)
                    retries -= 1
                    print(f"[RETRY] post-only reject; nudging px down -> {px} (retries left {retries})")
                    continue
                print(f"[ERR] creating order {i+1}/{n}: {e}")
                break

    if not live:
        return order_ids, []

    print(f"[PROBE] polling for fills up to {wait_sec}s…")
    t0 = time.time()
    filled = []
    seen_trade_ids = set()
    since_ms = now_ms() - 5*60*1000

    while time.time() - t0 < wait_sec:
        try:
            trades = ex.fetch_my_trades(symbol, since=since_ms)
            for tr in trades:
                tid = tr.get("id")
                if tid in seen_trade_ids: continue
                seen_trade_ids.add(tid)
                if tr.get("side") == "buy" and tr.get("symbol") == symbol:
                    filled.append({
                        "trade_id": tid,
                        "price": float(tr.get("price", 0)),
                        "amount": float(tr.get("amount", 0)),
                        "cost": float(tr.get("cost", 0)),
                        "timestamp": tr.get("timestamp"),
                    })
            if filled: break
        except Exception as e:
            print("[WARN] fetch_my_trades failed:", e)

        try:
            opens = ex.fetch_open_orders(symbol)
            print(f"[DBG] open_orders={len(opens)}")
        except Exception as e:
            print("[WARN] fetch_open_orders failed:", e)

        time.sleep(2.0)

    return order_ids, filled

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", type=str, default=os.getenv("SYMBOL_OVERRIDE","").strip())
    ap.add_argument("--n", type=int, default=int(os.getenv("N","5")))
    ap.add_argument("--usd", type=float, default=float(os.getenv("PER_ORDER_USD","10")))
    ap.add_argument("--wait", type=int, default=int(os.getenv("WAIT_SEC","45")))
    ap.add_argument("--ladder-ticks", type=int, default=2, help="tick steps between each probe order")
    ap.add_argument("--live", action="store_true", help="force live orders (overrides DRY_RUN env)")
    ap.add_argument("--cancel-after", action="store_true")
    args = ap.parse_args()

    symbol = args.symbol or DEFAULT_SYMBOL
    order_ids, filled = place_probe_maker_buys(symbol, args.n, args.usd, args.wait, args.ladder_ticks, live=args.live)

    print("\n=== PROBE SUMMARY ===")
    print(f"Symbol: {symbol}")
    print(f"Orders attempted: {args.n}")
    print(f"Orders placed:    {len(order_ids)} (live={args.live})")
    print(f"Fills observed:   {len(filled)}")
    if filled:
        total_cost = sum(f['cost'] for f in filled)
        total_amt  = sum(f['amount'] for f in filled)
        vwap = total_cost/total_amt if total_amt>0 else 0.0
        print(f"Filled amount:    {total_amt:.8f}")
        print(f"Filled cost:      {total_cost:.6f} {QUOTE}")
        print(f"VWAP:             {vwap:.10f}")
    print("=====================\n")

    if args.cancel-after and order_ids and args.live:
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
                    ex.cancel_order(oid, symbol)
                except Exception as e:
                    print(f"[CANCEL WARN] {oid}: {e}")

if __name__ == "__main__":
    main()
