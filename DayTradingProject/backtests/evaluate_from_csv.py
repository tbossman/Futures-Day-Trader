import pandas as pd, numpy as np

df = pd.read_csv("logs/trades.csv")
df["pnl"] = df["pnl"].astype(float)
wins = (df["pnl"] > 0).sum()
losses = (df["pnl"] <= 0).sum()
hit_rate = wins / max(wins + losses, 1)

gross_profit = df.loc[df["pnl"] > 0, "pnl"].sum()
gross_loss = -df.loc[df["pnl"] <= 0, "pnl"].sum()
profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

# equity curve stats
equity = df["equity"].astype(float)
dd = (equity / equity.cummax() - 1.0).min()

print(f"Trades: {len(df)}")
print(f"Hit rate: {hit_rate:.2%}")
print(f"Profit factor: {profit_factor:.2f}")
print(f"Max drawdown: {dd:.2%}")