import pandas as pd
try:
	from .highs_adapter import Constraint, Model, Objective, Variable
except ImportError:  # Skriptbetrieb ohne Package-Kontext
	from highs_adapter import Constraint, Model, Objective, Variable

log_performance = False

def extract_values(series):
	def value(x):
		try:
			return x.primal
		except AttributeError:
			return x
	return series.map(value)

def opt(c, start_time):
	"""
	Build a model (using optlang) and solve it.

	Parameters:
	c : Instance of a subclass of config_dummy.Config
	start_time : Timestamp when c.battery_free is valid. (i.e. nowish)

	Return value: optimized timetable as pandas DataFrame with columns:
	'grid_p' : power to/from the grid in kW - positive values: export
	'PV' : DC power available from the PV modules - forecast in kW
	'consumption' : local consumption forecast in kW as positive values
	'battery_p' : DC power to/from the battery in kW - positive values: discharging
	'battery' : Energy [kWh] the battery can take until it is full.
		    Negative values (over full) is possible, if target SoC < 100%.
	'battery_ub' : upper limit for 'battery' as per dynamic blackout reserve
	'discard' : DC power [kW], that has to be discarded.
	'heater' : DC power [kW] out of 'discard' that goes into the heating rod
		   (0 unless heating rod power, heat value and buffer budget are set).
	'ac_price' : value of AC power [Currency/kWh]
	'bat_price' : value of energy in battery [Currency/kWh]
	'dc_price' : value of DC power [Currency/kWh]
	'feedin_price' : What we get for exporting to the grid. [Currency/kWh]
	'consumption_price' : What we pay for buying from the grid. [Currency/kWh]
	"""

	if log_performance: print('Begin:', start_time.now())
	p2e = c.time_res / 3600
	parameters = pd.DataFrame({
		'consumption' : c.consumption(start_time),
		'dc_production' : c.forecast.production(start_time),
		'min_production' : c.forecast.min_production(start_time),
		'consumption_price' : c.consumption_price(start_time) * c.time_res / 3600,
		'feedin_price' : c.feedin_price(start_time) * c.time_res / 3600,
		'feedin_limit' : c.feedin_limit(start_time)}
	).resample(pd.Timedelta(seconds=c.time_res)).interpolate(limit_direction='backward')
	parameters.dropna(inplace=True)
	parameters['i'] = '_' + pd.RangeIndex(stop=len(parameters)).astype(str)

	# Calculate dynamic blackout reserve
	# This is also used to guarantee early filling of battery when the weather is unstable.
	residual = (parameters.consumption - parameters.min_production) * p2e
	bor_limit = pd.Series(c.battery_capacity, index=parameters.index).mask(residual > 0, c.max_blackout_reserve)
	bor = residual.rolling(c.blackout_time).apply(lambda x: x.cumsum().max())
	bor = bor.shift(-1, c.blackout_time).reindex_like(parameters).fillna(0).clip(upper=bor_limit, lower=0)

	# Make some effort to ensure cell balancing, if it seems possible to achive:
	if c.fullcharge_try and residual.iloc[0] < 0:
		i = residual.index.get_loc(residual.index[residual > 0][0])
		s = 0
		while i > 0:
			i -= 1
			s -= residual.iloc[i]
			if s > 1:  # kWh, maybe make this configureable?
				bor.iloc[i] = c.battery_capacity
				break

	# Make sure, that the system remains solveable:
	bat_content = c.battery_capacity - c.battery_free
	min_available_energy = parameters.dc_production.shift(fill_value=0).cumsum() * (p2e * (1 - 2 * c.battery_resistance))
	bor.clip(upper=bat_content + min_available_energy, inplace=True)

	battery_free_ub = c.battery_capacity - bor
	battery_free_ub.index = parameters.i

	battery_free_lb = (residual.clip(lower=0).cumsum() + c.battery_free).clip(upper=0)
	battery_free_lb.index = parameters.i

	# Manipulation for better numerical properties (ie use battery early)
	price_mask = (parameters.feedin_price / 1e6).cumsum()
	parameters.feedin_price += price_mask
	parameters.consumption_price -= price_mask

	if log_performance: print('After parameters:', start_time.now())

	battery_free = parameters.i.map(lambda i: Variable('battery_free' + i, lb = battery_free_lb[i], ub = battery_free_ub[i]))
	if c.fullcharge_try:
		battery_free[len(battery_free)-1] = 0
	else:
		battery_free.iloc[len(battery_free)-1] = c.battery_capacity / 2
	battery_p_pos = parameters.i.map(lambda i: Variable('battery_p_pos' + i, lb = 0, ub = c.battery_power_limit))
	battery_p_neg = parameters.i.map(lambda i: Variable('battery_p_neg' + i, lb = 0, ub = c.battery_power_limit))
	grid_p_pos = pd.Series(index=parameters.index, data=pd.RangeIndex(stop=len(parameters)).map(
		lambda i: Variable('grid_p_pos_' + str(i), lb = 0,
			ub = min(parameters.feedin_limit.iloc[i], c.ac_limit - parameters.consumption.iloc[i]))))
	grid_p_neg = parameters.i.map(lambda i: Variable('grid_p_neg' + i, lb = 0))
	discard_p_ub = pd.Series(index=parameters.i, data=parameters.dc_production.values)
	discard_p = parameters.i.map(lambda i: Variable('discard_p' + i, lb = 0, ub = discard_p_ub[i]))

	# Since we can't do proper internal resistance in a linear model,
	# we assume that (dis)charging below 0.1C is free and above we have
	# (linear) losses with one step. These variables define the loss part:
	battery_high1_p  = parameters.i.map(lambda i: Variable('battery_high1_p' + i, lb = 0, ub = c.battery_capacity / 10))
	battery_high2_p  = parameters.i.map(lambda i: Variable('battery_high2_p' + i, lb = 0, ub = c.battery_power_limit - c.battery_capacity / 5))

	dc_p = parameters.dc_production + battery_p_pos - battery_p_neg - discard_p - c.battery_resistance * battery_high1_p - 2 * c.battery_resistance * battery_high2_p

	# --- Heizstab als bewertete Senke -----------------------------------
	#
	# Ohne ihn ist 'discard' wertlos: Das Modell regelt ab, wenn es muss,
	# und der Heizstab bekommt den Rest geschenkt. Mit Wärmewert wird daraus
	# eine echte Alternative — und damit rechnet sich eine Abendentladung
	# nur noch, wenn die Einspeisung mehr bringt als die Wärme, die morgen
	# aus derselben Kilowattstunde würde.
	#
	# Drei Bedingungen müssen zusammenkommen, sonst wird nichts gebaut und
	# das Modell bleibt Zeile für Zeile das von vorher: eine Leistung, ein
	# Wärmewert und ein Aufnahmebudget des Puffers. Fehlt eines, ist der
	# Weg nicht bewertbar — und ein unbegrenzt bewerteter Heizstab würde
	# jede Entladung dauerhaft blockieren.
	heizstab_max_kw = float(getattr(c, 'heizstab_max_kw', 0.0) or 0.0)
	heizstab_waermewert = float(getattr(c, 'heizstab_waermewert', 0.0) or 0.0)
	heizstab_budget_kwh = float(getattr(c, 'heizstab_budget_kwh', 0.0) or 0.0)
	heizstab_aktiv = (
		heizstab_max_kw > 0 and heizstab_waermewert > 0 and heizstab_budget_kwh > 0
	)

	if heizstab_aktiv:
		# Der Heizstab hängt auf der AC-Seite, 'discard' ist DC — deshalb
		# die Obergrenze in DC umrechnen. Mehr als abgeregelt wird, kann er
		# ohnehin nicht bekommen (Nebenbedingung unten).
		heater_ub_dc = heizstab_max_kw / c.ac_efficiency
		heater_p = parameters.i.map(lambda i: Variable(
			'heater_p' + i, lb = 0, ub = min(discard_p_ub[i], heater_ub_dc)))
	else:
		heater_p = pd.Series(0.0, index=parameters.i.index)

	battery_p_max_var = Variable('battery_p_max', lb = 0)
	grid_p_max_var = Variable('grid_p_max', lb = 0)
	battery_p_max = pd.Series(battery_p_max_var, index=battery_p_neg.index)
	grid_p_max = pd.Series(grid_p_max_var, index=battery_p_neg.index)    
	if log_performance: print('After variables:', start_time.now())

	battery_p_max_constr = (battery_p_max - battery_p_neg - battery_p_pos).map(lambda ex: Constraint(ex, lb = 0))
	battery_high_p_constr = (battery_p_pos + battery_p_neg - battery_high1_p - battery_high2_p).map(lambda ex: Constraint(ex, lb = 0, ub = c.battery_capacity / 10))
	grid_p_max_constr = (grid_p_max - grid_p_pos).map(lambda ex: Constraint(ex, lb = 0))

	ac_constr = (dc_p * c.ac_efficiency + grid_p_neg - parameters.consumption - grid_p_pos).map(lambda ex: Constraint(ex, lb = 0, ub = 0))

	battery_constr = ((battery_p_pos - battery_p_neg) * c.time_res / 3600 - battery_free + battery_free.shift(fill_value=c.battery_free)).map(lambda ex: Constraint(ex, lb = 0, ub = 0))

	if log_performance: print('After constraints', start_time.now())

	model = Model()
	model.add(battery_p_max_constr)
	model.add(grid_p_max_constr)
	if c.no_grid_charging:
		dc_constr = dc_p.map(lambda ex: Constraint(ex, lb = 0))
		model.add(dc_constr)
	model.add(ac_constr)
	model.add(battery_constr)
	model.add(battery_high_p_constr)
	if heizstab_aktiv:
		# Der Heizstab bekommt höchstens, was abgeregelt wird …
		model.add((discard_p - heater_p).map(lambda ex: Constraint(ex, lb = 0)))
		# … und über den Horizont nicht mehr, als der Puffer noch aufnimmt.
		# Eine einzige Summenschranke statt eines zweiten Speichers mit
		# Zeitverlauf: Sie wird bei jedem Planlauf aus der gemessenen
		# Puffertemperatur neu gebildet und ist damit immer aktuell.
		model.add(Constraint(
			heater_p.sum() * c.ac_efficiency * p2e, ub = heizstab_budget_kwh,
			name = 'heizstab_budget'))
	ziel = ((grid_p_pos * parameters.feedin_price).sum() -
		(grid_p_neg * parameters.consumption_price).sum() -
		battery_p_max_var * c.max_battery_cost - grid_p_max_var * c.max_grid_cost -
		battery_p_pos.sum() * c.battery_cost * c.time_res / 3600 -
		battery_high2_p.sum() * c.battery_cost * c.time_res / 3600)
	if heizstab_aktiv:
		# Wärme zum vereinbarten Wert — dieselbe Einheit wie die Preisterme
		# oben (feedin_price trägt p2e bereits in sich, siehe parameters).
		ziel = ziel + heater_p.sum() * heizstab_waermewert * c.ac_efficiency * p2e
	model.objective = Objective(ziel, direction='max')
	status = model.optimize()

	if log_performance: print('After optimization:', start_time.now())

	timetable = pd.DataFrame({'grid_p' : extract_values(grid_p_pos) - extract_values(grid_p_neg),
		'PV' : parameters.dc_production,
		'consumption' : parameters.consumption,
		'battery_p' : extract_values(battery_p_pos) - extract_values(battery_p_neg),
#		'battery_high_p' : extract_values(battery_high_p),  # Only useful for debugging
		'battery' : extract_values(battery_free),
		'battery_ub' : battery_free_ub.values,
		'discard' : extract_values(discard_p),
		'heater' : extract_values(heater_p) if heizstab_aktiv else 0.0,
		'ac_price' : ac_constr.map(lambda x: -x.dual) * 3600 / c.time_res,
		'bat_price' : battery_constr.map(lambda x: x.dual),
		'dc_price' : discard_p.map(lambda x: -x.dual) * 3600 / c.time_res,
		'feedin_price' : parameters.feedin_price * 3600 / c.time_res,
		'consumption_price' : parameters.consumption_price * 3600 / c.time_res,
		'feedin_limit' : parameters.feedin_limit})

	return timetable
