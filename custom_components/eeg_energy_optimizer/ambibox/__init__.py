"""Ambibox (sidOS) — angestecktes Fahrzeug lesen.

``modbus.py`` spricht Modbus TCP, ``controller.py`` hält den Zustand und
gibt ihn an Sensoren und Panel weiter. Schritt 1 liest ausschließlich;
Steuern (Laden/Entladen) kommt in einem zweiten Schritt.
"""

from .controller import AmbiboxController, create_ambibox

__all__ = ["AmbiboxController", "create_ambibox"]
