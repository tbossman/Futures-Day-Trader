import pandas as pd, os
from stable_baselines3 import PPO
from rl.env import BTCEnv

def load_feat(path="data/processed/BTC_USD_1m_feat.parquet"):
    return pd.read_parquet(path)

if __name__ == "__main__":
    df = load_feat()
    cutoff = int(len(df)*0.8)
    env = BTCEnv(df.iloc[:cutoff].copy(), fee_bps=6, lookback=60)
    model = PPO("MlpPolicy", env, verbose=0)
    model.learn(total_timesteps=300_000)
    os.makedirs("models", exist_ok=True)
    model.save("models/rl_policy")
    print("saved models/rl_policy.zip")
