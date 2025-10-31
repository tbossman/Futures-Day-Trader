# test_maker_probe.py
"""
Lightweight probe to test if maker orders are being accepted/filled by the exchange.
It imports your `live3.py` strategy module and uses its exchange client, symbol,
rounding helpers, and maker_limit() wrapper.

What it does
------------
- Places N maker *buy* limit orders, each sized to ~$PER_ORDER_USD of quote value.
- Prices each order slightly below the current best bid (post-only maker).
- Polls for up to WAIT_SEC to see if any orders fill.
- Prints a concise summary and (optionally) cancels any remaining probe orders.

Safety
------
- Set DRY_RUN=true to simulate prices/amounts *without* creating real orders.
- Set CANCEL_AFTER=true to cancel any open probe orders at the end of the run.
- All behavior controlled via environment variables below or CLI flags.

Usage
-----
$ export DRY_RUN=false
$ export CANCEL_AFTER=true
$ python3 test_maker_probe.py --n 5 --usd 10 --wait 45

Requirements
------------
- Your environment must have `ccxt` installed and credentials configured exactly
  as expected by live3.py, since we import that module to reuse `ex`, `SYMBOL`, etc.

"""

import os
import time
import math
import argparse
from datetime import datetime, timezone

# Import user's live module
import importlib.util
import pathlib
live_path = pathlib.Path(__file__).with_name("live3.py")
spec = importlib.util.spec_from_file_location("live3", live_path)
live3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live3)

ex       = live3.ex
SYMBOL   = live3.SYMBOL
QUOTE    = getattr(live3, "QUOTE", "USDT")
PRICE_IMPROVE_PCT = getattr(live3, "PRICE_IMPROVE_PCT", 0.0002)  # 2 bps default

# Helpers from live3, with fallbacks if user renamed them
best_bid_ask = getattr(live3, "best_bid_ask")
maker_limit  = getattr(live3, "maker_limit")
cancel_all   = getattr(live3, "cancel_all", None)
round_amt    = getattr(live3, "round_amt", lambda x: float(x))
round_price  = getattr(live3, "round_price", lambda x: float(x))

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def place_probe_maker_buys(n:int, per_order_usd:float, wait_sec:int, ladder_bps:float=1.0):
    """
    Places N post-only maker *buy* orders at prices slightly below best bid.
    Ladder down by `ladder_bps` per order to avoid crossing.
    Returns (order_ids, filled_events)
    """
    dry_run = os.getenv("DRY_RUN","false").lower()=="true"
    order_ids = []
    placed = 0

    bid, ask = best_bid_ask()
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad market data for {SYMBOL}: bid={bid} ask={ask}")

    # Base maker price: a tiny improvement under best bid
    base_px = bid * (1.0 - PRICE_IMPROVE_PCT)
    # Convert bps ladder to absolute price increments
    ladder_step = bid * (ladder_bps/10000.0)

    print(f"[PROBE] {SYMBOL} bid={bid:.8f} ask={ask:.8f} | base maker px ~{base_px:.8f}")

    for i in range(n):
        px = base_px - i*ladder_step
        px = round_price(px)
        if px <= 0:
            print(f"[WARN] bad price ({px}); skipping order {i+1}/{n}")
            continue
        amt = round_amt(per_order_usd / px)
        if amt <= 0:
            print(f"[WARN] Skipping order {i+1}/{n}: computed amount <= 0 at px={px}")
            continue

        if dry_run:
            print(f"[DRY] would place BUY maker {SYMBOL} amt={amt} px={px}")
            placed += 1
            continue

        try:
            o = maker_limit("buy", amt, px, reduce_only=False)
            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            order_ids.append(oid)
            placed += 1
            print(f"[LIVE] placed BUY maker {i+1}/{n}: id={oid} amt={amt} px={px}")
            time.sleep(0.25)  # gentle pacing
        except Exception as e:
            print(f"[ERR] creating order {i+1}/{n}: {e}")

    # If dry run, skip polling
    if os.getenv("DRY_RUN","false").lower()=="true":
        return order_ids, []

    # Poll for fills
    print(f"[PROBE] polling for fills up to {wait_sec}s…")
    t0 = time.time()
    filled = []
    seen_trade_ids = set()
    since_ms = now_ms() - 5*60*1000  # last 5 minutes window

    while time.time() - t0 < wait_sec:
        try:
            trades = ex.fetch_my_trades(SYMBOL, since=since_ms)
            for tr in trades:
                tid = tr.get("id")
                if tid in seen_trade_ids:
                    continue
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

        # Also check order statuses for filled/partial
        try:
            opens = ex.fetch_open_orders(SYMBOL)
            print(f"[DBG] open_orders={len(opens)}")
        except Exception as e:
            print("[WARN] fetch_open_orders failed:", e)

        time.sleep(2.0)

    return order_ids, filled

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=int(os.getenv("N","5")), help="number of probe maker buys")
    ap.add_argument("--usd", type=float, default=float(os.getenv("PER_ORDER_USD","10")), help="USD (quote) size per order")
    ap.add_argument("--wait", type=int, default=int(os.getenv("WAIT_SEC","45")), help="seconds to poll for fills")
    ap.add_argument("--cancel-after", action="store_true", help="cancel remaining open probe orders at the end")
    args = ap.parse_args()

    order_ids, filled = place_probe_maker_buys(args.n, args.usd, args.wait)

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
        print(f"VWAP:             {vwap:.8f}")
    print("=====================\n")

    if args.cancel_after and order_ids and os.getenv("DRY_RUN","false").lower()!="true":
        if callable(cancel_all):
            try:
                print("[CLEANUP] cancel_all()…")
                cancel_all()
            except Exception as e:
                print("[CLEANUP WARN] cancel_all failed:", e)
        else:
            # Fallback: cancel by ids we created
            print("[CLEANUP] cancel created orders individually…")
            for oid in order_ids:
                if not oid: continue
                try:
                    ex.cancel_order(oid, SYMBOL)
                except Exception as e:
                    print(f"[CANCEL WARN] {oid}: {e}")

if __name__ == "__main__":
    main()
