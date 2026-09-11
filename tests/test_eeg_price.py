"""Tests für die EEG-Preisfunktion (eeg_price.py).

Geprüft wird die Rechnung ohne Home Assistant und ohne Solver: aus Anteil,
Vergütung, Gewichtung und dem Saldo der Gemeinschaft wird ein Preisauf- oder
-abschlag je Zeitpunkt. Die Vorzeichen-, Deckel- und Bodenfragen stecken hier,
nicht im LP.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import eeg_price as ep

TZ = timezone(timedelta(hours=2))
START = datetime(2026, 8, 25, 0, 0, tzinfo=TZ)

BASIS = 0.082          # Einspeisetarif des Energieversorgers
BEZUG = 0.25


def _stamps(stunden=24, schritt_min=30):
    return [START + timedelta(minutes=schritt_min * i)
            for i in range(int(stunden * 60 / schritt_min))]


def _intervalle(werte_je_ortszeit):
    """PeakShare-V2-Daten bauen: {Ortsstunde: kWh} -> vier Viertelstunden.

    Der Wert gilt für jede Viertelstunde der Stunde. Damit bleiben die
    Testaussagen dieselben wie zur Zeit der Stundenwerte, und der Saldo trägt
    sein Vorzeichen: positiv ist Bedarf, negativ Überschuss.
    """
    eintraege = []
    for h, v in sorted(werte_je_ortszeit.items()):
        for viertel in range(4):
            stempel = START + timedelta(hours=h, minutes=15 * viertel)
            eintraege.append(
                {
                    "timestamp": stempel.astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "saldoKwh": float(v),
                }
            )
    return eintraege


# ---------------------------------------------------------------------------
# Konfiguration lesen
# ---------------------------------------------------------------------------


def test_zwei_gemeinschaften_werden_gelesen():
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "Pucking",
        "peakshare_share_pct": 40, "peakshare_price": 0.102, "peakshare_weight": 0.01,
        "peakshare_community_2": "BEG",
        "peakshare_share_pct_2": 60, "peakshare_price_2": 0.102, "peakshare_weight_2": 0,
    })

    assert [x.name for x in g] == ["Pucking", "BEG"]
    assert g[0].anteil == pytest.approx(0.4)
    # Wert = Vergütung + Gewichtung; ohne Nachtfeld gilt der Tagsatz
    assert g[0].wert_tag == pytest.approx(0.112)
    assert g[0].wert_nacht == pytest.approx(0.112)
    assert g[1].wert_tag == pytest.approx(0.102)
    assert g[0].wert(ist_nacht=False) == pytest.approx(0.112)
    # Der Anzeigename ist der Name aus PeakShare — ein eigenes Kennzeichen
    # EEG/BEG gab es einmal, es hat nie gerechnet und ist entfallen.
    assert g[0].name == "Pucking"
    assert g[1].name == "BEG"
    assert ep.anteile_summe(g) == pytest.approx(1.0)


@pytest.mark.parametrize("config", [
    {},                                                            # nichts gesetzt
    {"peakshare_community": "BEG", "peakshare_share_pct": 0},       # Anteil 0
    {"peakshare_community": "BEG", "peakshare_share_pct": 50},      # kein Wert
    {"peakshare_community": "", "peakshare_share_pct": 50, "peakshare_price": 0.1},
    {"enable_peakshare": False, "peakshare_community": "BEG",
     "peakshare_share_pct": 50, "peakshare_price": 0.1},            # Funktion aus
])
def test_unvollstaendige_konfiguration_ergibt_keine_gemeinschaft(config):
    assert ep.gemeinschaften_aus_config(config) == []


def test_leere_panel_felder_stuerzen_nicht_ab():
    """Das Panel schickt für ein geleertes Zahlenfeld einen Leerstring."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": "",
        "peakshare_price": "", "peakshare_weight": "Unsinn",
    })
    assert g == []


# ---------------------------------------------------------------------------
# Bedarf abbilden
# ---------------------------------------------------------------------------


def test_saldo_wird_auf_epochenviertelstunden_abgebildet():
    saldo = ep.saldo_je_intervall(_intervalle({0: 5.0, 1: 0.0, 2: -12.5}))

    erste = int(START.timestamp() // 900)
    assert saldo[erste] == 5.0
    assert saldo[erste + 4] == 0.0
    # Vorzeichen bleibt erhalten: negativ ist Überschuss
    assert saldo[erste + 8] == -12.5


def test_saldo_ignoriert_muell():
    saldo = ep.saldo_je_intervall([
        {"timestamp": "kaputt", "saldoKwh": 5},
        {"timestamp": None, "saldoKwh": 5},
        {"saldoKwh": 5},
        {"timestamp": "2026-08-25T00:00:00.000Z"},          # ohne Wert
        "kein dict",
        {"timestamp": "2026-08-25T01:00:00.000Z", "saldoKwh": -3},   # Überschuss
    ])
    # Nur der Überschuss-Eintrag bleibt — und behält sein Vorzeichen. Anders
    # als früher wird nicht mehr auf 0 geklemmt: negativ ist jetzt eine
    # Aussage, kein Fehler.
    assert list(saldo.values()) == [-3.0]


# ---------------------------------------------------------------------------
# Aufschlag
# ---------------------------------------------------------------------------


def test_aufschlag_erreicht_zur_spitze_genau_den_anteil_an_der_differenz():
    """Kern der Formel: Anteil · (Wert − Basistarif) zur Bedarfsspitze."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG",
        "peakshare_share_pct": 50, "peakshare_price": 0.122, "peakshare_weight": 0,
    })
    stamps = _stamps(4)
    bedarf = {"BEG": ep.saldo_je_intervall(_intervalle({0: 0.0, 1: 50.0, 2: 100.0, 3: 25.0}))}

    auf, diagnose = ep.aufschlag_reihe(g, bedarf, stamps, BASIS)

    # Differenz 0,122 − 0,082 = 0,040; Anteil 50 % -> Spitze 0,020
    assert max(auf) == pytest.approx(0.020)
    assert diagnose[0]["max_aufschlag_ct"] == pytest.approx(2.0)
    assert diagnose[0]["spitze_kwh"] == pytest.approx(100.0)
    # Halber Bedarf -> halber Aufschlag; kein Bedarf -> nichts
    stunde = {s: a for s, a in zip(stamps, auf)}
    assert stunde[START] == pytest.approx(0.0)
    assert stunde[START + timedelta(hours=1)] == pytest.approx(0.010)
    assert stunde[START + timedelta(hours=2)] == pytest.approx(0.020)
    # Der Wert der Viertelstunde gilt, in die der Zeitpunkt fällt
    assert stunde[START + timedelta(minutes=90)] == pytest.approx(0.010)


def test_zwei_gemeinschaften_addieren_sich():
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "A", "peakshare_share_pct": 40,
        "peakshare_price": 0.102, "peakshare_weight": 0.01,     # Wert 0,112
        "peakshare_community_2": "B", "peakshare_share_pct_2": 60,
        "peakshare_price_2": 0.102, "peakshare_weight_2": 0,    # Wert 0,102
    })
    stamps = _stamps(3)
    bedarf = {
        "A": ep.saldo_je_intervall(_intervalle({0: 100.0, 1: 0.0, 2: 100.0})),
        "B": ep.saldo_je_intervall(_intervalle({0: 0.0, 1: 100.0, 2: 100.0})),
    }

    auf, _ = ep.aufschlag_reihe(g, bedarf, stamps, BASIS)
    je_stunde = {s: a for s, a in zip(stamps, auf) if s.minute == 0}
    werte = [je_stunde[START + timedelta(hours=h)] for h in range(3)]

    # A allein: 0,4 · 0,030 = 0,012 | B allein: 0,6 · 0,020 = 0,012
    assert werte[0] == pytest.approx(0.012)
    assert werte[1] == pytest.approx(0.012)
    # Beide gleichzeitig: Summe
    assert werte[2] == pytest.approx(0.024)


def test_ohne_bedarfsdaten_kein_aufschlag():
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.15,
    })
    auf, diagnose = ep.aufschlag_reihe(g, {}, _stamps(4), BASIS)

    assert set(auf) == {0.0}
    assert diagnose[0]["hinweis"] == "keine Bedarfsdaten"


def test_verguetung_unter_basistarif_wirkt_in_keine_richtung():
    """Zahlt die Gemeinschaft weniger als der EVU, gibt es nichts zu verschieben.

    Dann gilt das für beide Richtungen: weder ein Aufschlag in Bedarfsstunden
    noch ein Abschlag bei Überschuss.
    """
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.05,
    })
    bedarf = {"BEG": ep.saldo_je_intervall(_intervalle({0: 100.0}))}

    auf, diagnose = ep.aufschlag_reihe(g, bedarf, _stamps(2), BASIS)

    assert set(auf) == {0.0}
    assert diagnose[0]["hinweis"] == "kein Mehrwert gegenüber dem Basistarif"


# ---------------------------------------------------------------------------
# Tag- und Nachtsatz, Basistarif als Reihe
# ---------------------------------------------------------------------------


def test_nachtsatz_der_gemeinschaft_wird_verwendet():
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.102, "peakshare_price_night": 0.142,
    })
    assert g[0].wert_tag == pytest.approx(0.102)
    assert g[0].wert_nacht == pytest.approx(0.142)

    stamps = _stamps(2, schritt_min=60)
    bedarf = {"BEG": ep.saldo_je_intervall(_intervalle({0: 100.0, 1: 100.0}))}

    # Erste Stunde Tag, zweite Nacht — gleicher Bedarf, verschiedener Aufschlag
    auf, diagnose = ep.aufschlag_reihe(g, bedarf, stamps, BASIS, [False, True])

    assert auf[0] == pytest.approx(0.102 - BASIS)   # 2,0 ct
    assert auf[1] == pytest.approx(0.142 - BASIS)   # 6,0 ct
    assert diagnose[0]["wert_nacht_ct"] == pytest.approx(14.2)
    assert diagnose[0]["max_aufschlag_ct"] == pytest.approx(6.0)


def test_nachtsatz_null_bedeutet_gleich_wie_tag():
    """Ein leeres Nachtfeld darf den Anreiz nicht nachts löschen."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.102, "peakshare_price_night": 0,
    })
    assert g[0].wert_nacht == pytest.approx(g[0].wert_tag)


def test_basistarif_als_reihe():
    """Der OeMAG-Tarif und ein Nachttarif machen den Basistarif zeitabhängig —
    verglichen wird immer mit dem Wert desselben Zeitpunkts."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.102,
    })
    stamps = _stamps(2, schritt_min=60)
    bedarf = {"BEG": ep.saldo_je_intervall(_intervalle({0: 100.0, 1: 100.0}))}

    # Zweiter Zeitpunkt: Basistarif höher als die Vergütung -> kein Aufschlag
    auf, _ = ep.aufschlag_reihe(g, bedarf, stamps, [0.082, 0.120])

    assert auf[0] == pytest.approx(0.020)
    assert auf[1] == pytest.approx(0.0)


def test_kurze_basisreihe_wird_aufgefuellt():
    """Defensiv: eine zu kurze Reihe darf keinen IndexError werfen."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_price": 0.102,
    })
    stamps = _stamps(3, schritt_min=60)
    bedarf = {"BEG": ep.saldo_je_intervall(_intervalle({0: 100.0, 1: 100.0, 2: 100.0}))}

    auf, _ = ep.aufschlag_reihe(g, bedarf, stamps, [0.082])

    assert len(auf) == 3
    assert all(a == pytest.approx(0.020) for a in auf)


# ---------------------------------------------------------------------------
# Überschuss
# ---------------------------------------------------------------------------


def test_ueberschuss_erzeugt_einen_abschlag_derselben_groesse():
    """Spiegelbild des Aufschlags: gleiche Formel, gleicher Anteil, andere Richtung."""
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG",
        "peakshare_share_pct": 50, "peakshare_price": 0.122, "peakshare_weight": 0,
    })
    stamps = _stamps(4)
    # Spitzenbedarf 100, Spitzenüberschuss 100 — symmetrisch
    saldo = {"BEG": ep.saldo_je_intervall(
        _intervalle({0: 100.0, 1: 50.0, 2: -50.0, 3: -100.0})
    )}

    auf, diagnose = ep.aufschlag_reihe(g, saldo, stamps, BASIS)
    je_stunde = {s: a for s, a in zip(stamps, auf) if s.minute == 0}

    # Differenz 0,040, Anteil 50 % -> ±0,020 an den Spitzen
    assert je_stunde[START] == pytest.approx(0.020)
    assert je_stunde[START + timedelta(hours=1)] == pytest.approx(0.010)
    assert je_stunde[START + timedelta(hours=2)] == pytest.approx(-0.010)
    assert je_stunde[START + timedelta(hours=3)] == pytest.approx(-0.020)
    assert diagnose[0]["max_aufschlag_ct"] == pytest.approx(2.0)
    assert diagnose[0]["max_abschlag_ct"] == pytest.approx(-2.0)


def test_jede_seite_wird_auf_ihre_eigene_spitze_normiert():
    """Der entscheidende Punkt der gleichen Behandlung.

    Eine PV-starke Gemeinschaft hat mittags ein Vielfaches an Überschuss
    gegenüber ihrem Bedarf. Bei einer gemeinsamen Normierung bliebe vom
    Bedarf fast nichts — und der Bedarf ist die Größe, um die es geht.
    """
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG",
        "peakshare_share_pct": 100, "peakshare_price": 0.122, "peakshare_weight": 0,
    })
    stamps = _stamps(2)
    # Überschuss ist zwanzigmal so groß wie der Bedarf
    saldo = {"BEG": ep.saldo_je_intervall(_intervalle({0: 10.0, 1: -200.0}))}

    auf, diagnose = ep.aufschlag_reihe(g, saldo, stamps, BASIS)
    je_stunde = {s: a for s, a in zip(stamps, auf) if s.minute == 0}

    # Beide erreichen die volle Differenz — jede auf ihrer eigenen Skala
    assert je_stunde[START] == pytest.approx(0.040)
    assert je_stunde[START + timedelta(hours=1)] == pytest.approx(-0.040)
    assert diagnose[0]["spitze_kwh"] == pytest.approx(10.0)
    assert diagnose[0]["ueberschuss_spitze_kwh"] == pytest.approx(200.0)


def test_nur_ueberschuss_ohne_bedarf_wirkt_trotzdem():
    """Eine Gemeinschaft kann im ganzen Fenster Überschuss haben.

    Früher hätte eine leere Bedarfsseite die Rechnung abgebrochen ("keine
    Bedarfsdaten") — jetzt trägt die andere Seite.
    """
    g = ep.gemeinschaften_aus_config({
        "peakshare_community": "BEG",
        "peakshare_share_pct": 100, "peakshare_price": 0.122, "peakshare_weight": 0,
    })
    saldo = {"BEG": ep.saldo_je_intervall(_intervalle({0: -40.0, 1: -20.0}))}

    auf, diagnose = ep.aufschlag_reihe(g, saldo, _stamps(2), BASIS)

    # Beide Stunden liegen im Ueberschuss, also ist kein Zeitpunkt neutral:
    # die tiefere erreicht die volle Differenz, die halb so tiefe die Haelfte.
    assert min(auf) == pytest.approx(-0.040)
    assert max(auf) == pytest.approx(-0.020)
    assert "hinweis" not in diagnose[0]


# ---------------------------------------------------------------------------
# Boden
# ---------------------------------------------------------------------------


def test_boden_faengt_negative_preise_ab():
    """Unter null wirft das LP die Energie lieber weg, als sie zu verschenken."""
    preise, angehoben, tiefster = ep.mit_boden([0.05, -0.02, 0.0, -0.10])

    assert preise == [0.05, 0.0, 0.0, 0.0]
    assert angehoben == 2
    assert tiefster == pytest.approx(-0.10)


def test_boden_laesst_positive_preise_unberuehrt():
    preise, angehoben, tiefster = ep.mit_boden([0.05, 0.08])

    assert preise == [0.05, 0.08]
    assert angehoben == 0
    assert tiefster == pytest.approx(0.05)


def test_boden_vertraegt_eine_leere_reihe():
    preise, angehoben, tiefster = ep.mit_boden([])

    assert preise == []
    assert angehoben == 0
    assert tiefster == 0.0


# ---------------------------------------------------------------------------
# Feste Abnahmequote (Gemeinschaft ohne PeakShare)
# ---------------------------------------------------------------------------

QUOTE_CONFIG = {
    "eeg_demand_source": "quote",
    "peakshare_community": "EEG Musterdorf",
    "peakshare_share_pct": 100, "peakshare_price": 0.095, "peakshare_weight": 0,
    "peakshare_quote_pct": 40, "peakshare_quote_night_pct": 90,
}


def test_bedarfsquelle_ist_peakshare_ausser_bei_ausdruecklicher_quote():
    assert ep.bedarfsquelle({}) == ep.DEMAND_SOURCE_PEAKSHARE
    assert ep.bedarfsquelle({"eeg_demand_source": "peakshare"}) == ep.DEMAND_SOURCE_PEAKSHARE
    assert ep.bedarfsquelle({"eeg_demand_source": " Quote "}) == ep.DEMAND_SOURCE_QUOTE
    assert ep.bedarfsquelle({"eeg_demand_source": "unsinn"}) == ep.DEMAND_SOURCE_PEAKSHARE


def test_quoten_werden_gelesen():
    g = ep.gemeinschaften_aus_config(QUOTE_CONFIG)

    assert len(g) == 1
    assert g[0].quote_tag == pytest.approx(0.4)
    assert g[0].quote_nacht == pytest.approx(0.9)
    assert g[0].quote(ist_nacht=True) == pytest.approx(0.9)
    assert g[0].quote(ist_nacht=False) == pytest.approx(0.4)
    # Leeres Nachtfeld: wie am Tag. Über 100 % wird geklemmt.
    g = ep.gemeinschaften_aus_config(
        {**QUOTE_CONFIG, "peakshare_quote_night_pct": "", "peakshare_quote_pct": 130}
    )
    assert g[0].quote_tag == pytest.approx(1.0)
    assert g[0].quote_nacht == pytest.approx(1.0)


def test_ohne_quote_faellt_die_gemeinschaft_heraus():
    """Ohne Quote nimmt sie nichts auf — wie eine PeakShare-Gemeinschaft ohne
    Daten wirkt sie in keine Richtung."""
    assert ep.gemeinschaften_aus_config(
        {**QUOTE_CONFIG, "peakshare_quote_pct": 0, "peakshare_quote_night_pct": 0}
    ) == []
    assert ep.gemeinschaften_aus_config(
        {**QUOTE_CONFIG, "peakshare_quote_pct": "", "peakshare_quote_night_pct": ""}
    ) == []
    # Nur eine Nachtquote hält sie am Leben (Tag 0 %, Nacht 90 %).
    g = ep.gemeinschaften_aus_config({**QUOTE_CONFIG, "peakshare_quote_pct": 0})
    assert len(g) == 1 and g[0].quote_tag == 0.0 and g[0].quote_nacht == pytest.approx(0.9)


def test_peakshare_modus_kennt_keine_quote():
    """Gespeicherte Quotenfelder ändern im PeakShare-Modus nichts."""
    g = ep.gemeinschaften_aus_config({**QUOTE_CONFIG, "eeg_demand_source": "peakshare"})
    assert g[0].quote_tag is None and g[0].quote_nacht is None
    tarife = ep.echte_tarife_aus_config({**QUOTE_CONFIG, "eeg_demand_source": "peakshare"})
    assert "quote_tag" not in tarife[0] and "quote_nacht" not in tarife[0]


def test_echte_tarife_tragen_die_quote_im_quotenmodus():
    tarife = ep.echte_tarife_aus_config(QUOTE_CONFIG)
    assert tarife == [{
        "name": "EEG Musterdorf", "anteil": 1.0, "tag": 0.095, "nacht": 0.095,
        "quote_tag": pytest.approx(0.4), "quote_nacht": pytest.approx(0.9),
    }]
    # Ohne Quote bleibt der Eintrag (echter Satz), nimmt aber nichts auf.
    tarife = ep.echte_tarife_aus_config(
        {**QUOTE_CONFIG, "peakshare_quote_pct": "", "peakshare_quote_night_pct": ""}
    )
    assert tarife[0]["quote_tag"] == 0.0 and tarife[0]["quote_nacht"] == 0.0
    # Nur die Nachtquote gesetzt: tags 0, nachts 90 %.
    tarife = ep.echte_tarife_aus_config({**QUOTE_CONFIG, "peakshare_quote_pct": ""})
    assert tarife[0]["quote_tag"] == 0.0 and tarife[0]["quote_nacht"] == pytest.approx(0.9)


def test_quoten_aufschlag_ist_der_mischpreis_je_tageszeit():
    """Anteil 100 %, Satz 9,5 ct, Basis 8,989 ct: tags 40 % der Differenz,
    nachts 90 % — an jedem Zeitpunkt, ohne Bedarfsdaten."""
    g = ep.gemeinschaften_aus_config(QUOTE_CONFIG)
    stamps = _stamps(24, 60)
    nacht = [s.hour >= 20 or s.hour < 6 for s in stamps]

    aufschlaege, diagnose = ep.quoten_aufschlag_reihe(g, stamps, 0.08989, nacht)

    for a, n in zip(aufschlaege, nacht):
        assert a == pytest.approx((0.9 if n else 0.4) * (0.095 - 0.08989))
    assert diagnose[0]["hinweis"] == "feste Abnahmequote"
    assert diagnose[0]["quote_tag_pct"] == 40.0
    assert diagnose[0]["quote_nacht_pct"] == 90.0
    assert diagnose[0]["max_aufschlag_ct"] == pytest.approx(0.46, abs=0.005)
    assert diagnose[0]["max_abschlag_ct"] == 0.0


def test_quoten_aufschlag_zaehlt_negative_differenz_mit():
    """SUNNY im Jänner (10,969 ct) über dem EEG-Satz: der Mischpreis liegt
    wirklich darunter — er ist der erwartete Erlös, kein Anreiz."""
    g = ep.gemeinschaften_aus_config({**QUOTE_CONFIG, "peakshare_share_pct": 50})
    stamps = _stamps(2, 60)

    aufschlaege, diagnose = ep.quoten_aufschlag_reihe(g, stamps, 0.10969, [False, False])

    assert all(a == pytest.approx(0.5 * 0.4 * (0.095 - 0.10969)) for a in aufschlaege)
    assert diagnose[0]["max_abschlag_ct"] < 0


def test_quoten_aufschlag_mit_basisreihe_und_zwei_gemeinschaften():
    g = ep.gemeinschaften_aus_config({
        **QUOTE_CONFIG, "peakshare_share_pct": 60,
        "peakshare_community_2": "BEG", "peakshare_share_pct_2": 40,
        "peakshare_price_2": 0.12, "peakshare_quote_pct_2": 50,
    })
    stamps = _stamps(2, 60)

    aufschlaege, diagnose = ep.quoten_aufschlag_reihe(g, stamps, [0.05, 0.10], [False, False])

    assert aufschlaege[0] == pytest.approx(0.6 * 0.4 * (0.095 - 0.05) + 0.4 * 0.5 * (0.12 - 0.05))
    assert aufschlaege[1] == pytest.approx(0.6 * 0.4 * (0.095 - 0.10) + 0.4 * 0.5 * (0.12 - 0.10))
    assert [e["name"] for e in diagnose] == ["EEG Musterdorf", "BEG"]


def test_quoten_aufschlag_ohne_quote_ist_null():
    g = [ep.Gemeinschaft(name="X", anteil=1.0, wert_tag=0.1, wert_nacht=0.1)]
    aufschlaege, diagnose = ep.quoten_aufschlag_reihe(g, _stamps(2, 60), 0.05)
    assert aufschlaege == [0.0, 0.0]
    assert diagnose[0]["hinweis"] == "keine Abnahmequote"
