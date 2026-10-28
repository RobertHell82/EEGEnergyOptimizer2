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
| `opt_highs.py` | Haralds `opt-optlang.py`; zwei technische Änderungen und sechs Abweichungen in `opt()` (`LOCAL CHANGE`), siehe unten |
| `opt_test.py` | unverändert |
| `timetableopt` | unverändert |

## Was wir geändert haben

Zwei Änderungen sind rein technisch und berühren die Rechnung nicht:

* **Dateiname** `opt-optlang.py` → `opt_highs.py`: Ein Bindestrich ist in
  Python kein gültiger Modulname, `import` scheitert daran.
* **Import** `from optlang import *` → die vier Namen aus `highs_adapter`, mit
  Rückfall auf den flachen Import, damit Haralds Skript-Workflow
  (`python3 -i opt_test.py`) weiter funktioniert.

Dazu kommen **sechs Abweichungen in `opt()`**, alle im Code mit `LOCAL CHANGE`
markiert (`grep -n "LOCAL CHANGE" opt_highs.py`). Wer mit Haralds Stand
abgleicht, muss genau diese Stellen gesondert behandeln — alles andere im
Modell ist unangetastet. Fünf davon greifen in die Rechnung ein, die sechste
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

### 2a. Die Schranke rechnet auch die Ladeleistung (22.09.2026)

Dieselbe Schranke, ein zweiter Durchgang. Sie prüfte, ob die geforderte
Energie **da** ist, nicht ob sie **rechtzeitig hineinpasst**: Jede
überschüssige Kilowattstunde galt als sofort im Speicher. Bei viel PV und
wenig Ladeleistung eilt der gedachte Füllstand dem wirklichen davon, und die
Reserve fordert einen Ladestand, den die Batterie bis dahin nicht erreichen
kann. Anders als beim ersten Fall bekommt man dann keinen teuren Plan,
sondern **gar keinen**: Das Modell ist unlösbar.

Anlage Ansfelden, 16,5 kWp PV — die Batterie hängt am kleineren der beiden
Wechselrichter und nimmt 4,1 kW auf:

| | |
|---|---|
| Reserve-Forderung für 13:45 | 14,94 kWh |
| mit 4,1 kW bis dahin erreichbar | 9,58 kWh |
| Folge | `Solver-Status: infeasible`, kein Fahrplan |

Am 21. und 22.09.2026 stand die Anlage deshalb stundenlang ungesteuert, an
beiden Tagen um die Mittagszeit — nur dann läuft der gedachte Füllstand
schnell nach oben, und nur dann ist die Nacht weit genug weg, dass die
Reserve das ganze Nachtdefizit verlangt.

`steps` wird jetzt nach oben auf `battery_power_limit · p2e` geklemmt.
Bewusst nur nach oben: Ein Entladeschritt, der schneller fällt, als die
Batterie kann, senkt den Stand und macht die Schranke damit konservativer —
er kann nie eine unerreichbare Forderung durchlassen.

Gemessen über 336 Szenarien (Startzeit × Ladestand × PV-Spitze × Nachtlast):
39 hatten keine Lösung, nach der Korrektur lösen **alle 336 mit vollem
18-Stunden-Fenster**. Die Reserve verliert nichts.

Tests: `test_reserve_verlangt_nie_mehr_als_ohne_netzbezug_erreichbar`,
`test_fahrplan_kauft_nicht_mehr_als_der_standardbetrieb`,
`test_reserve_bleibt_wirksam`, `test_viel_pv_wenig_ladeleistung_ergibt_einen_plan`
und `test_reserve_verlangt_nie_mehr_als_die_ladeleistung_schafft` in
`tests/test_schedule.py`, mit den echten Prognosereihen der Anlage als Fixture.

### 2b. Die Schranke gilt für das Ende des Slots (08.10.2026)

`bor[i]` begrenzt `battery_free[i]`, und das ist laut Bilanz-Nebenbedingung
(`battery_free = battery_free.shift(fill_value=c.battery_free) + Fluss`) der
Stand **nach** Slot i. Die Schranke aus Abschnitt 2 schrieb aber den Stand
**vor** dem Schritt in `reachable`. In einem Defizit-Slot liegt der Stand am
Beginn um die Hauslast des Slots über dem am Ende. Eine Forderung genau auf
dem Stand am Beginn verbot deshalb jede Entladung in diesem Slot, und das Haus
kaufte seine Last zu. Das ist genau das, was Abschnitt 2 ausschließen soll.

Sichtbar wurde es mit der Untergrenze des Ladeziels vor dem Termin
(Abschnitt 3), die nachts in jedem Slot greift: Im ersten Slot des Plans
kaufte das Haus 0,52 kW, obwohl die Batterie bei 70 % stand. Die Schranke
hängt jetzt den Stand nach dem Schritt an. In Überschuss-Slots lässt sie
damit eine Viertelstunde Laden mehr zu, was die Batterie in diesem Slot
tatsächlich schafft (der Schritt ist weiter auf die Ladeleistung gedeckelt).

Die beiden Eigenschaftstests aus Abschnitt 2/2a vergleichen jetzt mit dem
Stand nach dem Schritt.

### 2c. Die Schranke rechnet mit den Verlusten des Modells (28.10.2026)

Der dritte Durchgang an derselben Schranke. Sie rechnete jeden Schritt mit
pauschal `2 · battery_resistance` Verlust (8 %). Das Modell selbst verliert
weniger: bis 0,1 C nichts, bis 0,2 C `battery_resistance` der Leistung
darüber, oberhalb das Doppelte (`battery_high1_p` / `battery_high2_p` in
`dc_p`). Die Schranke lag damit unter dem, was das Modell tatsächlich
erreicht, und das Modell verkaufte die Differenz.

Schädlich wird das, wenn die Schranke bindet, also beim unerreichbaren
Ladeziel (Abschnitt 3). Anlage Linz, 26.10.2026, 15,8 kWh: ab 10 Uhr war das
Ziel 100 % nicht mehr zu schaffen. Jeder Plan wollte trotzdem 0,18 kW je
sonnigem Slot einspeisen, rund 0,3 kWh bis zum Termin. Der Executor setzte
dafür ein Ladelimit auf die Planleistung, statt freizugeben, und bei
wechselhafter Sonne ging jede Spitze darüber ins Netz: 1,9 kWh statt der
geplanten 0,36. Die Batterie endete bei 81 %.

Der Schritt nimmt jetzt die Umkehrung der Verlustfunktion: die
Batterieleistung `p` mit `p + Verluste(p)` = Überschuss beim Laden und
`p − Verluste(p)` = Fehlbetrag beim Entladen. Das ist genau der Weg, den das
Modell gehen kann (Netz 0, jeder Überschuss in die Batterie), also nie mehr,
als es erreicht. Die Klemmung auf die Batterieleistung aus 2a bleibt, ebenso
die Netzlade-Erreichbarkeit aus Abschnitt 5, die dieselbe Funktion nutzt.

Nachgerechnet über alle 875 Pläne aus dem Archiv der Anlage (20.–27.10.2026):
54 Pläne mit unerreichbarem Ziel speisten vorher bis zum Termin ein
(21,2 kWh), danach 8, und diese erreichen jetzt 100 %. Kein Plan wurde
unlösbar, keiner kauft mehr zu, kein Termin liegt tiefer.

`HAConfig.ladeziel` und `ladeziel_termine` rechnen mit denselben gestuften
Verlusten (`_ladeleistung_aus_dc` / `_entladeleistung_fuer_dc` in
`schedule.py`). Tests: `tests/test_erreichbarkeit_verluste.py`, geprüft gegen
eine eigene Bisektion über die Verlustfunktion.

Nebenbefund, nicht mit korrigiert: Liegt `battery_power_limit` unter 0,2 C
des Modellfensters, ist die Obergrenze von `battery_high2_p` negativ, und das
Modell baut nicht (Linz mit 2,5 kW statt 5: `lb=0 > ub=-0.34`). Das war
schon vorher so und gilt für große Batterien an kleinen Wechselrichtern.

### 3. Ladeziel zum Ende der PV-Zeit (04.10.2026)

Eine Vorgabe des Betreibers, kein Modellfehler: „Abends soll die Batterie
voll sein, auch wenn es sich nach den Preisen nicht lohnt.“ In Grünbach lag
OeMAG ab dem 01.10.2026 über dem Tagessatz der Gemeinschaft; eine
gespeicherte Kilowattstunde war nachts nichts mehr wert als mittags, und der
Fahrplan lud nur noch, was das Haus über Nacht braucht (73–85 % statt voll).
Das ist aus Sicht der Preise richtig — es gibt nur keinen Parameter, der die
Vorgabe ausdrückt:

* `bor` verlangt den größten Fehlbetrag der nächsten 18 Stunden, also gerade
  die Nacht.
* `max_blackout_reserve` hält die Reserve auch in Defizit-Slots und sperrt
  damit die Nachteinspeisung — der verworfene Weg (siehe `CLAUDE.md`).
* `fullcharge_try` kommt dem am nächsten, hängt aber eine Endbedingung an:
  `battery_free` im letzten Slot = 0, also voll am Horizontende, wann immer
  das ist — um 3 Uhr früh ohne Netzladen unlösbar.

Die Änderung sind vier Zeilen direkt vor der Schranke aus Abschnitt 2: Hat
die Konfiguration eine Methode `ladeziel(parameters, surplus)`, geht deren
Mindestinhalt je Slot als Untergrenze in `bor` ein — **vor** der Kappung auf
das ohne Netzbezug Erreichbare. Damit gelten dieselben Garantien wie für die
Reserve: kein Zukauf, keine unerreichbare Forderung. Welcher Slot das Ziel
trägt (je Kalendertag der letzte mit Überschuss), steht in `HAConfig.ladeziel`
in `schedule.py`, nicht hier. Dort fällt auch ein Ziel weg, von dem aus der
Plan bis zum Horizontende nicht mehr auf Haralds Endstand (halb voll) käme —
sonst wäre das LP unlösbar.

Ohne die Methode, oder mit Ladeziel aus (überall 0), ist `bor` unverändert,
denn `bor` ist schon nach unten bei 0 geklemmt. Upstream wäre das sauberer als
Aufteilung von `fullcharge_try` in „voll zum Ende der PV-Zeit“ und
„voll am Horizontende“; vorgeschlagen ist das.

Nachgerechnet an allen archivierten Plänen von Grünbach (27.09.–04.10.2026)
mit Ladeziel 100 %: alle **1004 Pläne lösbar**, in keinem mehr Netzbezug als
ohne Ladeziel. 1462 Ziel-Termine, geplanter Ladestand dort im Median 100 %,
am tiefsten 59,8 % — an trüben Tagen das, was die PV hergab.

Seit 08.10.2026 liefert `HAConfig.ladeziel` nicht nur am Termin einen Wert,
sondern auch davor: den Inhalt, der nötig ist, damit das Ziel auch auf dem
vorsichtigen Pfad (`parameters.min_production`) noch erreicht wird, rückwärts
vom Termin gerechnet. Der Hook hier bleibt unverändert, nur die Werte sind
andere. Anlass war eine Anlage in der Schweiz: Erwartet waren 29 kWh, der Plan
speiste davor 8 kWh ein, es kamen 21 kWh, und abends standen 77 % statt 100 %.

Tests: `tests/test_ladeziel.py`.

### 4. Endstand nur, soweit die PV hinkommt (12.10.2026)

Ohne `fullcharge_try` legt `opt()` den letzten Slot auf halben Inhalt fest
(`battery_free.iloc[-1] = battery_capacity / 2`). Das ist eine Gleichung, keine
Untergrenze: Startet die Batterie unter halb und bringt die Sonne sie im
Horizont nicht dorthin, gibt es ohne Netzladen keinen Weg — das LP ist
unlösbar, es gibt keinen Fahrplan, nach 15 Minuten greift der Failsafe.

Nachgerechnet am Grünbach-Plan vom 04.10.2026 (16,6 kWh, Mindest-Ladestand
20 %, Endstand also 60 %), PV skaliert, Start bei 25 %:

| PV im Horizont | vorher | mit `no_grid_charging = False` | jetzt |
|---|---|---|---|
| 101 / 20 kWh | lösbar | identisch | identisch |
| 5 kWh | `infeasible` | kauft 5,8 kWh **nur für den Endstand** | lösbar, hält 25 % |
| 0 kWh | `infeasible` | kauft 5,8 kWh **nur für den Endstand** | lösbar, hält 25 % |

Aufgefallen ist es nicht, weil das LP noch keinen Winter erlebt hat: Es
braucht zwei fast sonnenlose Tage und eine Batterie unter der Mitte des
Modellfensters — Dezembernebel.

Der Endstand ist jetzt *min(halb, PV-erreichbar)*. PV-erreichbar heißt: Die
Batterie hält, was sie hat, und nimmt jeden Überschuss-Schritt auf (dieselben
`steps` und dieselbe Ladeleistungsgrenze wie in Abschnitt 2a); das Haus darf
dabei aus dem Netz leben. Bewusst nicht der Stand aus `reachable`
(„Haus zuerst aus der Batterie“): Der liegt an trüben Tagen auch dann unter
halb, wenn halb erreichbar wäre, und hätte Pläne geändert, die heute lösbar
sind. So ändert sich nur, was vorher keine Lösung hatte.

Gleichzeitig Voraussetzung für Netzladen (`no_grid_charging = False`):
Sonst wird die Endbedingung zum Kaufbefehl.

Tests: `tests/test_endstand.py`.

### 5. Netzladen in ausgewählten Slots (12.10.2026, Beta)

„Batterie aus dem Netz laden": Schafft die Sonne das Ladeziel nicht, darf der
Plan zukaufen — nur in Slots, in denen Netzstrom billiger ist als der Strom,
den die Batterie danach ersetzt. Haralds Modell kennt dafür nur den globalen
Schalter `no_grid_charging` (`dc_p ≥ 0` in jedem Slot oder in keinem). Ein
Parameter, der „in diesen Slots, bis zu dieser Leistung" ausdrückt, fehlt.

Die Änderung hat drei Teile, alle hinter einer Methode
`netzladen(parameters, surplus)` der Konfiguration. Sie liefert je Slot eine
Untergrenze für `dc_p` (≤ 0, kW DC) oder `None`:

* **`dc_p ≥ Untergrenze`** statt `dc_p ≥ 0`. Wo die Grenze 0 ist, bleibt
  alles wie bisher.
* **Ladeziel mit eigener Erreichbarkeit.** Das Ladeziel wird gegen einen
  zweiten Füllstandsverlauf gekappt, der in den erlaubten Slots das Haus aus
  dem Netz und die Batterie aus PV plus Netz rechnet. Die Reserve bleibt beim
  Verlauf ohne Kauf — sie kauft nie.
* Liefert die Methode `None` (oder fehlt sie), ist das Modell Zeile für Zeile
  das ohne Netzladen: Das Ladeziel geht wie in Abschnitt 3 vor der Kappung in
  `bor` ein (`test_aus_ist_identisch` in `tests/test_netzladen.py`).

Welcher Slot erlaubt ist, steht in `HAConfig.netzladen` in `schedule.py`:
vor dem Ende der PV-Zeit, Bezugspreis + Alterung + Mindestvorteil unter dem
lastgewichteten Bezugspreis der folgenden Nacht × `ac_efficiency`², und nur
mit veröffentlichtem Preis.

Eine Eigenheit des Modells, die beim Netzladen sichtbar wird und hier
bewusst **nicht** korrigiert ist: Die AC-Bilanz rechnet `dc_p · η` auch für
negatives `dc_p`. Aus 0,95 kWh Netzbezug werden so 1 kWh in der Batterie,
Laden aus dem Netz gilt dem Modell als verlustfrei. Ohne Netzladen tritt das
nie auf. Die Slot-Auswahl rechnet deshalb mit beiden Wandlungen selbst; das
LP überschätzt nur, wie viel sich innerhalb eines erlaubten Slots lohnt.

Tests: `tests/test_netzladen.py`.

### 6. Solver-Status nach `optimize()`

Keine Änderung an der Rechnung: Ist der Status nicht `optimal`, wirft `opt()`
`RuntimeError` mit dem Status (`infeasible`, `unbounded`, …). Vorher standen
in den Variablen keine Werte, und die Ergebnistabelle scheiterte an
`None - None`, ohne zu sagen, woran es lag.

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

Danach die Änderungen oben erneut anwenden. Die beiden technischen sind
in Sekunden erledigt; bei den sechs inhaltlichen ist zu prüfen, ob Harald
dieselbe Stelle angefasst hat. Die zehn `LOCAL CHANGE`-Marker verteilen sich
so:

1. **Heizstab** (Abschnitt 1) — die `discard_p`-Zerlegung in `heater_p` +
   `spill_p`, die beiden Nebenbedingungen und der Zielterm.
2. **Reserve-Schranke** (2, 2a, 2b, 2c) — unter „Make sure, that the system
   remains solveable": `steps` mit den gestuften Verlusten (`charge_from` /
   `discharge_for`), die Klemmung auf `max_step` und der Füllstand am
   Slot-Ende in `reachable`.
3. **Ladeziel-Hook** (3) — `c.ladeziel(parameters, surplus)` hebt `bor` vor
   der Kappung an.
4. **Endstand** (4) — im `else` von `fullcharge_try`: halb voll, höchstens
   was die PV allein erreicht.
5. **Netzladen-Hook** (5) — `c.netzladen(parameters, surplus)`, die eigene
   Erreichbarkeit `reachable_netz` für das Ladeziel und `dc_constr` mit der
   Untergrenze je Slot.
6. **Solver-Status** (6) — die Prüfung nach `model.optimize()`.

Die Reserve-Korrektur wäre idealerweise upstream aufgehoben; ist sie das,
entfällt sie hier ersatzlos.
