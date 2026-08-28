"""Tests für das Lesen des OeMAG-Einspeisetarifs.

Die OeMAG bietet keine Schnittstelle, der Tarif steht in einer HTML-Tabelle.
Geprüft wird deshalb genau das Zerlegen — mit einem Auszug der echten Seite
vom 25.08.2026 — und das Verhalten, wenn sich die Seite ändert.
"""

import pytest

from custom_components.eeg_energy_optimizer import oemag

# Auszug der echten Seite (gekürzt, Aufbau unverändert).
ECHTES_HTML = """
<h2>Marktpreis 2026</h2>
<table class="table-bordered table-hover table-striped">
<tbody>
<tr><th>Monat</th><th>Marktpreis Photovoltaik<br>und andere Energieträger<br>(außer Windkraft)</th>
    <th>Marktpreis Windkraft</th><th>Rechtliche Grundlage</th><th>Kommentar</th></tr>
<tr><td>Jänner</td><td>8,842 ct/kWh</td><td>8,796 ct/kWh</td>
    <td>Preis gem. &sect; 13 Abs. 3 iVm &sect; 41 &Ouml;SG</td><td>Marktpreis abz&uuml;gl. Aufwand</td></tr>
<tr><td>Februar</td><td>8,457 ct/kWh</td><td>8,411 ct/kWh</td><td>...</td><td>...</td></tr>
<tr><td>M&auml;rz</td><td>5,720 ct/kWh</td><td>5,674 ct/kWh</td><td>...</td><td>...</td></tr>
<tr><td>April</td><td>6,772 ct/kWh</td><td>6,726 ct/kWh</td><td>...</td><td>...</td></tr>
<tr><td>Mai</td><td>6,772 ct/kWh</td><td>6,726 ct/kWh</td><td>...</td><td>...</td></tr>
<tr><td>Juni</td><td>6,772 ct/kWh</td><td>6,726 ct/kWh</td><td>...</td><td>...</td></tr>
<tr><td>Juli</td><td>6,146 ct/kWh</td><td>6,100 ct/kWh</td><td>...</td><td>...</td></tr>
</tbody>
</table>
<table><tr><td>Fall 1</td><td>Fall 2</td></tr>
<tr><td>Schritt 1 -&gt; Ticketausgabe</td><td>99,999 ct/kWh</td></tr></table>
"""


def test_echte_seite_wird_zerlegt():
    tarife = oemag.parse_tarife(ECHTES_HTML)

    assert len(tarife) == 7
    assert tarife[1] == pytest.approx(0.08842)
    assert tarife[3] == pytest.approx(0.05720)   # März mit HTML-Entity
    assert tarife[7] == pytest.approx(0.06146)
    # Die zweite Tabelle (Ablaufbeschreibung) darf nicht mitgelesen werden.
    assert 8 not in tarife and 12 not in tarife


def test_windkraftspalte_wird_nicht_verwechselt():
    """Spalte 2 ist Photovoltaik, Spalte 3 Windkraft — 6,146 statt 6,100."""
    tarife = oemag.parse_tarife(ECHTES_HTML)
    assert tarife[7] == pytest.approx(0.06146)
    assert tarife[7] != pytest.approx(0.06100)


@pytest.mark.parametrize("html", ["", "<p>Seite umgebaut</p>", None,
                                  "<table><tr><td>Monat</td><td>Preis</td></tr></table>"])
def test_unlesbares_html_ergibt_nichts(html):
    assert oemag.parse_tarife(html) == {}


def test_laufender_monat_noch_nicht_veroeffentlicht():
    """Am 25.08.2026 endete die Tabelle bei Juli — dann gilt Juli."""
    tarife = oemag.parse_tarife(ECHTES_HTML)

    assert oemag.tarif_fuer(tarife, 8) == (pytest.approx(0.06146), 7)
    assert oemag.tarif_fuer(tarife, 12) == (pytest.approx(0.06146), 7)


def test_laufender_monat_vorhanden():
    tarife = oemag.parse_tarife(ECHTES_HTML)
    assert oemag.tarif_fuer(tarife, 4) == (pytest.approx(0.06772), 4)


def test_jahreswechsel_nimmt_den_juengsten_bekannten_monat():
    """Im Jänner steht die Tabelle des neuen Jahres oft noch leer; enthält sie
    nur spätere Monate, ist der jüngste davon der beste bekannte Wert."""
    tarife = {6: 0.05, 7: 0.06}
    assert oemag.tarif_fuer(tarife, 1) == (0.06, 7)


def test_ohne_tarife_kein_ergebnis():
    assert oemag.tarif_fuer({}, 8) is None


@pytest.mark.parametrize("text,erwartet", [
    ("6,146 ct/kWh", 0.06146),
    ("6.146 ct/kWh", 0.06146),
    ("10,923 ct/kWh", 0.10923),
    ("  8,842   ct/kWh ", 0.08842),
    ("6,146", None),          # ohne Einheit: nicht verwendbar
    ("ct/kWh", None),
    ("", None),
])
def test_preis_lesen(text, erwartet):
    ergebnis = oemag._ct_pro_kwh(text)
    if erwartet is None:
        assert ergebnis is None
    else:
        assert ergebnis == pytest.approx(erwartet)


def test_preisanbieter_werden_vor_dem_wizard_abbruch_geladen():
    """OeMAG und Spot müssen schon im Einrichtungsassistenten verfügbar sein.

    ``async_setup_entry`` steigt bei unvollständiger Einrichtung früh aus
    (``if not setup_complete``) und registriert nur das Panel. Standen die
    Preis-Anbieter hinter diesem Ausstieg, meldete das Panel im Assistenten
    „Anbieter nicht geladen" — und der Knopf „Jetzt holen" konnte daran
    nichts ändern, weil der WebSocket-Befehl den Anbieter gar nicht findet.
    Das traf jede Neuinstallation, weil OeMAG die Vorgabe der Standard-
    vergütung ist.

    Beide Anbieter hängen an keiner Anlage und an keinem Sensor — sie holen
    nur eine Website und dürfen deshalb vor dem Ausstieg stehen.
    """
    from pathlib import Path

    quelle = (
        Path(__file__).resolve().parents[1]
        / "custom_components" / "eeg_energy_optimizer" / "__init__.py"
    ).read_text(encoding="utf-8")

    abbruch = quelle.index("if not setup_complete:")
    for marker, name in (
        ('["oemag"] = oemag_provider', "OeMAG"),
        ('["spot"] = spot_provider', "Spot"),
    ):
        stelle = quelle.index(marker)
        assert stelle < abbruch, (
            f"Der {name}-Anbieter wird erst nach dem Ausstieg bei "
            "unvollständiger Einrichtung angelegt — im Einrichtungs"
            "assistenten meldet das Panel dann 'Anbieter nicht geladen'."
        )
