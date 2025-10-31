import numpy as np, pandas as pd
from stable_baselines3 import PPO

class RLSignal:
    def __init__(self, model_path="models/rl_policy.zip", lookback=60, feat_cols=None):
        self.model = PPO.load(model_path)
        self.lookback = lookback
        self.cols = feat_cols or []

    def decide(self, df: pd.DataFrame):
        fcols = [c for c in self.cols if c in df.columns]
        window = df.iloc[-self.lookback:][fcols].values.astype(np.float32).flatten()
        action, _ = self.model.predict(window, deterministic=True)
        return int(action)  # 0 flat, 1 long, 2 close
