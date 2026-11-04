"""Untergrenzen des Ladestands je Slot — eine Rechnung für alle.

Drei Wünsche verlangen, dass die Batterie zu bestimmten Zeiten mindestens
einen Stand hat, und kommen über den Ladeziel-Hook in ``opt()``
(``chamo/README.md`` § 3, § 5):

* **Ladeziel** — am Ende der PV-Zeit, davor rückwärts auf dem vorsichtigen
  Pfad verteilt. Kauft nie, höchstens über wirtschaftliches Netzladen.
* **Mindestladung am Abend** — beim Sonnenuntergang jedes Tages.
  Pflicht, notfalls aus dem Netz.
* **Spitzenreserve aus dem Netz** — vor jeder Viertelstunde, die zur Spitze
  beiträgt. Pflicht, notfalls aus dem Netz.

Der Mindest-Ladestand selbst (mit Backup und der Spitzenreserve von jetzt)
ist keine Forderung hier, er steckt als fehlende Kapazität im Modell
(``HAConfig.__init__``).

Bis 2.1.64 lag das in elf Methoden von ``HAConfig``, die einander riefen,
mit drei Erreichbarkeitsrechnungen und zwei Regeln für den Planungsrand.
Die Fehler, die der Replay-Korpus fand, lagen fast alle in diesen Fugen.
Hier steht dieselbe Rechnung einmal, ohne pandas und ohne ``hass``:
``HAConfig`` füllt ``Lage`` aus den gerasterten Parametern von ``opt()``,
die Plan-Ausgabe liest dasselbe Ergebnis.

Alle Stände in kWh im Modellfenster (über dem Boden, bis zum Deckel) und
für das ENDE eines Slots, wie ``battery_free[i]`` in ``opt_highs``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property

# Haralds Innenwiderstand je Stufe (``opt_highs``: ``c.battery_resistance``).
BATTERIE_WIDERSTAND = 0.04

# Die Spitzenreserve fordert nur vor einer Viertelstunde, in der die Kurve
# um mehr als das fällt — kleinere Stufen sind Rundung, kein Bedarf.
RESERVE_NETZ_BEDARF_MIN_KWH = 0.05
# Die Mindestladung rechnet rückwärts vom Termin mit nur diesem Anteil der
# erlaubten Netzladeleistung (die PV zählt voll). Mit der vollen Leistung
# lud der Plan genau in den letzten Viertelstunden vor dem Termin — ohne
# Puffer, wenn das Haus mehr braucht und unter der Netzgrenze weniger für
# die Batterie bleibt (Traun 03.11.2026: 6 kWh fehlten, geplant 14:45–16:30
# mit 2,9 kW, keine Viertelstunde Luft). Je größer der Fehlbetrag gegenüber
# der erwarteten PV, desto früher beginnt das Laden. Entscheidung 03.11.2026.
MINDESTLADUNG_LEISTUNG_ANTEIL = 0.7
# Ersatzstandort für den Sonnenuntergang, wenn keiner bekannt ist (alte
# Archive, Tests): Linz. Innerhalb Österreichs liegt der Untergang damit
# höchstens etwa 20 Minuten daneben — für den Termin einer Viertelstunde
# reicht das, für eine echte Anlage kommt er aus Home Assistant.
ERSATZ_BREITE = 48.3
ERSATZ_LAENGE = 14.3


# ---------------------------------------------------------------------------
# Batteriephysik (Haralds gestufte Verluste)
# ---------------------------------------------------------------------------


def batterie_verluste_kw(p_kw: float, kapazitaet_kwh: float) -> float:
    """Innenwiderstandsverluste in kW zu einer Batterieleistung — Haralds Modell.

    ``opt_highs.py`` kann keinen echten Innenwiderstand (quadratisch) linear
    abbilden und nimmt zwei Stufen: bis 0,1 C ist (Ent-)Laden verlustfrei,
    zwischen 0,1 C und 0,2 C gehen ``battery_resistance`` (4 %) der Leistung
    darüber verloren, oberhalb von 0,2 C das Doppelte. „C" ist die Kapazität
    in kWh als Leistung in kW gelesen. Die Verluste gehen vom DC-Bus ab, die
    Batterie selbst sieht die volle Leistung — beim Laden kommt also weniger
    von der PV an, beim Entladen weniger beim Haus. Richtung ist egal.
    """
    r = BATTERIE_WIDERSTAND
    stufe = kapazitaet_kwh / 10.0
    p = abs(p_kw)
    hoch1 = min(max(p - stufe, 0.0), stufe)
    hoch2 = max(p - 2.0 * stufe, 0.0)
    return r * hoch1 + 2.0 * r * hoch2


def ladeleistung_aus_dc(angebot_kw: float, kapazitaet_kwh: float) -> float:
    """Umkehrung von ``batterie_verluste_kw`` fürs Laden: die Batterieleistung
    p, für die ``p + Verluste(p)`` genau das DC-Angebot ausschöpft."""
    r = BATTERIE_WIDERSTAND
    stufe = kapazitaet_kwh / 10.0
    if angebot_kw <= stufe:
        return max(angebot_kw, 0.0)
    if angebot_kw <= stufe * (2.0 + r):          # bis p = 0,2 C
        return (angebot_kw + r * stufe) / (1.0 + r)
    return (angebot_kw + 3.0 * r * stufe) / (1.0 + 2.0 * r)


def entladeleistung_fuer_dc(bedarf_kw: float, kapazitaet_kwh: float) -> float:
    """Umkehrung fürs Entladen: die Batterieleistung p, für die
    ``p − Verluste(p)`` genau den DC-Bedarf deckt."""
    r = BATTERIE_WIDERSTAND
    stufe = kapazitaet_kwh / 10.0
    if bedarf_kw <= stufe:
        return max(bedarf_kw, 0.0)
    if bedarf_kw <= stufe * (2.0 - r):           # bis p = 0,2 C
        return (bedarf_kw - r * stufe) / (1.0 - r)
    return (bedarf_kw - 3.0 * r * stufe) / (1.0 - 2.0 * r)


# ---------------------------------------------------------------------------
# Termine
# ---------------------------------------------------------------------------


def pv_enden(werte: list[float], tage: list) -> list[int]:
    """Je Kalendertag der letzte Slot mit Überschuss — das Ende der PV-Zeit.

    Nur wenn danach am selben Tag noch ein Slot im Horizont liegt (sonst ist
    es das Ende der Rechnung, nicht der Sonne) und nicht nur eine Wolke
    dazwischen war. ``werte`` ist der Überschuss je Slot, ``tage`` das Datum.
    Gemeinsam für Ladeziel und Netzladen.
    """
    enden: list[int] = []
    for i in range(len(werte)):
        if werte[i] <= 0:
            continue
        naechster = i + 1
        if naechster >= len(werte) or tage[naechster] != tage[i]:
            continue  # Horizont- oder Tagesende, kein PV-Ende
        if werte[naechster] > 0:
            continue  # Überschuss geht weiter
        if any(werte[k] > 0 for k in range(naechster, len(werte)) if tage[k] == tage[i]):
            continue  # nur eine Wolke, später am Tag kommt noch Sonne
        enden.append(i)
    return enden


def sonnenuntergang(tag, breite: float, laenge: float, zone) -> datetime | None:
    """Sonnenuntergang an ``tag`` (Ortszeit ``zone``) — NOAA, Höhe −0,833°.

    Rückfall, wenn Home Assistant keinen liefert; auf etwa zwei Minuten
    genau (Traun 03.11.2026: 16:43 statt 16:41). Mit ``sonnenstand`` aus der
    PV-Kalibrierung, halbiert zwischen Mittag und 23 Uhr. None, wenn die
    Sonne an dem Tag nicht untergeht.
    """
    from .pvprognose.kalibrierung import sonnenstand

    unten = -0.833
    von = datetime(tag.year, tag.month, tag.day, 12, 0, tzinfo=zone)
    bis = datetime(tag.year, tag.month, tag.day, 23, 0, tzinfo=zone)
    if sonnenstand(von, breite, laenge)[1] <= unten or sonnenstand(bis, breite, laenge)[1] > unten:
        return None
    for _ in range(30):
        mitte = von + (bis - von) / 2
        if sonnenstand(mitte, breite, laenge)[1] > unten:
            von = mitte
        else:
            bis = mitte
    return von.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Eingänge und Rechnung
# ---------------------------------------------------------------------------


@dataclass
class Akku:
    """Die Batterie im Modellfenster, wie ``HAConfig`` sie ``opt()`` gibt."""

    kapazitaet: float          # kWh zwischen Boden und Deckel
    frei: float                # kWh bis zum Deckel
    leistung: float            # kW, ``battery_power_limit``
    ac_limit: float            # kW
    eta: float                 # ``ac_efficiency``
    p2e: float                 # h je Slot

    @property
    def stand(self) -> float:
        return self.kapazitaet - self.frei


@dataclass
class Vorgaben:
    """Was eingestellt ist — alle Wünsche, Grenzen und Preise außer der Reihe."""

    ladeziel_kwh: float = 0.0
    abend_kwh: float = 0.0
    netzladen_max_kw: float = 0.0
    netzladen_netzgrenze_kw: float = 0.0
    netzladen_vorteil: float = 0.0
    battery_cost: float = 0.0
    spitzenreserve_netz_kw: float = 0.0
    # Spitzenreserve: Kurve in % je Viertelstunde (Epoche → %), die Grenze
    # der Spitzenkappung je Viertelstunde (Epoche → kW) und die Standard-
    # grenze. ``reserve_aktiv`` heißt: Die Batterie-Stufe läuft, ihre Grenze
    # gilt für jede Netzladung.
    reserve_aktiv: bool = False
    reserve_pct: dict = field(default_factory=dict)
    kappung_kw: dict = field(default_factory=dict)
    kappung_standard_kw: float = 0.0
    sicherheit_kw: float = 0.3
    # Für die Reserve in kWh im Modellfenster: echte Kapazität, Boden ohne
    # Reserve und Boden des Modells in Prozent.
    kapazitaet_echt_kwh: float = 0.0
    boden_basis_pct: float | None = None
    boden_pct: float = 0.0
    # Termine der Mindestladung; None = am Ersatzstandort rechnen.
    sonnenuntergaenge: list[datetime] | None = None


@dataclass
class Lage:
    """Die gerasterten Reihen eines ``opt()``-Laufs, je Slot."""

    stempel: list[datetime]
    ueberschuss: list[float]          # DC: PV − Haus/η
    pv: list[float]                   # DC-Erzeugung
    haus: list[float]                 # AC
    einspeisegrenze: list[float]      # kW
    preise: list[float] | None = None  # Bezugspreis €/kWh
    fest: list[bool] | None = None     # Preis veröffentlicht?
    vorsichtig: list[float] | None = None  # Überschuss auf dem p10-Pfad


class Forderungen:
    """Termine, Forderungen und Netzgrenzen eines Laufs — einmal gerechnet.

    ``ziel`` geht als Mindestinhalt je Slot in den Ladeziel-Hook,
    ``netzladen`` als Untergrenze von ``dc_p`` in den Netzlade-Hook.
    """

    def __init__(self, lage: Lage, akku: Akku, vorgaben: Vorgaben) -> None:
        self.lage = lage
        self.akku = akku
        self.v = vorgaben
        self.n = len(lage.ueberschuss)
        self.tage = [s.date() for s in lage.stempel]

    # -- Planungsrand ---------------------------------------------------

    @cached_property
    def abbau_bis_ende(self) -> tuple[list[float], float]:
        """(Abbau ab Slot j bis zum Horizontende in kWh, Endstand in kWh).

        Wie viel die Batterie nach einem Slot noch abgeben kann, bevor der
        Horizont endet, und auf welchen Stand ``opt()`` sie dort festlegt.
        Eine Forderung über ``Endstand + rest[i + 1]`` am Ende von Slot ``i``
        lässt das LP ohne Lösung: Es müsste schneller entladen, als die
        Batterie kann.
        """
        a = self.akku
        werte = self.lage.ueberschuss
        verbrauch = self.lage.haus
        grenze = self.lage.einspeisegrenze
        # Was die Batterie je Slot höchstens abgeben kann, ohne zu laden:
        # in Slots ohne Überschuss den Fehlbetrag des Hauses plus die
        # erlaubte Einspeisung, beides auf der DC-Seite und gedeckelt durch
        # die Leistungsgrenze. Slots mit Überschuss zählen nicht — dort
        # versorgt die PV das Haus.
        abbau = [
            0.0 if werte[j] > 0 else min(
                a.leistung,
                -werte[j] + max(0.0, min(grenze[j], a.ac_limit - verbrauch[j])) / a.eta,
            ) * a.p2e
            for j in range(self.n)
        ]
        rest = [0.0] * (self.n + 1)
        for j in range(self.n - 1, -1, -1):
            rest[j] = rest[j + 1] + abbau[j]
        # Der Endstand, wie opt() ihn setzt (chamo/README.md § 4): halb, aber
        # nie mehr, als die Batterie aus PV allein erreicht.
        stand = a.stand
        for w in werte:
            if w > 0:
                stand = min(
                    a.kapazitaet,
                    stand + min(ladeleistung_aus_dc(w, a.kapazitaet), a.leistung) * a.p2e,
                )
        return rest, min(a.kapazitaet / 2.0, stand)

    @cached_property
    def termine_ladeziel(self) -> list[int]:
        """Je Kalendertag das Ende der PV-Zeit (``pv_enden``) — nur mit Ladeziel.

        Ein Termin am Planungsrand, von dem der Plan nicht mehr auf den
        Endstand käme, bleibt stehen — seine Forderung senkt ``_rand`` wie
        jede andere (Entscheidung 03.11.2026; bis 2.1.64 wurde er gestrichen).
        """
        if self.v.ladeziel_kwh <= 0 or self.n == 0:
            return []
        return pv_enden(self.lage.ueberschuss, self.tage)

    @cached_property
    def termine_abend(self) -> list[int]:
        """Je Kalendertag der Slot, in dem die Sonne untergeht — der Termin
        der Mindestladung am Abend, gefordert für sein Ende.

        „Abend = Sonnenuntergang" (Entscheidung 03.11.2026). Bis dahin war der
        Termin das Ende der PV-Zeit (letzter Überschuss-Slot), an dunklen
        Tagen das Ende der Erzeugung. Endete der Überschuss schon zu Mittag,
        lag der Termin dort, war um 13:15 vorbei, und für den Abend forderte
        nichts mehr — Traun 03.11.2026 stand bei 21 % statt 60 %. Dazu kamen
        drei Sonderregeln (dunkler Tag, winziger Überschuss, Überschuss vor
        dem Start), die mit dem Sonnenuntergang alle entfallen; auch ein Tag
        ganz ohne PV hat jetzt einen Termin.

        Ohne ``abend_kwh`` keine. Ein Untergang vor dem Horizontstart oder
        hinter seinem Ende hat keinen Slot und damit keinen Termin.
        """
        if self.v.abend_kwh <= 0 or self.n == 0:
            return []
        stempel = self.lage.stempel
        untergaenge = self.v.sonnenuntergaenge
        if untergaenge is None:
            zone = stempel[0].tzinfo
            tage = sorted(set(self.tage))
            untergaenge = [
                u for u in (
                    sonnenuntergang(t, ERSATZ_BREITE, ERSATZ_LAENGE, zone) for t in tage
                ) if u is not None
            ]
        slot_s = self.akku.p2e * 3600.0
        termine: list[int] = []
        for untergang in untergaenge:
            u = untergang.timestamp()
            for k, t in enumerate(stempel):
                if t.timestamp() <= u < t.timestamp() + slot_s:
                    # Auch kurz vor dem Untergang bleibt der Termin. Bis 2.1.66
                    # entfiel er, wenn sein Slot in weniger als einer Stunde
                    # endete — und brach damit eine laufende Netzladung ab:
                    # Traun 03.11.2026, 16:00, Ladestand 30 % statt 60 %.
                    # Die letzte Stunde vor Sonnenuntergang ist genau die Zeit,
                    # in der notfalls noch geladen werden muss.
                    termine.append(k)
                    break
        return sorted(set(termine))

    def _rand(self, forderung: list[float]) -> list[float]:
        """Jede Forderung gekappt auf das, wovon der Plan bis zum Horizontende
        noch auf den Endstand kommt (``abbau_bis_ende``).

        Der Endstand ist ein Kunstgriff von ``opt()`` (halb voll, an dunklen
        Tagen weniger), keine echte Anforderung. Eine Forderung kurz davor,
        die die Batterie bis dahin nicht wieder abbauen kann, ließe das LP
        ohne Lösung (Replay-Korpus, Huawei und SolaX 01.11.2026: kein Plan,
        Failsafe). Gekappt statt gestrichen: So viel, wie zusammenpasst,
        gilt weiter; jeder Planlauf rückt den Rand ohnehin nach hinten.
        """
        rest, ende = self.abbau_bis_ende
        ergebnis = []
        for k, f in enumerate(forderung):
            grenze = ende + rest[k + 1]
            if f > grenze + 1e-9:
                self.am_rand_abgesenkt.add(k)
                f = grenze
            ergebnis.append(f)
        return ergebnis

    @cached_property
    def am_rand_abgesenkt(self) -> set[int]:
        """Slots, deren Forderung ``_rand`` abgesenkt hat — für Anzeige und Grund."""
        return set()

    # -- Erreichbarkeit -------------------------------------------------

    def erreichbar(self, untergrenze: list[float] | None) -> list[float]:
        """Stand am Ende jedes Slots, wenn jede Gelegenheit zum Laden genutzt wird.

        Wie die Erreichbarkeit in ``opt_highs`` (mit denselben gestuften
        Verlusten): Überschuss lädt, Defizit entlädt, und wo ``untergrenze``
        unter null liegt, lädt die Batterie aus PV plus Netz. Ohne
        ``untergrenze`` also das ohne Zukauf Erreichbare.

        Gedeckelt wird wie dort nur das Laden, nicht die Entladung
        (``min(step, max_step)`` in ``opt_highs``): Eine hier gedeckelte
        Entladung hielt den Stand höher als das LP und ließ eine Forderung
        stehen, die ``opt()`` danach doch kappen musste.
        """
        a = self.akku
        lb = untergrenze if untergrenze is not None else [0.0] * self.n
        stand = a.stand
        ergebnis = []
        for w, p, u in zip(self.lage.ueberschuss, self.lage.pv, lb):
            if u < 0:
                schritt = min(ladeleistung_aus_dc(p - u, a.kapazitaet), a.leistung)
            elif w > 0:
                schritt = min(ladeleistung_aus_dc(w, a.kapazitaet), a.leistung)
            else:
                schritt = -entladeleistung_fuer_dc(-w, a.kapazitaet)
            stand = min(a.kapazitaet, max(0.0, stand + schritt * a.p2e))
            ergebnis.append(stand)
        return ergebnis

    # -- Netzgrenzen ----------------------------------------------------

    def netz_erlaubt_kw(self, max_kw: float) -> list[float]:
        """Was die Batterie je Slot höchstens aus dem Netz ziehen darf (kW, AC).

        Eine Regel für beide Wege, wirtschaftliches Netzladen und
        Pflichtladung: höchstens ``max_kw``, und den Zähler (Haus + Ladung)
        weder über die Netzgrenze des Netzladens noch — mit der Batterie-Stufe
        der Spitzenkappung — über deren Grenze je Slot minus
        ``sicherheit_kw``. Dieselbe Kappung macht der Executor bei jeder
        Netzladung. Bis 2.1.64 kannte nur die Pflichtladung die Grenze der
        Spitzenkappung; das wirtschaftliche Netzladen plante darüber, der
        Executor kappte, und die Batterie lud weniger als geplant
        (Replay-Korpus, Huawei 02.11.2026: 4 kW Bezug bei 3 kW Grenze).
        """
        return [kw for kw, _, _ in self.netz_begrenzung(max_kw)]

    def netz_begrenzung(self, max_kw: float) -> list[tuple[float, str | None, float]]:
        """Je Slot (erlaubte Netzladung kW, bindende Grenze, deren Wert in kW).

        Die Grenze ist ``"kappung"`` (Spitzenkappung), ``"netzgrenze"``
        (Netzgrenze des Netzladens) oder None (nur die Ladeleistung) — für
        die Begründung im Plan.
        """
        v = self.v
        ergebnis = []
        for t, h in zip(self.lage.stempel, self.lage.haus):
            erlaubt, quelle, wert = max_kw, None, max_kw
            if v.reserve_aktiv:
                grenze = v.kappung_kw.get(int(t.timestamp()), v.kappung_standard_kw)
                if grenze - v.sicherheit_kw - h < erlaubt:
                    erlaubt, quelle, wert = grenze - v.sicherheit_kw - h, "kappung", grenze
            if v.netzladen_netzgrenze_kw > 0 and v.netzladen_netzgrenze_kw - h < erlaubt:
                erlaubt, quelle, wert = (
                    v.netzladen_netzgrenze_kw - h, "netzgrenze", v.netzladen_netzgrenze_kw
                )
            ergebnis.append((max(0.0, erlaubt), quelle, wert))
        return ergebnis

    def begrenzung_bis(self, termin: int) -> dict | None:
        """Was die Netzladung von jetzt bis zum Ende von Slot ``termin``
        deckelt — für die Telemetrie einer verfehlten Mindestladung.

        None, wenn keine Grenze am Zähler greift (nur die Ladeleistung).
        Sonst ``durch`` (``"kappung"`` / ``"netzgrenze"``, die öfter bindende),
        deren Wert, in wie vielen der Slots sie band, und ``moeglich_kwh``:
        was bis zum Termin höchstens aus dem Netz in die Batterie konnte, mit
        der Hausprognose. Wolfsteiner 03.11.2026: Haus 4,5 kW Dauerlast neben
        einer Spitzenkappung von 5 kW — die Mindestladung 50 % war nicht zu
        laden, die Meldung sah aus wie ein Versagen der Steuerung.
        """
        max_kw = max(float(self.v.spitzenreserve_netz_kw or 0.0),
                     float(self.v.netzladen_max_kw or 0.0))
        if max_kw <= 0:
            return None
        teil = self.netz_begrenzung(max_kw)[: termin + 1]
        gebunden = [(q, w) for _, q, w in teil if q is not None]
        if not gebunden:
            return None
        durch = max(("kappung", "netzgrenze"), key=lambda q: sum(g == q for g, _ in gebunden))
        return {
            "durch": durch,
            "grenze_kw": round(next(w for q, w in gebunden if q == durch), 2),
            "slots": len(gebunden),
            "von": len(teil),
            "moeglich_kwh": round(sum(kw for kw, _, _ in teil) * self.akku.p2e, 2),
        }

    def _fest(self) -> list[bool]:
        return list(self.lage.fest) if self.lage.fest is not None else [True] * self.n

    @cached_property
    def wirtschaftlich(self) -> list[float] | None:
        """Untergrenze von ``dc_p`` je Slot (kW, DC, ≤ 0) — oder None.

        Wo die Grenze unter null liegt, darf das LP aus dem Netz laden,
        höchstens ``netzladen_max_kw`` (AC). None heißt: nirgends — dann ist
        das Modell Zeile für Zeile das ohne Netzladen.

        Erlaubt ist ein Slot vor dem Ende der PV-Zeit eines Tages (ab dem
        vorigen PV-Ende), wenn Netzstrom dort billiger ist als der Strom, den
        die Batterie danach ersetzt: der nach Hauslast gewichtete Bezugspreis
        der Nacht bis zum nächsten Überschuss. Billiger heißt nach beiden
        Wandlungen (AC→DC→AC, ``eta``²), Alterung (``battery_cost``) und
        Mindestvorteil (``netzladen_vorteil``). Ein Slot mit
        fortgeschriebenem Börsenpreis ist nie erlaubt.

        Der Vergleich gilt je kWh. Ob die Nacht die Energie auch braucht,
        entscheidet er nicht — das Ladeziel darf diese Slots nutzen und
        füllt dann bis zum Ziel, auch wenn ein Teil später eingespeist wird.
        """
        max_kw = float(self.v.netzladen_max_kw or 0.0)
        if max_kw <= 0 or self.n == 0 or self.lage.preise is None:
            return None
        preise = self.lage.preise
        haus = self.lage.haus
        werte = self.lage.ueberschuss
        fest = self._fest()
        wandlung = self.akku.eta ** 2
        aufschlag = float(self.v.battery_cost or 0.0) + float(self.v.netzladen_vorteil or 0.0)
        grenzen = [-kw / self.akku.eta for kw in self.netz_erlaubt_kw(max_kw)]
        untergrenze = [0.0] * self.n
        self.ersetzt_preis: list[float | None] = [None] * self.n
        von = 0
        for i in pv_enden(werte, self.tage):
            k = i + 1
            gewicht = summe = 0.0
            while k < self.n and werte[k] <= 0:
                gewicht += haus[k]
                summe += haus[k] * preise[k]
                k += 1
            if gewicht > 0:
                ersetzt = summe / gewicht
                for j in range(von, i + 1):
                    if fest[j] and preise[j] + aufschlag < ersetzt * wandlung:
                        untergrenze[j] = grenzen[j]
                        self.ersetzt_preis[j] = ersetzt
            von = i + 1
        if not any(u < 0 for u in untergrenze):
            return None
        return untergrenze

    # -- Pflichtforderungen aus dem Netz --------------------------------

    @cached_property
    def reserve_forderung(self) -> list[float] | None:
        """Mindestinhalt je Slot aus der Spitzenreserve.

        Nur mit „Reserve notfalls aus dem Netz" (``spitzenreserve_netz_kw``).
        Die Kurve sagt je Viertelstunde, wie viel Energie ab ihr bis zum
        nächsten Überschuss über der Grenze liegt. Gefordert wird sie nur am
        Ende des Slots VOR einer Viertelstunde, die selbst zum Bedarf beiträgt
        (Kurve fällt dort um mehr als ``RESERVE_NETZ_BEDARF_MIN_KWH``): Bis
        dahin ist jede Viertelstunde gleich gut zum Nachladen, und die
        günstigste darf es sein. Eine Forderung schon am Abend für die
        Wärmepumpe um 07:30 ließe nur das sofortige Laden zu.

        Gezählt im Modellfenster, also über dem Boden des Modells, der die
        Reserve von jetzt schon enthält, soweit der Ladestand reicht.
        """
        v = self.v
        if float(v.spitzenreserve_netz_kw or 0.0) <= 0:
            return None
        if v.boden_basis_pct is None or not v.reserve_pct or self.n == 0:
            return None
        kap = float(v.kapazitaet_echt_kwh)
        stempel = [int(t.timestamp()) for t in self.lage.stempel]
        schritt_s = int(round(self.akku.p2e * 3600))
        forderung = [0.0] * self.n
        for k in range(self.n - 1):
            # Ohne Kurvenwert (hinter dem Horizont) fällt die Kurve nicht —
            # ein „danach = 0“ machte aus der Mindestreserve eine Forderung
            # am letzten Slot (Traun 02.11.2026). Die Kurve reicht einen Slot
            # weiter als das LP; den Wert dort gibt es also meist noch.
            jetzt = v.reserve_pct.get(stempel[k + 1])
            danach = v.reserve_pct.get(
                stempel[k + 2] if k + 2 < self.n else stempel[-1] + schritt_s
            )
            if jetzt is None or danach is None:
                continue
            anteil = (jetzt - danach) / 100.0 * kap
            if anteil <= RESERVE_NETZ_BEDARF_MIN_KWH:
                continue
            kwh = kap * (float(v.boden_basis_pct) + jetzt - float(v.boden_pct)) / 100.0
            forderung[k] = min(self.akku.kapazitaet, max(0.0, kwh))
        return forderung if any(f > 0 for f in forderung) else None

    @cached_property
    def abend_forderung(self) -> list[float] | None:
        """Mindestladung am Abend: am Termin ``abend_kwh``, davor rückwärts
        so viel, dass der Rest mit ``MINDESTLADUNG_LEISTUNG_ANTEIL`` der
        erlaubten Netzladeleistung plus der erwarteten PV noch hereinkommt.

        Wie die vorsichtige Kette des Ladeziels, nur mit gedrosselter
        Netzleistung statt gedrosselter Sonne. Welche Kette zu welchem Termin
        gehört, merkt ``abend_termin_je_slot`` (für den Grund).
        """
        if self.v.abend_kwh <= 0 or not self.termine_abend:
            return None
        a = self.akku
        max_kw = max(float(self.v.spitzenreserve_netz_kw or 0.0),
                     float(self.v.netzladen_max_kw or 0.0))
        netz = self.netz_erlaubt_kw(max_kw)
        schritt = [
            min(
                ladeleistung_aus_dc(p + MINDESTLADUNG_LEISTUNG_ANTEIL * n / a.eta, a.kapazitaet),
                a.leistung,
            ) * a.p2e
            for p, n in zip(self.lage.pv, netz)
        ]
        forderung = [0.0] * self.n
        for i in self.termine_abend:
            bedarf = self.v.abend_kwh
            if bedarf >= forderung[i]:
                forderung[i] = bedarf
                self.abend_termin_je_slot[i] = i
            # Vom Ende von k auf das Ende von k+1 wirkt der Schritt von k+1.
            for k in range(i - 1, -1, -1):
                bedarf -= schritt[k + 1]
                if bedarf <= 0.0:
                    break
                if bedarf > forderung[k]:
                    forderung[k] = bedarf
                    self.abend_termin_je_slot[k] = i
        return forderung

    @cached_property
    def abend_termin_je_slot(self) -> dict[int, int]:
        """Slot → Termin der Mindestladung, zu dem seine Forderung gehört."""
        return {}

    @cached_property
    def netz_forderung(self) -> list[float] | None:
        """Was aus dem Netz notfalls erreicht werden muss (kWh je Slot).

        Spitzenreserve und Mindestladung, je Slot die höhere — gekappt auf
        das, wovon der Plan bis zum Horizontende noch auf den Endstand kommt.
        Ohne die Kappung hatte das LP keine Lösung, sobald das Horizontende
        kurz hinter der Abendspitze lag: An einem dunklen Tag ist der
        Endstand das, was die PV allein schafft (oft 0), die Reserve davor
        aber aus dem Netz gekauft — 2,6 kWh in zwei Slots abzubauen schafft
        keine 5-kW-Batterie (Replay-Korpus, Huawei und SolaX 01.11.2026).
        """
        reserve = self.reserve_forderung
        abend = self.abend_forderung
        if reserve is None:
            forderung = abend
        elif abend is None:
            forderung = reserve
        else:
            forderung = [max(a, b) for a, b in zip(reserve, abend)]
        if forderung is None:
            return None
        return self._rand(forderung)

    @cached_property
    def pflicht(self) -> list[float] | None:
        """Pflichtladung aus dem Netz je Slot (kW, DC, ≤ 0) — oder None.

        Unabhängig vom Preis, aber so günstig wie möglich: Für die erste
        Forderung, die der Stand ohne Zukauf nicht erreicht, wird der
        günstigste noch freie Slot davor gewählt — bei gleichem Preis der
        späteste —, dann neu gerechnet, bis alles erreicht ist oder kein Slot
        mehr hilft. Hat die Gemeinschaft Überschuss, ist das der günstigste
        Slot. Nur gewählte Slots bekommen eine Grenze — so kauft das LP
        nicht nebenbei auf Vorrat.
        """
        forderung = self.netz_forderung
        if forderung is None or self.lage.preise is None:
            return None
        a = self.akku
        max_kw = max(float(self.v.spitzenreserve_netz_kw or 0.0),
                     float(self.v.netzladen_max_kw or 0.0))
        preise = self.lage.preise
        pv = self.lage.pv
        werte = self.lage.ueberschuss
        fest = self._fest()
        netz = self.netz_erlaubt_kw(max_kw)

        def schritt(k: int, mit_netz: bool) -> float:
            if mit_netz:
                return min(
                    ladeleistung_aus_dc(pv[k] + netz[k] / a.eta, a.kapazitaet), a.leistung
                ) * a.p2e
            w = werte[k]
            if w > 0:
                return min(ladeleistung_aus_dc(w, a.kapazitaet), a.leistung) * a.p2e
            # Die Entladung NICHT auf die Batterieleistung deckeln — wie
            # ``erreichbar`` und ``opt_highs``. Gedeckelt blieb der Stand
            # hier höher, als er im LP fällt, wenn das Haus mehr braucht, als
            # die Batterie hergibt: Es wurden zu wenige Pflicht-Slots
            # gewählt, und ``opt()`` kappte die Forderung danach still.
            return -entladeleistung_fuer_dc(-w, a.kapazitaet) * a.p2e

        gewaehlt = [False] * self.n
        # Forderungen, die auch mit jedem freien Slot davor nicht erreichbar
        # sind, werden übersprungen, nicht die Suche beendet: Bis 2.1.64
        # stand hier ein ``break``, und ein unerreichbarer früher Termin —
        # etwa das Ende des Überschusses im zweiten Slot des Plans — nahm
        # allen späteren die Pflichtladung (Replay-Korpus, Kostal
        # 28.10.2026). ``opt()`` kappt die unerreichbare Forderung ohnehin.
        aufgegeben: set[int] = set()
        for _ in range(2 * self.n):
            stand = a.stand
            luecke = None
            for k in range(self.n):
                stand = min(a.kapazitaet, max(0.0, stand + schritt(k, gewaehlt[k])))
                if k not in aufgegeben and stand < forderung[k] - 1e-6:
                    luecke = k
                    break
            if luecke is None:
                break
            kandidaten = [
                j for j in range(luecke + 1) if not gewaehlt[j] and fest[j] and netz[j] > 0
            ]
            if not kandidaten:
                aufgegeben.add(luecke)
                continue
            gewaehlt[min(kandidaten, key=lambda j: (preise[j], -j))] = True
        if not any(gewaehlt):
            return None
        return [-netz[k] / a.eta if gewaehlt[k] else 0.0 for k in range(self.n)]

    # -- Ergebnis für die Hooks -----------------------------------------

    @cached_property
    def netzladen(self) -> list[float] | None:
        """Untergrenze von ``dc_p`` je Slot: die tiefere aus wirtschaftlich
        und Pflicht. Ohne beide None."""
        w, p = self.wirtschaftlich, self.pflicht
        if p is None:
            return w
        if w is None:
            return p
        return [min(a, b) for a, b in zip(w, p)]

    @cached_property
    def ladeziel_kette(self) -> list[float]:
        """Mindestinhalt je Slot aus dem Ladeziel, rückwärts vom Termin.

        Am Termin steht das Ziel selbst. Davor steht, was die Batterie
        mindestens halten muss, damit sie das Ziel auch dann noch erreicht,
        wenn nur der vorsichtige Pfad (``min_production``: p10, sonst 60 %
        der Erwartung) eintrifft: Überschuss-Slots senken die Forderung um
        das, was sie im schlechten Fall laden, Defizit-Slots heben sie um
        das, was das Haus dort aus der Batterie nimmt. Ohne vorsichtigen Pfad
        gilt der Erwartungswert.

        Warum (Anlage Schweiz, 07.10.2026): Mit dem Ziel nur am Termin
        rechnete der Plan mit der ERWARTETEN Prognose von 29 kWh, speiste
        nachts 5,5 kWh und vormittags 2,5 kWh ein und wollte mittags
        nachladen. Es kamen 21 kWh, abends standen 77 % statt 100.
        """
        a = self.akku
        forderung = [0.0] * self.n
        if not self.termine_ladeziel:
            return forderung
        werte = self.lage.vorsichtig if self.lage.vorsichtig is not None else self.lage.ueberschuss
        # Dieselben gestuften Verluste wie die Erreichbarkeitsschranke in
        # opt_highs.py, gedeckelt durch die Batterieleistung.
        schritt = [
            (
                min(ladeleistung_aus_dc(w, a.kapazitaet), a.leistung) if w > 0
                else -min(entladeleistung_fuer_dc(-w, a.kapazitaet), a.leistung)
            ) * a.p2e
            for w in werte
        ]
        # Wie bor in opt() gilt jeder Wert für den Stand am ENDE des Slots
        # (battery_free[i]): Vom Ende von k auf das Ende von k+1 wirkt
        # der Schritt von k+1.
        for i in self.termine_ladeziel:
            bedarf = self.v.ladeziel_kwh
            forderung[i] = max(forderung[i], bedarf)
            for k in range(i - 1, -1, -1):
                bedarf = min(a.kapazitaet, max(0.0, bedarf - schritt[k + 1]))
                if bedarf <= 0.0:
                    break
                forderung[k] = max(forderung[k], bedarf)
        if self.pflicht is not None:
            # Die Pflicht-Slots gehören der Pflicht: opt() kappt das Ziel
            # auf das mit ALLEN freigegebenen Slots Erreichbare, das Ladeziel
            # selbst darf aber nur, was ohne Zukauf oder mit dem
            # wirtschaftlichen Netzladen geht. Sonst füllte es über die für
            # 0,75 kWh Reserve gewählten Slots bis 100 % aus dem Netz
            # (Traun 02.11.2026).
            erreichbar = self.erreichbar(self.wirtschaftlich)
            forderung = [min(f, e) for f, e in zip(forderung, erreichbar)]
        return self._rand(forderung)

    @cached_property
    def ziel(self) -> list[float]:
        """Mindestinhalt je Slot für den Ladeziel-Hook: Ladeziel und
        Netzforderung, je Slot die höhere."""
        kette = self.ladeziel_kette
        netz = self.netz_forderung
        if netz is None:
            return kette
        if not self.termine_ladeziel:
            return list(netz)
        return [max(f, r) for f, r in zip(kette, netz)]

    # -- Begründung je Slot ---------------------------------------------

    def gruende(
        self,
        inhalt_kwh: list[float],
        netzladung_kw: list[float],
        pct: "Callable[[float], float]",
        ladeziel_pct: float,
        abend_pct: float,
    ) -> list[str | None]:
        """Warum der Plan in einem Slot lädt oder Ladung hält — oder None.

        Nur, wo eine Forderung dieser Rechnung den Slot erklärt:

        * **Netzladung** — für eine Pflichtforderung (Mindestladung,
          Spitzenreserve) oder, im wirtschaftlichen Netzladen, weil der Strom
          jetzt billiger ist als der, den die Batterie danach ersetzt; dazu
          die Grenze, die den Bezug deckelt.
        * **Hält Ladung** — der geplante Stand am Slot-Ende liegt auf einer
          Forderung (Ladeziel samt vorsichtiger Rückwärtskette, Mindestladung,
          Spitzenreserve); am Planungsrand mit dem abgesenkten Wert.

        Reine Preisentscheidungen ohne Forderung (Einspeisen in Bedarfsstunden
        der Gemeinschaft) erklärt das nicht — dafür bräuchte es die Dualpreise.

        ``inhalt_kwh`` ist der geplante Stand am Ende jedes Slots im
        Modellfenster, ``pct`` rechnet kWh im Modellfenster in Prozent um.
        """
        gruende: list[str | None] = [None] * self.n
        netz = self.netz_forderung or [0.0] * self.n
        kette = self.ladeziel_kette
        pflicht = self.pflicht
        wirtschaftlich = self.wirtschaftlich
        ersetzt = getattr(self, "ersetzt_preis", [None] * self.n)
        max_kw = max(float(self.v.spitzenreserve_netz_kw or 0.0),
                     float(self.v.netzladen_max_kw or 0.0))
        begrenzung = self.netz_begrenzung(max_kw) if max_kw > 0 else []
        # Die Forderung, wie opt() sie wirken lässt (chamo § 2b/§ 5): gekappt
        # auf das ohne Zukauf Erreichbare, mit Netz-Slots auf das mit ihnen
        # Erreichbare.
        wirksam = [min(z, e) for z, e in zip(self.ziel, self.erreichbar(self.netzladen))]

        def uhr(k: int) -> str:
            return self.lage.stempel[min(k, self.n - 1)].strftime("%H:%M")

        def zahl(x: float, stellen: int = 0) -> str:
            return f"{x:.{stellen}f}".replace(".", ",")

        def netz_quelle(k: int) -> str | None:
            """Die nächste Netzforderung ab Slot k, als Satzteil."""
            for j in range(k, self.n):
                if netz[j] <= 0:
                    continue
                if j in self.abend_termin_je_slot:
                    termin = self.abend_termin_je_slot[j]
                    text = f"die Mindestladung ({zahl(abend_pct)} % um {uhr(termin)})"
                    if termin in self.am_rand_abgesenkt:
                        text += f", am Planungsrand auf {zahl(pct(netz[termin]))} % abgesenkt"
                    return text
                return f"die Spitzenreserve (Spitze um {uhr(j + 1)})"
            return None

        def ladeziel_quelle(k: int) -> str | None:
            termin = next((i for i in self.termine_ladeziel if i >= k), None)
            if termin is None:
                return None
            text = f"das Ladeziel ({zahl(ladeziel_pct)} % um {uhr(termin)})"
            if termin in self.am_rand_abgesenkt:
                text += f", am Planungsrand auf {zahl(pct(wirksam[termin]))} % abgesenkt"
            elif pct(wirksam[termin]) < ladeziel_pct - 0.5:
                text += f", erreichbar sind {zahl(pct(wirksam[termin]))} %"
            elif k < termin:
                text += ", auch bei schwacher Sonne"
            return text

        for k in range(self.n):
            laden = netzladung_kw[k] if k < len(netzladung_kw) else 0.0
            if laden >= 0.05:
                if pflicht is not None and pflicht[k] < 0 and netz_quelle(k):
                    text = f"Netzladung für {netz_quelle(k)}"
                elif wirtschaftlich is not None and wirtschaftlich[k] < 0 and ersetzt[k]:
                    text = (
                        f"Netzladung, weil günstig: {zahl(100 * self.lage.preise[k], 1)} ct "
                        f"statt {zahl(100 * ersetzt[k], 1)} ct danach"
                    )
                else:
                    text = None
                if text and begrenzung:
                    erlaubt, quelle, wert = begrenzung[k]
                    if quelle and laden >= erlaubt - 0.1:
                        art = "die Spitzenkappung" if quelle == "kappung" else "die Netzgrenze"
                        text += f"; Bezug begrenzt durch {art} ({zahl(wert, 1)} kW)"
                gruende[k] = text
                continue
            # Bindend ist eine Forderung nur, wo der Plan AUF ihr liegt — und
            # zwar auf der wirksamen: opt() kappt sie noch auf das Erreichbare
            # (ohne Zukauf, bzw. mit den freigegebenen Netz-Slots). Liegt der
            # Plan darunter, hat die Kappung gegriffen, nicht die Forderung.
            forderung = wirksam[k]
            if forderung <= 0.05 or abs(inhalt_kwh[k] - forderung) > 0.05:
                continue
            if netz[k] >= kette[k]:
                quelle = netz_quelle(k)
            else:
                quelle = ladeziel_quelle(k)
            if quelle:
                gruende[k] = f"Hält Ladung für {quelle}"
        return gruende
