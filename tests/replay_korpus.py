"""Archivierte Fahrpläne wieder in ``ScheduleInputs`` übersetzen.

Ein Eintrag des Fahrplan-Archivs (``schedule_archive.py``) trägt alles, was
``opt()`` gesehen hat — aber verteilt: PV, Verbrauch und Zeitachse stehen in
den Plan-Slots, der Rest in ``eingaben``. Hier wird beides wieder
zusammengesetzt, so dass ``schedule.solve()`` denselben Plan rechnet.

Die Eingänge haben das Raster von ``_grid_timestamps`` (erster Punkt auf
``start``, dann 30 min), die Slots 15 min. Die Stützpunkte liegen auf
Slotgrenzen; ``opt()`` übernimmt sie unverändert und interpoliert nur
dazwischen. Darum genügt es, an jedem Stützpunkt den Slot gleicher Zeit zu
nehmen.

Genutzt von ``test_replay_korpus.py`` und ``scripts/replay_korpus.py``.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import pathlib
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.eeg_energy_optimizer import schedule as sched

KORPUS = pathlib.Path(__file__).parent / "fixtures" / "replay"
ZONE = ZoneInfo("Europe/Vienna")

# Felder, deren Werte Datumsangaben sind.
_DATUMSFELDER = ("start",)


def eintrag_laden(pfad: pathlib.Path) -> dict[str, Any]:
    """Liest einen Eintrag — Archivformat (.json.gz) oder Korpus (.json)."""
    if pfad.suffix == ".gz":
        with gzip.open(pfad, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(pfad.read_text(encoding="utf-8"))


def korpus_dateien() -> list[pathlib.Path]:
    return sorted(KORPUS.glob("*.json.gz"))


def _stuetzpunkte(start: datetime, slots: list[dict]) -> list[datetime]:
    letzter = datetime.fromisoformat(slots[-1]["t"])
    horizont = letzter + timedelta(minutes=15) - start
    stunden = horizont.total_seconds() / 3600.0
    return sched._grid_timestamps(start, stunden)


def _wert_am(slots_nach_zeit: dict[datetime, dict], zeiten: list[datetime],
             stempel: datetime, spalte: str) -> float:
    slot = slots_nach_zeit.get(stempel)
    if slot is None:
        # Der letzte Stützpunkt liegt genau auf dem Horizontende, hinter dem
        # letzten Slot — dort gilt dessen Wert (opt() schneidet ab).
        frueher = [z for z in zeiten if z <= stempel]
        slot = slots_nach_zeit[frueher[-1]]
    return float(slot.get(spalte) or 0.0)


def inputs_aus_eintrag(eintrag: dict[str, Any]) -> sched.ScheduleInputs:
    """Baut die ``ScheduleInputs`` eines archivierten Laufs nach."""
    eingaben = dict(eintrag["eingaben"])
    slots = eintrag["plan"]["slots"]
    # Das Archiv kennt nur den festen Offset (+01:00/+02:00), nicht die Zone.
    # Über die Zeitumstellung rechnet opt() den Horizont damit anders (188
    # statt 192 Slots, 25.10.2026). Alle Anlagen liegen in AT/DE/CH — dieselben
    # Umstellungsregeln, also genügt eine Zone.
    start = datetime.fromisoformat(eingaben["start"]).astimezone(ZONE)

    felder = {f.name: f for f in dataclasses.fields(sched.ScheduleInputs)}
    werte: dict[str, Any] = {}
    for name, wert in eingaben.items():
        if name not in felder:
            continue  # Feld einer neueren Version, das es hier nicht gibt
        if name in _DATUMSFELDER and isinstance(wert, str):
            wert = datetime.fromisoformat(wert)
        werte[name] = wert

    if werte.get("eeg_bedarf"):
        werte["eeg_bedarf"] = {
            name: {int(k): float(v) for k, v in reihe.items()}
            for name, reihe in werte["eeg_bedarf"].items()
        }

    zeiten = [datetime.fromisoformat(s["t"]) for s in slots]
    nach_zeit = dict(zip(zeiten, slots))
    stempel = _stuetzpunkte(start, slots)

    laengen = {
        len(werte[n]) for n in (
            "min_production_kw", "eeg_bonus", "feedin_price_series",
            "consumption_price_series", "consumption_price_fest",
        ) if werte.get(n) is not None
    }
    if len(laengen) > 1:
        raise ValueError(f"Reihen verschieden lang: {sorted(laengen)}")
    if laengen:
        # Die Länge der archivierten Reihen ist die Wahrheit. Über die
        # Zeitumstellung zählt der Horizont in Stunden anders als die
        # Stützpunkte (25.10.2026: 95 statt 97) — nur das Raster ist fest.
        anzahl = laengen.pop()
        if anzahl > len(stempel):
            raise ValueError(f"Raster passt nicht: {len(stempel)} Stützpunkte, Reihen {anzahl}")
        stempel = stempel[:anzahl]

    werte["timestamps"] = stempel
    werte["consumption_kw"] = [
        _wert_am(nach_zeit, zeiten, t, "consumption") for t in stempel
    ]
    werte["production_kw"] = [_wert_am(nach_zeit, zeiten, t, "PV") for t in stempel]
    werte.setdefault("min_production_kw", None)
    werte["start"] = start
    # Seit 30.10.2026 (2.1.53) rechnet async_collect_inputs unter dem
    # Mindest-Ladestand vom gemessenen Ladestand aus. Ältere Archive tragen
    # den Boden noch über dem Ladestand — so gebaut, wie der heutige Code sie
    # bauen würde, nicht wie damals.
    soc = werte.get("soc_pct")
    boden = werte.get("min_soc_pct")
    if isinstance(soc, (int, float)) and isinstance(boden, (int, float)) and soc < boden:
        werte["min_soc_einstellung_pct"] = boden
        werte["min_soc_pct"] = max(0.0, float(soc))
        if werte.get("min_soc_basis_pct") is not None:
            werte["min_soc_basis_pct"] = werte["min_soc_pct"]
    return sched.ScheduleInputs(**werte)


def abweichung(alt: list[dict], neu: list[dict], spalte: str = "battery_p") -> float:
    """Größte Abweichung einer Spalte zwischen zwei Plänen (kW bzw. %)."""
    return max(
        (abs(float(a.get(spalte) or 0.0) - float(b.get(spalte) or 0.0))
         for a, b in zip(alt, neu)),
        default=0.0,
    )


# ---------------------------------------------------------------------------
# Korpus: Einträge anonymisieren
# ---------------------------------------------------------------------------

# Plan-Spalten, die der Korpus behält: der Loader braucht PV/Verbrauch/Zeit,
# der Vergleich die Batterie, das Netz und den Ladestand.
KORPUS_SPALTEN = ("t", "PV", "consumption", "battery_p", "grid_p", "soc", "feedin_price")


def anonymisieren(eintrag: dict[str, Any], kuerzel: str) -> dict[str, Any]:
    """Nur was das Nachrechnen braucht — ohne Namen und Einstellungen.

    Gemeinschaftsnamen werden zu G1, G2 (``eeg_tarife`` und ``eeg_bedarf``
    hängen über den Namen zusammen). Die Einstellungen fallen ganz weg, bis
    auf den Wechselrichtertyp: Alles, was ``solve()`` braucht, steht in den
    ``eingaben``.
    """
    eingaben = json.loads(json.dumps(eintrag["eingaben"]))
    namen: dict[str, str] = {}
    for tarif in eingaben.get("eeg_tarife") or []:
        alt = str(tarif.get("name") or "")
        namen.setdefault(alt, f"G{len(namen) + 1}")
        tarif["name"] = namen[alt]
    if eingaben.get("eeg_bedarf"):
        eingaben["eeg_bedarf"] = {
            namen.setdefault(alt, f"G{len(namen) + 1}"): reihe
            for alt, reihe in eingaben["eeg_bedarf"].items()
        }
    eingaben.pop("eeg_details", None)
    plan = eintrag["plan"]
    gewinn = plan.get("gewinn") or {}
    return {
        "anlage": kuerzel,
        "gespeichert": eintrag.get("gespeichert"),
        "version": eintrag.get("version"),
        "wechselrichter": (eintrag.get("einstellungen") or {}).get("inverter_type"),
        "eingaben": eingaben,
        "plan": {
            "slots": [{k: s.get(k) for k in KORPUS_SPALTEN} for s in plan["slots"]],
            "vorteil": gewinn.get("vorteil"),
        },
    }


# ---------------------------------------------------------------------------
# Varianten: Funktionen, die es zum Zeitpunkt des Archivs nicht gab oder die
# an dieser Anlage aus waren, auf die echten Reihen geschaltet
# ---------------------------------------------------------------------------

_ETA = 0.95  # HAConfig.ac_efficiency


def _billig_reihe(inputs: sched.ScheduleInputs, abschlag: float = 0.08) -> list[float]:
    """Bezugspreis mit zwei billigen Fenstern (01–05 und 11–15 Uhr) — wie
    ein Spot- oder Gemeinschaftstarif, ohne den Netzladen nichts kauft."""
    reihe = []
    for t in inputs.timestamps:
        billig = 1 <= t.hour < 5 or 11 <= t.hour < 15
        reihe.append(round(inputs.consumption_price - (abschlag if billig else 0.0), 5))
    return reihe


def _mit_netzladen(inputs: sched.ScheduleInputs, **extra: Any) -> sched.ScheduleInputs:
    werte: dict[str, Any] = {
        "consumption_price_series": _billig_reihe(inputs),
        "consumption_price_fest": [True] * len(inputs.timestamps),
        "netzladen_max_kw": inputs.battery_power_limit_kw,
    }
    werte.update(extra)
    return dataclasses.replace(inputs, **werte)


def _mit_spitzenreserve(
    inputs: sched.ScheduleInputs, grenze_kw: float = 3.0, auto_kw: float = 6.0
) -> sched.ScheduleInputs:
    """Spitzenreserve mit Nachladen aus dem Netz, wie ``async_collect_inputs``
    sie bildet — auf einem erfundenen Verlauf: das Haus der Anlage plus jeden
    Abend 18:00–19:30 ein Auto mit ``auto_kw``.

    Spiegelt ``_spitzenreserve_fuer_plan`` und den Block danach in
    ``async_collect_inputs``; ändert sich dort etwas, muss es hier mit.
    """
    from custom_components.eeg_energy_optimizer import spitzenreserve as sr

    start = inputs.start
    horizont_h = (inputs.timestamps[-1] - start).total_seconds() / 3600.0
    viertel = sched._grid_timestamps(start, horizont_h, 15)
    ueberschuss = sr.ueberschuss_je_slot(
        viertel, inputs.timestamps, inputs.production_kw, inputs.consumption_kw
    )
    # Haus − PV je Wanduhr-Viertelstunde aus den ersten 24 h der Anlage.
    netto: dict[tuple[int, int], float] = {}
    for t, pv, haus in zip(inputs.timestamps, inputs.production_kw, inputs.consumption_kw):
        for m in (0, 15):
            netto.setdefault((t.hour, (t.minute // 30) * 30 + m), haus - pv)
    verlauf: dict[datetime, float] = {}
    heute = start.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    for tag in range(1, 29):
        basis = heute - timedelta(days=tag)
        for k in range(96):
            t = basis + timedelta(minutes=15 * k)
            wert = netto.get((t.hour, t.minute), 0.5)
            if 18 * 60 <= t.hour * 60 + t.minute < 19 * 60 + 30:
                wert += auto_kw
            verlauf[t] = wert
    grenzen = [grenze_kw] * len(viertel)
    entlade = inputs.battery_power_limit_kw
    kurve_kwh, tage = sr.reservekurve(verlauf, viertel, ueberschuss, grenzen, entlade)
    min_pct, max_pct = sr.reserve_grenzen_pct({})
    kurve_pct = sr.kurve_in_pct(kurve_kwh, inputs.battery_capacity_kwh, min_pct, max_pct)
    plan_pct = sr.plan_reserve_pct(kurve_pct, ueberschuss, viertel) or 0.0
    info: dict[str, Any] = {
        "grenze_kw": grenze_kw,
        "entlade_kw": entlade,
        "kurve": [[t.isoformat(), p] for t, p in zip(viertel, kurve_pct)],
        "grenzen": grenzen,
        "plan_pct": round(plan_pct, 1),
        "im_ueberschuss": bool(ueberschuss and ueberschuss[0]),
        "min_pct": min_pct,
        "max_pct": max_pct,
        "tage": tage,
        "statistik": True,
    }
    # Hat die Anlage schon eine Reserve, steckt sie in min_soc_pct — die
    # Variante ersetzt sie, statt sie ein zweites Mal aufzusetzen.
    basis = (
        inputs.min_soc_basis_pct if inputs.min_soc_basis_pct is not None
        else inputs.min_soc_pct
    )
    reserve = plan_pct
    if inputs.soc_pct is not None:
        reserve = min(reserve, max(0.0, inputs.soc_pct - basis))
    neu_min = min(basis + reserve, max(basis, inputs.max_soc_pct - sched.SOC_BAND_MIN_PCT))
    info["plan_wirksam_pct"] = round(neu_min - basis, 1)
    ladeziel = inputs.ladeziel_pct
    if info["im_ueberschuss"] and plan_pct > 0:
        ladeziel = max(ladeziel, min(inputs.max_soc_pct, basis + plan_pct))
    return dataclasses.replace(
        inputs,
        spitzenreserve=info,
        min_soc_pct=neu_min,
        min_soc_basis_pct=basis,
        ladeziel_pct=ladeziel,
        consumption_price_series=_billig_reihe(inputs),
        consumption_price_fest=[True] * len(inputs.timestamps),
        spitzenreserve_netz_kw=inputs.battery_power_limit_kw,
    )


def _dunkel(inputs: sched.ScheduleInputs) -> sched.ScheduleInputs:
    """Ein Novembertag: ein Zehntel der PV, Mindestladung 50 %."""
    faktor = 0.1
    return _mit_netzladen(
        dataclasses.replace(
            inputs,
            production_kw=[v * faktor for v in inputs.production_kw],
            min_production_kw=(
                None if inputs.min_production_kw is None
                else [v * faktor for v in inputs.min_production_kw]
            ),
        ),
        netzladen_abend_pct=50.0,
    )


def _mit_heizstab(inputs: sched.ScheduleInputs) -> sched.ScheduleInputs:
    return dataclasses.replace(
        inputs,
        heizstab_max_kw=3.0,
        heizstab_waermewert=0.15,
        heizstab_budget_kwh=6.0,
        heizstab_puffer_liter=500.0,
        heizstab_temp_c=50.0,
        heizstab_maxtemp_c=70.0,
    )


VARIANTEN = {
    "archiv": lambda i: i,
    # Netzgrenze unter Batterie- plus Hausleistung, damit sie bindet.
    "netzladen": lambda i: _mit_netzladen(i, netzladen_netzgrenze_kw=3.0),
    # 90 %: über dem, was diese Anlagen im Herbst von selbst erreichen —
    # sonst bindet die Mindestladung nie.
    "mindestladung": lambda i: _mit_netzladen(i, netzladen_abend_pct=90.0),
    "dunkel": _dunkel,
    "spitzenreserve_netz": _mit_spitzenreserve,
    # Reserve an einem dunklen Tag: muss aus dem Netz nachladen, unter der
    # Grenze der Spitzenkappung.
    "spitzenreserve_dunkel": lambda i: _mit_spitzenreserve(
        dataclasses.replace(_dunkel(i), netzladen_max_kw=0.0, netzladen_abend_pct=0.0),
        auto_kw=8.0,
    ),
    "heizstab": _mit_heizstab,
}


# ---------------------------------------------------------------------------
# Regeln, die jeder Plan einhalten muss
# ---------------------------------------------------------------------------

_TOL_KW = 0.05
_TOL_PCT = 0.5


def verstoesse(inputs: sched.ScheduleInputs, plan: dict[str, Any]) -> list[str]:
    """Alle Regelverstöße eines gerechneten Plans, als lesbare Sätze.

    Nur Aussagen, die unabhängig von Preisen und Prognose immer gelten
    müssen — keine Sollwerte. Wo eine Regel aus einem Vorfall stammt, steht
    er dabei, damit ein roter Test gleich sagt, was wieder kaputt ist.
    """
    fehler: list[str] = []
    slots = plan.get("slots") or []
    if len(slots) < 96:
        return [f"zu kurzer Plan: {len(slots)} Slots"]

    def melde(slot: dict, text: str) -> None:
        if len(fehler) < 20:
            fehler.append(f"{slot['t']}: {text}")

    soc0 = inputs.soc_pct if inputs.soc_pct is not None else inputs.min_soc_pct
    boden = min(inputs.min_soc_pct, soc0) - _TOL_PCT
    deckel = max(inputs.max_soc_pct, soc0) + _TOL_PCT
    netz_frei = inputs.netzladen_max_kw > 0 or inputs.spitzenreserve_netz_kw > 0
    netz_max = max(inputs.netzladen_max_kw, inputs.spitzenreserve_netz_kw)
    fest = dict(zip(inputs.timestamps, inputs.consumption_price_fest or []))
    reserve = inputs.spitzenreserve or {}
    grenze_je_t = {
        t: g for (t, _), g in zip(reserve.get("kurve") or [], reserve.get("grenzen") or [])
    }
    heiz_tag: dict[str, float] = {}

    for slot in slots:
        soc = float(slot["soc"])
        pv = float(slot.get("PV") or 0.0)
        haus = float(slot.get("consumption") or 0.0)
        netz = float(slot.get("grid_p") or 0.0)
        bat = float(slot.get("battery_p") or 0.0)
        heiz = float(slot.get("heizstab") or 0.0)
        laden_netz = sched.netzladung_kw(slot, _ETA)

        # Grundregel: der Ladestand bleibt im Band.
        if not boden <= soc <= deckel:
            melde(slot, f"Ladestand {soc} % außerhalb [{boden:.1f}, {deckel:.1f}]")
        # Einspeisegrenze (auch die abgesenkte von „netzdienlich").
        if netz > inputs.feedin_limit_kw + _TOL_KW:
            melde(slot, f"Einspeisung {netz:.2f} kW über der Grenze {inputs.feedin_limit_kw}")
        # Batterieleistung — opt() rechnet DC, die Grenze ist AC.
        if abs(bat) > inputs.battery_power_limit_kw / _ETA + _TOL_KW:
            melde(slot, f"Batterie {bat:.2f} kW über {inputs.battery_power_limit_kw} kW")
        # Ansfelden 21.09.2026: Reserve und Ladeziel dürfen nie einen Zukauf
        # in die Batterie erzwingen, solange Netzladen aus ist.
        if not netz_frei and laden_netz > _TOL_KW:
            melde(slot, f"lädt {laden_netz:.2f} kW aus dem Netz ohne Freigabe")
        if netz_frei and laden_netz > _TOL_KW:
            if laden_netz > netz_max + _TOL_KW:
                melde(slot, f"Netzladung {laden_netz:.2f} kW über {netz_max} kW")
            if fest and fest.get(datetime.fromisoformat(slot["t"])) is False:
                melde(slot, "Netzladung auf einem fortgeschriebenen Preis")
            grenze_netz = inputs.netzladen_netzgrenze_kw
            if grenze_netz > 0 and -netz > grenze_netz + _TOL_KW:
                melde(slot, f"Netzbezug {-netz:.2f} kW über der Netzgrenze {grenze_netz}")
            grenze = grenze_je_t.get(slot["t"])
            if grenze is not None and -netz > grenze + _TOL_KW:
                melde(slot, f"Reserve-Nachladen: Bezug {-netz:.2f} kW über der "
                            f"Spitzenkappung {grenze}")
            # Kauf und Verkauf zugleich — der Preisdeckel soll das verhindern.
            if netz > _TOL_KW:
                melde(slot, f"lädt {laden_netz:.2f} kW aus dem Netz und speist "
                            f"{netz:.2f} kW ein")
        # Grünbach 14.09.2026: der Heizstab lebt nur vom PV-Überschuss.
        if heiz > _TOL_KW:
            if heiz > inputs.heizstab_max_kw + _TOL_KW:
                melde(slot, f"Heizstab {heiz:.2f} kW über {inputs.heizstab_max_kw}")
            if heiz > max(0.0, pv - haus / _ETA) + 0.1:
                melde(slot, f"Heizstab {heiz:.2f} kW ohne PV-Überschuss "
                            f"(PV {pv:.2f}, Haus {haus:.2f})")
            tag = slot["t"][:10]
            heiz_tag[tag] = heiz_tag.get(tag, 0.0) + heiz * inputs.time_res_s / 3600.0

    for tag, kwh in heiz_tag.items():
        if inputs.heizstab_budget_kwh > 0 and kwh > inputs.heizstab_budget_kwh / _ETA + 0.1:
            fehler.append(f"{tag}: Heizstab {kwh:.2f} kWh über dem Tagesbudget "
                          f"{inputs.heizstab_budget_kwh}")

    # 2.1.62–64: Die Mindestladung am Abend muss am Termin stehen. Mit
    # Netzladen ist sie erreichbar, es sei denn, die Zeit bis dahin reicht
    # selbst bei voller Leistung nicht.
    abend = plan.get("netzladen_abend") or {}
    for termin in abend.get("termine") or []:
        # Am Planungsrand gilt der abgesenkte Wert (``gefordert_pct``): Der
        # feste Endstand von opt() lässt dort nicht mehr zu.
        ziel = min(float(abend["ziel_pct"]), float(termin.get("gefordert_pct", 100.0)))
        if float(termin["geplant_pct"]) < ziel - 1.0:
            fehlt_kwh = (ziel - float(soc0)) / 100.0 * inputs.battery_capacity_kwh
            if fehlt_kwh <= _ladbar_bis_kwh(inputs, slots, termin["t"]):
                fehler.append(
                    f"{termin['t']}: Mindestladung {ziel} % verfehlt "
                    f"(geplant {termin['geplant_pct']} %)"
                )
    return fehler


def _ladbar_bis_kwh(inputs: sched.ScheduleInputs, slots: list[dict], bis: str) -> float:
    """Obergrenze dessen, was die Batterie bis zum Ende von Slot ``bis`` laden kann.

    Je Slot PV plus erlaubter Netzbezug neben dem Haus, gedeckelt durch die
    Batterieleistung — ohne Verluste und ohne Entladung, also großzügig. Der
    Netzbezug ist durch die Netzgrenze des Netzladens und die Grenze der
    Spitzenkappung (minus Sicherheit) begrenzt: An einer Anlage mit 4 kW
    Grenze kommt eine 6-kW-Batterie in vier Stunden nicht von 19 auf 90 %
    (Fronius 02.11.2026), und das ist kein Fehler des Plans.
    """
    from custom_components.eeg_energy_optimizer.spitzenreserve import SICHERHEIT_KW

    reserve = inputs.spitzenreserve or {}
    grenzen = {t: g for (t, _), g in zip(reserve.get("kurve") or [], reserve.get("grenzen") or [])}
    standard = float(reserve.get("grenze_kw") or 0.0)
    p2e = inputs.time_res_s / 3600.0
    summe = 0.0
    for slot in slots:
        netz = float("inf")
        if inputs.netzladen_netzgrenze_kw > 0:
            netz = inputs.netzladen_netzgrenze_kw
        if reserve:
            netz = min(netz, grenzen.get(slot["t"], standard) - SICHERHEIT_KW)
        haus = float(slot.get("consumption") or 0.0)
        pv = float(slot.get("PV") or 0.0)
        laden = min(inputs.battery_power_limit_kw, pv + max(0.0, netz - haus))
        summe += max(0.0, laden) * p2e
        if slot["t"] == bis:
            break
    return summe
