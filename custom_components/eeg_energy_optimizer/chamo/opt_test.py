"""
Run this as `python3 -i opt_test.py` to get one optimization run for
interactive testing.
"""

import config
import opt-optlan as opt
import matplotlib.pyplot as plt
plt.ion()
c = config.Config()
c.fetch()
t = opt.opt(c, c.forecast.forecast_df.index[0])

