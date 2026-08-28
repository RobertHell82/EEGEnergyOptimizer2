"""Fahrplan-Optimierung — Upstream: EngagePV/chamo.

Bewusst leer, was Imports betrifft: hier darf kein pandas landen. Home
Assistant lädt Integrations-Packages im Event-Loop, und der pandas-Import
blockiert lange genug, dass HA es als blocking call meldet. Wer opt()
braucht, importiert opt_highs im Executor.
"""
