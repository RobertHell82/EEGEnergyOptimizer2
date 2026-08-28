"""
This file is the dummy implementation of a config provider.
Mostly there to document the API and as base class for real configs.
"""

def warp(d, start_time):
	"""
	Convert a time-indexed object (DataFrame, Series) to start with
	start_time.

	d : expected to be indexed by seconds after midnight
	"""

	import pandas as pd

	# Index is expected to be seconds after midnight:
	t = start_time.time()
	t = t.hour * 3600 + t.minute * 60 + t.second
	l = d[d.index < t]
	l.index += d.index[-1] - 2 * d.index[0] + d.index[1]
	u = d[d.index >= t]
	d = pd.concat([u, l])
	d.index = start_time + pd.Timedelta(seconds=1) * (d.index - d.index[0])

	return d


def ts_from_file(file, start_time, columns=None):
	"""
	Read data from a CSV file and generate a time series, that starts
	with start_time.
	"""

	import pandas as pd

	d = pd.read_csv(file, index_col=0, usecols=columns, parse_dates=True)

	if type(d.index) == pd.core.indexes.datetimes.DatetimeIndex:
		return d.loc[start_time:]

	return warp(d, start_time)


class DummyForecast:
	def __init__(self, production_file):
		self.production_file = production_file

	def production(self, start_time):
		"""
		Return a time series with a local production estimate in [kW].

		start_time: First time stamp.

		This implementation reads data from a file if provided and
		returns 0 otherwise.
		"""
		try:
			ts = ts_from_file(self.production_file, start_time)
			return ts[ts.columns[0]]
		except:
			return 0

	def min_production(self, start_time):
		"""
		Return worst case assumption about production.
		"""

		s = self.production(start_time)
		return (s.clip(upper=2) + s) / 2


class Config:
	"""
	Base class for config providers. You can use this class directly only if
	you read consumption forecast and production forecast from files like you
	would do when running a simulation.
	"""

	def __init__(self, time_res=900, consumption_file=None, production_file=None):
		"""
		time_res: Target resolution of the optimized time table in seconds.
		"""
		self.time_res = time_res
		self.consumption_file = consumption_file
		self.forecast = DummyForecast(production_file)

	# Start calculating the time table this many seconds before the current
	# period is over.
	time_buffer = 110

	# Control whether we should try cell balancing.
	fullcharge_try = False

	# By default don't charge the battery from the grid, because it is
	# often forbidden.
	no_grid_charging = True

	def fetch(self):
		"""
		Fetches all the data used for building a model.

		Call this, before starting an optimization run, to get new
		forecast data etc.
		"""
		pass

	def push(self, timetable):
		"""
		Update BMS targets according to the new timetable.
		(And/or save the data for analysis etc.)
		"""
		pass

	def error(self):
		"""
		Handle the case that the optimization didn't yield a viable solution.
		"""
		pass

	grid_fee = 0.1647	#(0.152 - 0.0973) + 0.11

	ac_limit = 19.5	# The maximum AC Power of the inverter. Unit: kW

	ac_efficiency = 0.95

	def feedin_limit(self, start_time):
		"""
		Return a time series with grid limit values. Unit: kW

		start_time: First time stamp.
		"""

		return self.ac_limit - 0.5  # A scalar should work in place of a series.

	def feedin_price(self, start_time):
		"""
		Return a time series with power prices [/kWh].
		Positive values earn money.

		start_time: First time stamp.
		"""

		return 0.0973  # A scalar should work in place of a series.

	def consumption_price(self, start_time):
		"""
		Return a time series with power prices [/kWh].
		Positive values cost money.

		start_time: First time stamp.
		"""

		return self.feedin_price(start_time) + self.grid_fee

	def consumption(self, start_time):
		"""
		Return a time series with a local consumption estimate in [kW].

		start_time: First time stamp.

		This implementation reads data from a file if provided and
		returns 0 otherwise.
		"""
		try:
			ts = ts_from_file(self.consumption_file, start_time)
			return ts[ts.columns[0]]
		except:
			return 0

	# How much costs a kWh from the battery in currency based on life time
	# reduction? Difficult to estimate, because batteries age even without
	# usage.
	battery_cost = 0.01

	# The hight of the power peak (in kW) also costs something.
	# For batteries: The high currents actually cost life time.
	# For grid: Some operators have fees based on peak power.
	max_grid_cost = 0.011
	max_battery_cost = 0.01

	# Internal resistance of battery: How much power gets lost, when
	# (dis)charging at 1kW?
	# This should be >0 both for physical reasons and numerical stability.
	# This should be a quadratic term, but currently we only have a linear
	# model.
	battery_resistance = 0.04

	battery_power_limit = 7		# kW

	battery_capacity = 12.5	# kWh  - total capacity

	battery_free = 5	# kWh  - currently free capacity

	blackout_time = '18h'	# pandas time_offset. Try to prepare for this long blackouts.

	max_blackout_reserve = 6	# kWh
