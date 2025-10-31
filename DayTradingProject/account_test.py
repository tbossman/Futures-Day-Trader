# account_test.py  (spend ~$10 USD on BTC)
import os, ccxt
from dotenv import load_dotenv

load_dotenv()
ex = ccxt.coinbase({
    "apiKey": os.getenv("API_KEY"),
    "secret": (os.getenv("API_SECRET") or "").replace("\\n","\n"),
    "enableRateLimit": True,
    # optional: disable the price requirement globally
    "options": {"createMarketBuyOrderRequiresPrice": False},
})

symbol = "BTC/USD"
cost_usd = 10  # try >= your exchange min notional

print(f"Buying ~${cost_usd} {symbol}…")
order = ex.create_order(symbol, "market", "buy", cost_usd, None, {"cost": cost_usd})
print("OK order id:", order.get("id"), "status:", order.get("status"))