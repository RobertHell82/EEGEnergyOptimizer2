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

Zwei Änderungen sind rein technisch und berühren die Rechnung nicht:

* **Dateiname** `opt-optlang.py` → `opt_highs.py`: Ein Bindestrich ist in
  Python kein gültiger Modulname, `import` scheitert daran.
* **Import** `from optlang import *` → die vier Namen aus `highs_adapter`, mit
  Rückfall auf den flachen Import, damit Haralds Skript-Workflow
  (`python3 -i opt_test.py`) weiter funktioniert.

Dazu kommen **drei Abweichungen in `opt()`**, alle im Code mit `LOCAL CHANGE`
markiert (`grep -n "LOCAL CHANGE" opt_highs.py`). Wer mit Haralds Stand
abgleicht, muss genau diese Stellen gesondert behandeln — alles andere im
Modell ist unangetastet. Zwei davon greifen in die Rechnung ein, die dritte
ist eine Fehlermeldung: nach `model.optimize()` wird der Solver-Status
geprüft, weil die Ergebnistabelle sonst an `None - None` scheitert und nicht
sagt, woran es lag.

### 1. Heizstab als bewertete Senke

`discard_p` wird in `heater_p` + `spill_p` aufgeteilt, `heater_p` geht mit dem
Wärmewert in die Zielfunktion und ist doppelt gedeckelt (Leistung je Slot,
Pufferbudget je Kalendertag). Das Modell bekommt damit eine
Entscheidungsvariable, zwei Nebenbedingungen und einen Zielterm mehr.

Ohne Leistung, Wärmewert **und** Budget wird nichts davon gebaut — dann ist
das Modell Zeile für Zeile das alte (Regressionstest
`test_ohne_heizstab_identisch`). Ausführlich in `../../../CLAUDE.md`.

### 2. Reserve nur aus Überschuss, nie aus dem Bestand

Die dynamische Notstrom-Reserve (`bor`) wird auf das gedeckelt, was der
Speicher haben *kann*. Diese Schranke war eine reine Lösbarkeitsgarantie
(„Make sure, that the system remains solveable") und rechnete mit *Inhalt +
gesamter PV-Erzeugung*. Das ist zu großzügig: Das Haus lebt von derselben
Energie, und was nicht mehr in den vollen Speicher passt, geht ohnehin ins
Netz. Die Schranke ließ deshalb Reserve-Forderungen durch, die zwar
erreichbar waren — aber nur, wenn man den Hausverbrauch **zukauft**, statt
ihn aus dem Speicher zu nehmen.

Gemessen an der Anlage Ansfelden am 21.09.2026 (16,6 kWh, Mindest-Ladestand
10 %, Bezug 21,05 ct, Einspeisung 3,9 ct):

| | |
|---|---|
| Reserve-Forderung für den nächsten Morgen | 4,14 kWh |
| ohne Netzbezug erreichbar | 0,87 kWh |
| Netzbezug im Plan / im Standardbetrieb | 5,43 / 2,31 kWh |
| tiefster geplanter Ladestand | 30,2 % (Boden: 10 %) |
| Ergebnis gegen Standardbetrieb | **−0,51 €** |

Im Archiv der Anlage wiesen 36 von 119 Plänen aus sieben Tagen einen
negativen Vorteil aus, und zwar synchron dazu, wie oft die Reserve band.

Verschärft ist die Frage, nicht der Mechanismus: nicht mehr „kann der
Speicher das haben?", sondern „kann er das haben, ohne dass jemand Strom
kauft?". Der Stand wird dafür Slot für Slot fortgeschrieben — Batterie
zuerst, Rest aus dem Netz, begrenzt bei leer und bei voll. Eine kumulierte
Summe genügt nicht: Sobald der Speicher einmal voll war, ist der übrige
Überschuss ins Netz gegangen und darf nicht weiter mitgezählt werden.

Die Reserve bleibt in Kraft — sie verlangt weiterhin, dass Überschuss in den
Speicher geht statt ins Netz (an denselben Reihen bindet sie in 18 Slots).
Sie kann nur nicht mehr verlangen, dass vorhandene Energie liegen bleibt,
während das Haus sie braucht. Die Schranke kann die Reserve ausschließlich
**senken**.

Nebenbefund, der nicht mit korrigiert ist: `bor_limit` setzt den Deckel in
Slots mit PV-Überschuss auf die volle Batteriekapazität und nur in
Defizit-Slots auf den eingestellten `max_blackout_reserve`. Ein eingestelltes
„keine Reserve" gilt damit nur in der Hälfte der Slots, und die Grenze
zwischen beiden Fällen ist scharf: Am 22.09.2026 um 09:00 lag der
Worst-Case-Pfad **37 W** über der Hauslast — hätte er 37 W darunter gelegen,
wäre der Deckel 0 statt 14,94 kWh gewesen. Nach der Korrektur ist das
folgenlos, weil die Schranke dahinter greift; gemeldet ist es trotzdem.

Tests: `test_reserve_verlangt_nie_mehr_als_ohne_netzbezug_erreichbar`,
`test_fahrplan_kauft_nicht_mehr_als_der_standardbetrieb` und
`test_reserve_bleibt_wirksam` in `tests/test_schedule.py`, mit den echten
Prognosereihen der Anlage als Fixture.

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

Danach die vier Änderungen oben erneut anwenden. Die beiden technischen sind
in Sekunden erledigt; bei den beiden inhaltlichen ist zu prüfen, ob Harald
dieselbe Stelle angefasst hat — beim Heizstab die `discard_p`-Zerlegung, bei
der Reserve die Schranke unter „Make sure, that the system remains
solveable". Die Reserve-Korrektur wäre idealerweise upstream aufgehoben; ist
sie das, entfällt sie hier ersatzlos.
