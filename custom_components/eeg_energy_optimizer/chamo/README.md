# Herkunft dieses Ordners

Der Fahrplan-Optimierer in diesem Ordner stammt aus dem Projekt
**[EngagePV/chamo](https://gitlab.com/EngagePV/chamo)** und wurde vollständig
von **Harald Geyer** geschrieben. Übernommen mit seiner Zustimmung; das
Urheberrecht bleibt bei ihm.

Ursprungs-Commit:
[`de2570bf`](https://gitlab.com/EngagePV/chamo/-/commit/de2570bf01b57fa21235e832b1cc5c779d7b2f3c)
(„Draft: Add optimization layer", 23.08.2026). Er liegt in der Git-Historie
dieses Repos unverändert und unter seiner Autorschaft vor — eingespielt per
`git am`, danach folgen unsere Änderungen als getrennte Commits.

## Was von Harald ist

| Datei | Zustand |
|-------|---------|
| `config_dummy.py` | unverändert |
| `opt_highs.py` | Haralds `opt-optlang.py`; zwei Änderungen, siehe unten |
| `opt_test.py` | unverändert |
| `timetableopt` | unverändert |

## Was wir geändert haben

* **Dateiname** `opt-optlang.py` → `opt_highs.py`: Ein Bindestrich ist in
  Python kein gültiger Modulname, `import` scheitert daran.
* **Import** `from optlang import *` → die vier Namen aus `highs_adapter`, mit
  Rückfall auf den flachen Import, damit Haralds Skript-Workflow
  (`python3 -i opt_test.py`) weiter funktioniert.

Die Logik in `opt()` ist Zeile für Zeile unverändert.

## Was von uns ist

* `highs_adapter.py` — optlang-kompatible Minimalschicht auf HiGHS. Nötig, weil
  `optlang` über `swiglpk` auf HAOS (Alpine/musl) nicht installierbar ist.
  Begründung und Messwerte in [`../../../CHAMO.md`](../../../CHAMO.md).
* `__init__.py` — hält den Ordner als Python-Package zusammen, ohne pandas in
  den Event-Loop zu ziehen.

## Upstream nachziehen

Neue Versionen von Haralds Dateien lassen sich als Patch holen und in diesen
Ordner anwenden:

```
curl -sSL https://gitlab.com/EngagePV/chamo/-/commit/<sha>.patch -o upstream.patch
git apply --directory=custom_components/eeg_energy_optimizer -p2 upstream.patch
```

Danach die beiden Änderungen oben erneut anwenden — sie sind absichtlich so
klein gehalten, dass das in Sekunden geht.
