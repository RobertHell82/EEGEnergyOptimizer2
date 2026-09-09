"""Heizstab — ein Fronius Ohmpilot als steuerbare Senke für PV-Überschuss.

Zwei Schichten, wie bei den Wechselrichtern:

* ``ohmpilot_modbus.py`` — der Treiber (Modbus TCP, Sollwert, Ist-Leistung,
  Temperatur, Zeitsynchronisation). Übernommen aus HA_Optimierung_Gruenbach.
* ``controller.py`` — die Regel: wann heizt der Heizstab, mit wie viel, und
  warum. Wird vom ``ScheduleExecutor`` je Guard-Lauf gefragt; schreibt
  selbst alle 30 s, weil der Ohmpilot ohne Sollwert nach 50 s abschaltet.
"""
