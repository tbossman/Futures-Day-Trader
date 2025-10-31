import gymnasium as gym
import numpy as np
import pandas as pd

class BTCEnv(gym.Env):
    metadata = {"render_modes": []}
    def __init__(self, df: pd.DataFrame, fee_bps=6, lookback=60):
        super().__init__()
        self.df = df.reset_index(drop=True)
        self.fee = fee_bps/10000.0
        self.lookback = lookback
        fcols = [c for c in df.columns if c not in ("timestamp","open","high","low","close","volume")]
        self.fcols = fcols
        self.observation_space = gym.spaces.Box(low=-np.inf, high=np.inf, shape=(lookback*len(fcols),), dtype=np.float32)
        self.action_space = gym.spaces.Discrete(3)  # 0=flat,1=long,2=close
        self.reset()

    def _obs(self):
        w = self.df.loc[self.i-self.lookback:self.i-1, self.fcols].values.astype(np.float32).flatten()
        return w

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.i = self.lookback
        self.pos = 0; self.entry=0.0
        return self._obs(), {}

    def step(self, action):
        price = float(self.df.loc[self.i, "close"])
        reward=0.0
        if action==1 and self.pos==0:
            self.pos=1; self.entry=price*(1+self.fee)
        elif action==2 and self.pos==1:
            pnl = (price*(1-self.fee)-self.entry)/max(self.entry,1e-9)
            reward = float(pnl)
            self.pos=0; self.entry=0.0
        self.i += 1
        done = self.i >= len(self.df)-1
        obs = self._obs() if not done else np.zeros_like(self._obs())
        return obs, reward, done, False, {}
