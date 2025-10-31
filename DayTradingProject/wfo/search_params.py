import itertools, numpy as np, pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

class EmaStrat(Strategy):
    n1=20; n2=50; sl_mult=1.0; tp_mult=1.5; atr_win=14
    def init(self):
        c = pd.Series(self.data.Close)
        self.ema1 = self.I(lambda: c.ewm(span=self.n1, adjust=False).mean())
        self.ema2 = self.I(lambda: c.ewm(span=self.n2, adjust=False).mean())
        self.atr  = self.I(lambda: c.diff().abs().rolling(self.atr_win).mean())
    def next(self):
        price = float(self.data.Close[-1])
        if not self.position and crossover(self.ema1, self.ema2):
            atr = float(self.atr[-1] or 0.0)
            if atr > 0:
                sl = price - self.sl_mult * atr
                tp = price + self.tp_mult * atr
                self.buy(sl=sl, tp=tp)

def search(df):
    grid = itertools.product([10,20,30],[40,50,80],[10,14,20],[0.8,1.0,1.2],[1.2,1.5,2.0])
    best=None; best_pf=-1
    bt = Backtest(df, EmaStrat, cash=10_000, commission=0.0006)
    for n1,n2,atrw,slm,tpm in grid:
        if n1>=n2: continue
        stats = bt.run(n1=n1,n2=n2,atr_win=atrw,sl_mult=slm,tp_mult=tpm)
        pf = stats.get("Profit Factor", 0)
        dd = stats.get("Max. Drawdown [%]", 1000)
        if pf>best_pf and dd<35:
            best_pf, best = pf, (n1,n2,atrw,slm,tpm)
    return best, best_pf
