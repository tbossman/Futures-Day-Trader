import json, glob, pandas as pd
from wfo.search_params import search

files = glob.glob("data/processed/*_feat.parquet")
if not files: raise SystemExit("No processed features. Run features/collect.py and features/build.py first.")
df = pd.read_parquet(files[0]).rename(columns=str.capitalize)

bar_per_m = 30*24*60  # approx 1m bars per month
train = 3*bar_per_m; test = 1*bar_per_m
i=0; best_params=None; best_pf=-1

while i + train + test < len(df):
    tr = df.iloc[i:i+train]
    params, pf = search(tr)
    if pf > best_pf:
        best_pf, best_params = pf, params
    i += test

if not best_params: raise SystemExit("WFO found no params.")
out = {
  "ema_fast": best_params[0],
  "ema_slow": best_params[1],
  "atr_window": best_params[2],
  "sl_mult": best_params[3],
  "tp_mult": best_params[4],
  "gate_threshold": 0.55,
  "rl_enabled": true,
  "rl_lookback": 60
}
with open("models/params.json","w") as f: json.dump(out, f, indent=2)
print("Wrote models/params.json", out)
