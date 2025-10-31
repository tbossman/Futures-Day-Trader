# test_maker_probe_v4.py
import os, time, math, argparse, random
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
PRICE_IMPROVE_PCT_DEFAULT = float(getattr(live3, "PRICE_IMPROVE_PCT", 0.0002))

best_bid_ask = getattr(live3, "best_bid_ask")
maker_limit  = getattr(live3, "maker_limit")
cancel_all   = getattr(live3, "cancel_all", None)

def _fallback_round_amt(x): return float(f"{float(x):.8f}")
def _fallback_round_price(x): return float(f"{float(x):.8f}")
round_amt   = getattr(live3, "round_amt", _fallback_round_amt)
round_price = getattr(live3, "round_price", _fallback_round_price)

def load_market_info(sym):
    ex.load_markets()
    m = ex.market(sym)
    prec = m.get("precision") or {}
    limits = m.get("limits") or {}
    info = m.get("info") or {}
    price_step = 10.0 ** (-(prec.get("price") or 8))
    amt_step   = 10.0 ** (-(prec.get("amount") or 8))
    min_price  = (limits.get("price") or {}).get("min")
    min_amt    = (limits.get("amount") or {}).get("min")
    min_cost   = (limits.get("cost")  or {}).get("min")
    tick_size = None
    for k in ("tickSize","tick_size","price_tick"):
        v = info.get(k)
        if v:
            try: tick_size = float(v)
            except: pass
    if tick_size and tick_size>0:
        price_step = tick_size
    return {"price_step": price_step, "amt_step": amt_step, "min_price": min_price, "min_amt": min_amt, "min_cost": min_cost}

def step_floor(x, step):
    return math.floor(x/step)*step if step and step>0 else x

def step_round(x, step):
    return round(x/step)*step if step and step>0 else x

def safe_round_price(p, mi):
    px = p
    px = step_floor(px, mi["price_step"])
    if mi["min_price"] and px < mi["min_price"]:
        px = mi["min_price"]
    if px <= 0:
        px = max(mi["min_price"] or 1e-12, 1e-12)
    return float(px)

def safe_round_amount(a, px, mi):
    amt = a
    amt = step_floor(amt, mi["amt_step"])
    if mi["min_amt"] and amt < mi["min_amt"]:
        amt = mi["min_amt"]
    if mi["min_cost"] and px*amt < mi["min_cost"]:
        need = mi["min_cost"]/px
        need = step_floor(need + (mi["amt_step"] or 0), mi["amt_step"] or 1e-8)
        amt = max(amt, need)
    return float(amt)

def now_ms(): return int(datetime.now(timezone.utc).timestamp()*1000)

def place_or_adjust_buy(symbol, amt, desired_px, mi, chase_sec=0):
    """Place a post-only maker buy at desired_px; optionally chase the bid for up to chase_sec seconds."""
    # initial place
    px = safe_round_price(desired_px, mi)
    # retry if post-only reject by stepping down 1 tick up to 3 times
    retries = 3
    oid = None
    while True:
        try:
            o = maker_limit("buy", amt, px, reduce_only=False)
            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            break
        except Exception as e:
            msg = str(e).lower()
            if retries>0 and ("post only" in msg or "would be taker" in msg or "taker" in msg):
                px = safe_round_price(px - mi["price_step"], mi)
                retries -= 1
                continue
            raise

    # optional chase loop: if best bid moves up and we are underbid, cancel/replace to sit on bid
    end_t = time.time() + max(0, chase_sec)
    last_px = px
    while time.time() < end_t:
        try:
            bid, ask = best_bid_ask()
            target_px = step_floor(bid, mi["price_step"])  # sit right at bid
            if target_px > last_px:
                # replace (cancel + place new)
                try:
                    ex.cancel_order(oid, symbol)
                except Exception:
                    pass
                new_px = safe_round_price(target_px, mi)
                try:
                    o = maker_limit("buy", amt, new_px, reduce_only=False)
                    oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                    last_px = new_px
                except Exception as e:
                    # if would-be-taker, nudge down 1 tick
                    if "taker" in str(e).lower():
                        new_px = safe_round_price(new_px - mi["price_step"], mi)
                        o = maker_limit("buy", amt, new_px, reduce_only=False)
                        oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                        last_px = new_px
        except Exception:
            pass
        time.sleep(1.0)

    return oid

def place_probe_maker_buys(symbol, n, target_usd, usd_spread, wait_sec, ladder_ticks, live, mode, chase_sec):
    ex.load_markets()
    mi = load_market_info(symbol)
    bid, ask = best_bid_ask()
    if bid<=0 or ask<=0:
        raise RuntimeError(f"Bad market data for {symbol}: bid={bid} ask={ask}")

    # compute base price per mode
    if mode == "atbid":
        base_px = step_floor(bid, mi["price_step"])
    elif mode == "underbid":
        base_px = step_floor(bid, mi["price_step"]) - mi["price_step"]  # 1 tick under bid
    else:
        # custom: bps under bid
        bps = float(mode)  # e.g., "2" = 2 bps
        base_px = bid * (1.0 - bps/10000.0)

    ladder_step = mi["price_step"] if (mi["price_step"] and mi["price_step"]>0) else bid*(1.0/10000.0)

    print(f"[PROBE] {symbol} bid={bid:.10f} ask={ask:.10f} | base_px={base_px:.10f} mode={mode}")
    print(f"[INFO] steps: price_step={mi['price_step']} amt_step={mi['amt_step']} min_price={mi['min_price']} min_amt={mi['min_amt']} min_cost={mi['min_cost']}")
    print(f"[INFO] live={live} | chase_sec={chase_sec} | usd_target={target_usd} spread=±{int(usd_spread*100)}%")

    order_ids = []
    for i in range(n):
        # randomize USD in [target*(1-spread), target*(1+spread)]
        usd = target_usd * (1.0 + random.uniform(-usd_spread, usd_spread))
        raw_px = base_px - i*ladder_ticks*ladder_step
        px = safe_round_price(raw_px, mi)
        amt = usd / max(px, 1e-12)
        amt = safe_round_amount(amt, px, mi)

        print(f"[DBG] order {i+1}: usd={usd:.2f} px_target={raw_px:.12f} -> px={px} amt={amt}")
        if not live:
            continue

        try:
            oid = place_or_adjust_buy(symbol, amt, px, mi, chase_sec=chase_sec)
            order_ids.append(oid)
            print(f"[LIVE] placed id={oid} amt={amt} px~{px}")
        except Exception as e:
            print(f"[ERR] order {i+1}: {e}")

    # Poll for fills
    if not live:
        return order_ids, []

    print(f"[PROBE] polling for fills up to {wait_sec}s…")
    t0 = time.time()
    filled = []
    seen_ids = set()
    since = now_ms() - 5*60*1000

    while time.time() - t0 < wait_sec:
        try:
            trades = ex.fetch_my_trades(symbol, since=since)
            for tr in trades:
                tid = tr.get("id")
                if tid in seen_ids: continue
                seen_ids.add(tid)
                if tr.get("side")=="buy" and tr.get("symbol")==symbol:
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
        time.sleep(2.0)

    return order_ids, filled

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", type=str, default=os.getenv("SYMBOL_OVERRIDE","").strip())
    ap.add_argument("--n", type=int, default=int(os.getenv("N","5")))
    ap.add_argument("--usd", type=float, default=float(os.getenv("PER_ORDER_USD","10")))
    ap.add_argument("--usd-spread", type=float, default=0.20, help="randomize per-order USD ±this fraction (e.g., 0.2 = ±20%)")
    ap.add_argument("--wait", type=int, default=int(os.getenv("WAIT_SEC","45")))
    ap.add_argument("--ladder-ticks", type=int, default=1)
    ap.add_argument("--mode", type=str, default="atbid", help="maker placement: atbid | underbid | <bps> (e.g., '2' bps under bid)")
    ap.add_argument("--chase-sec", type=int, default=15, help="per-order chase window to sit at best bid")
    ap.add_argument("--live", action="store_true", help="place live orders")
    ap.add_argument("--cancel-after", action="store_true", help="cancel open probe orders at end")
    args = ap.parse_args()

    symbol = args.symbol or DEFAULT_SYMBOL
    order_ids, filled = place_probe_maker_buys(symbol, args.n, args.usd, args.usd_spread, args.wait, args.ladder_ticks, args.live, args.mode, args.chase_sec)

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

    if args.cancel_after and order_ids and args.live:
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
