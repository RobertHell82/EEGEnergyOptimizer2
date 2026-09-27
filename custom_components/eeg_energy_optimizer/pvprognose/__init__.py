"""Eigene PV-Prognose: Open-Meteo-Wetter + Anlagenmodell, ohne fremde Integration.

Öffentliche Schnittstelle des Pakets — der Rest der Integration importiert
nur von hier:

* ``PvPrognoseProvider`` — hält die Leistungsreihe, frischt sie auf, liefert
  Fahrplan-Halbstunden, Tagessummen und den Stand fürs Panel
* ``berechne_einmalig`` — Probe für „Prognose berechnen" (ungespeicherte Eingaben)
* ``pruefe_flaechen`` — Prüfung/Normierung der Flächenliste beim Speichern
* ``Flaeche`` — die Modulfläche als Datentyp
"""

from .modell import Flaeche, pruefe_flaechen
from .provider import MAX_ALTER_S, WARN_ALTER_S, PvPrognoseProvider, berechne_einmalig

__all__ = [
    "Flaeche",
    "MAX_ALTER_S",
    "PvPrognoseProvider",
    "WARN_ALTER_S",
    "berechne_einmalig",
    "pruefe_flaechen",
]
