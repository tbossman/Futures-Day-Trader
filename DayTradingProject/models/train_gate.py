import glob, os, joblib, numpy as np, pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score

DATA_GLOB = "data/processed/*_feat.parquet"
os.makedirs("models", exist_ok=True)

files = glob.glob(DATA_GLOB)
if not files: raise SystemExit("No processed features; run features/collect.py and features/build.py")
dfs = [pd.read_parquet(p) for p in files]
df = pd.concat(dfs, ignore_index=True)

fwd = df["close"].shift(-5)
y = ((fwd - df["close"])/df["close"] > 0.0005).astype(int)
X = pd.DataFrame({
    "ema_fast": df["ema_fast"],
    "ema_slow": df["ema_slow"],
    "ema_diff": df["ema_fast"] - df["ema_slow"],
    "rsi": df["rsi"],
    "atr": df["atr"],
    "ret_1": df["ret_1"],
}).replace([np.inf,-np.inf], np.nan).dropna()
y = y.loc[X.index]

tscv = TimeSeriesSplit(n_splits=4)
best_auc=-1; best=None
for tr,te in tscv.split(X):
    base=LogisticRegression(max_iter=2000)
    clf = CalibratedClassifierCV(base, cv=3)
    clf.fit(X.iloc[tr], y.iloc[tr])
    p = clf.predict_proba(X.iloc[te])[:,1]
    auc = roc_auc_score(y.iloc[te], p)
    if auc>best_auc: best_auc, best = auc, clf

joblib.dump({"model":best, "features":list(X.columns), "metric_auc":float(best_auc)}, "models/gate.joblib")
print("saved models/gate.joblib AUC=", best_auc)
