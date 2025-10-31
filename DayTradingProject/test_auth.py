# test_auth.py — SAFE, NO-TRADE tester for your setup.
# - Does NOT import or modify live.py
# - Connects to exchange (read-only), prints market info
# - Simulates a full trade cycle OFFLINE with your exact rules:
#     TP% = 4.9, SL% = 13.44, maker fee 0.25%/side, progressive sizing 0.70→0.75→0.80 of equity
# - Uses current bid/ask as reference prices (or SIM_ENTRY override)
#
# Usage:
#   EXCHANGE=kraken SYMBOLS=DOGE/USD API_KEY=... API_SECRET=... \
#   START_EQUITY=1000 GROWTH_PCT=4.9 SL_PCT=13.44 \
#   POS_FRAC_MIN=0.70 POS_FRAC_PREFERRED=0.75 POS_FRAC_MAX=0.80 \
#   MAKER_FEE_RATE=0.0025 PRICE_IMPROVE_PCT=0.0002 \
#   python test_auth.py
#
# Optional offline entry price (no auth needed):
#   SIM_ENTRY=0.12345 python test_auth.py
#
import os
from datetime import datetime, timezone

try:
    import ccxt
except Exception:
    ccxt = None

def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

def main() -> int:
    EXCHANGE = os.getenv("EXCHANGE", "kraken").strip()
    SYMBOL   = os.getenv("SYMBOLS", "DOGE/USD").split(",")[0].strip()

    START_EQUITY = env_float("START_EQUITY", 1000.0)

    # Strategy constants (strict to your predictor)
    TP_PCT   = env_float("GROWTH_PCT", 4.9)      # always 4.9
    SL_PCT   = env_float("SL_PCT", 13.44)        # 13.44
    # Maker economics
    MAKER_FEE_RATE    = env_float("MAKER_FEE_RATE", 0.0025)  # per side
    ROUNDTRIP_FEE_PCT = 2.0 * MAKER_FEE_RATE * 100.0         # as percent
    PRICE_IMPROVE_PCT = env_float("PRICE_IMPROVE_PCT", 0.0002)

    # Progressive sizing
    POS_FRAC_MIN  = env_float("POS_FRAC_MIN", 0.70)
    POS_FRAC_PREF = env_float("POS_FRAC_PREFERRED", 0.75)
    POS_FRAC_MAX  = env_float("POS_FRAC_MAX", 0.80)

    print("== TEST AUTH (NO TRADES) ==")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Exchange={EXCHANGE}  Symbol={SYMBOL}")
    print(f"Equity={START_EQUITY:.2f}")
    print(f"TP%={TP_PCT:.4f}%  SL%={SL_PCT:.4f}%  MakerFee/side={MAKER_FEE_RATE*100:.2f}% (~{ROUNDTRIP_FEE_PCT:.3f}% roundtrip)")
    print(f"Sizing: {POS_FRAC_MIN:.2f} → {POS_FRAC_PREF:.2f} → {POS_FRAC_MAX:.2f} of equity (progressive)")

    # Get reference bid/ask unless SIM_ENTRY is provided
    sim_entry = os.getenv("SIM_ENTRY")
    bid = ask = None

    if sim_entry is not None or ccxt is None:
        try:
            px = float(sim_entry) if sim_entry is not None else 1.0
        except Exception:
            px = 1.0
        bid, ask = px, px * (1 + 0.0001)  # tiny spread
        print("\n[INFO] Offline mode (SIM_ENTRY). Using entry≈", px)
    else:
        try:
            ex = getattr(ccxt, EXCHANGE)({
                "apiKey": os.getenv("API_KEY", ""),
                "secret": os.getenv("API_SECRET", ""),
                "enableRateLimit": True,
            })
            markets = ex.load_markets()
            if SYMBOL not in markets:
                print(f"[ERR] Symbol {SYMBOL} not found on {EXCHANGE}")
                return 1
            t = ex.fetch_ticker(SYMBOL)
            bid, ask = float(t.get("bid") or 0.0), float(t.get("ask") or 0.0)
            print(f"[OK ] Connected. bid={bid} ask={ask} @ {t.get('datetime')}")
            print(f"[OK ] Open orders (read-only): {len(ex.fetch_open_orders(SYMBOL))}")
        except Exception as e:
            print("[WARN] Exchange access failed; falling back to offline SIM_ENTRY.")
            px = float(sim_entry) if sim_entry is not None else 1.0
            bid, ask = px, px * (1 + 0.0001)

    if not bid or not ask:
        print("[ERR] No reference price available.")
        return 1

    # --- Simulate a LONG trade cycle, maker-only, progressive staging ---
    entry_ref = bid  # buy at/near bid as maker
    price_improve = 1 - PRICE_IMPROVE_PCT
    # Stage 1/2/3 targets
    stages = [POS_FRAC_MIN, POS_FRAC_PREF, POS_FRAC_MAX]
    notional_targets = [START_EQUITY * f for f in stages]

    print("\n== DRY SIM: LONG, MAKER, PROGRESSIVE ==")
    print(f"Entry ref (bid)={entry_ref:.6f}  price_improve={PRICE_IMPROVE_PCT*10000:.1f} bps inside")

    # Simulate fills at improved bid (maker)
    fills = []
    for i, target_quote in enumerate(notional_targets, 1):
        child_amt = target_quote / entry_ref
        child_px  = entry_ref * price_improve  # maker inside bid
        fills.append((child_amt, child_px))
        v = sum(a*p for a,p in fills) / sum(a for a,_ in fills)
        cur_notional = sum(a for a,_ in fills) * entry_ref
        print(f" Stage {i}: target_notional≈{target_quote:.2f} | child_px≈{child_px:.6f} | VWAP≈{v:.6f} | notional≈{cur_notional:.2f}")

    # Final VWAP and TP/SL from percentages
    total_amt = sum(a for a,_ in fills)
    entry_vwap = sum(a*p for a,p in fills) / total_amt
    tp_px = entry_vwap * (1 + TP_PCT/100.0)
    sl_px = entry_vwap * (1 - SL_PCT/100.0)

    # P&L at TP and at SL (net of maker fees)
    full_notional_entry = entry_vwap * total_amt
    full_notional_exit_tp = tp_px * total_amt
    full_notional_exit_sl = sl_px * total_amt

    # Roundtrip maker fees (entry + exit) on notional
    fees_tp = MAKER_FEE_RATE * (full_notional_entry + full_notional_exit_tp)
    fees_sl = MAKER_FEE_RATE * (full_notional_entry + full_notional_exit_sl)

    gross_tp = full_notional_exit_tp - full_notional_entry
    gross_sl = full_notional_entry - full_notional_exit_sl  # positive number for loss magnitude

    net_tp = gross_tp - fees_tp
    net_sl = gross_sl + fees_sl  # loss magnitude including fees

    print("\n== RESULTS ==")
    print(f" Entry VWAP={entry_vwap:.6f}")
    print(f" TP px={tp_px:.6f} (+{TP_PCT:.4f}%)  | SL px={sl_px:.6f} (-{SL_PCT:.4f}%)")
    print(f" Position amt≈{total_amt:.6f}  Notional entry≈{full_notional_entry:.2f}")
    print(f" Gross TP≈{gross_tp:.2f}  Fees≈{fees_tp:.2f}  Net TP≈{net_tp:.2f}")
    print(f" Gross SL≈-{gross_sl:.2f}  Fees≈{fees_sl:.2f}  Net SL≈-{net_sl:.2f}")
    print("\n(No live orders were placed. This is an offline simulation.)")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())