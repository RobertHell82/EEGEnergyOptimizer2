"""Tarife der Gemeinschaften vom Server (gemeinschaftstarif.py)."""

from datetime import date

from custom_components.eeg_energy_optimizer import gemeinschaftstarif as gt

# So liefert der Server den Tarif der Energiewende Ansfelden für Q4/2026.
EWA = {
    "verband": "ewa",
    "verband_name": "Energiewende Ansfelden",
    "tarife": [
        {"gueltig_ab": "2026-07-01", "einspeisung_tag": 0.09, "einspeisung_nacht": None,
         "nacht": None, "bezug": 0.13, "quelle": None, "hinweis": None},
        {"gueltig_ab": "2026-10-01", "einspeisung_tag": 0.098, "einspeisung_nacht": 0.108,
         "nacht": "sonne", "bezug": 0.137, "quelle": "https://www.ew-ansfelden.at/about-7",
         "hinweis": "Speicherstrom nach Sonnenuntergang bekommt den Wasser/Wind-Tarif."},
        {"gueltig_ab": "2027-01-01", "einspeisung_tag": 0.1, "einspeisung_nacht": 0.11,
         "nacht": "sonne", "bezug": 0.14, "quelle": None, "hinweis": None},
    ],
}
ANTWORT = {"version": 1, "gemeinschaften": {"BEG": EWA, "Pucking": EWA}}
DATEN = gt.parse_antwort(ANTWORT)
OKT = date(2026, 10, 30)
SONNE = (16.75, 7.1)   # Untergang 16:45, Aufgang 07:06

BASIS = {
    "peakshare_community": "BEG",
    "peakshare_share_pct": 100,
    "peakshare_price": 0.12,
    "peakshare_price_night": 0.15,
    "peakshare_weight": 0.01,
    "peakshare_night_start": "20:00",
    "peakshare_night_end": "06:00",
    "peakshare_bezug_price": 0.2,
    "peakshare_bezug_ebene": "regional",
}


class TestParse:
    def test_gueltige_antwort(self):
        assert set(DATEN) == {"BEG", "Pucking"}
        assert [t["gueltig_ab"] for t in DATEN["BEG"]["tarife"]] == [
            "2026-07-01", "2026-10-01", "2027-01-01"]

    def test_unplausibles_faellt_weg(self):
        daten = gt.parse_antwort({"gemeinschaften": {"X": {"tarife": [
            {"gueltig_ab": "2026-10-01", "einspeisung_tag": 9.8},          # Cent statt Euro
            {"gueltig_ab": "01.10.2026", "einspeisung_tag": 0.098},        # Datumsformat
            {"gueltig_ab": "2026-11-01", "einspeisung_tag": 0.098,
             "einspeisung_nacht": -1, "nacht": "25-6", "bezug": "x"},
        ]}}})
        (t,) = daten["X"]["tarife"]
        assert t["gueltig_ab"] == "2026-11-01"
        assert t["einspeisung_nacht"] is None and t["nacht"] is None and t["bezug"] is None

    def test_muell(self):
        assert gt.parse_antwort(None) == {}
        assert gt.parse_antwort({"gemeinschaften": []}) == {}


class TestTarifAm:
    def test_jüngster_gueltiger(self):
        assert gt.tarif_am(DATEN["BEG"], OKT)["einspeisung_tag"] == 0.098
        assert gt.tarif_am(DATEN["BEG"], date(2026, 9, 30))["einspeisung_tag"] == 0.09
        assert gt.tarif_am(DATEN["BEG"], date(2027, 1, 1))["einspeisung_tag"] == 0.1

    def test_vor_dem_ersten_keiner(self):
        assert gt.tarif_am(DATEN["BEG"], date(2026, 1, 1)) is None

    def test_naechster(self):
        assert gt.naechster_tarif(DATEN["BEG"], OKT)["gueltig_ab"] == "2027-01-01"
        assert gt.naechster_tarif(DATEN["BEG"], date(2027, 2, 1)) is None

    def test_name_ohne_gross_klein(self):
        assert gt.eintrag_fuer(DATEN, " beg ") is DATEN["BEG"]
        assert gt.eintrag_fuer(DATEN, "") is None


class TestNachtfenster:
    def test_sonne_nach_innen_gerundet(self):
        assert gt.nachtfenster("sonne", SONNE) == (17, 7)
        assert gt.nachtfenster("sonne", (17.0, 7.0)) == (17, 7)

    def test_sonne_ohne_sonnenstand(self):
        assert gt.nachtfenster("sonne", None) is None

    def test_festes_fenster(self):
        assert gt.nachtfenster("22-6", None) == (22, 6)
        assert gt.nachtfenster(None, SONNE) is None


class TestUeberlagern:
    def test_server_tarif_ersetzt_saetze(self):
        cfg, info = gt.ueberlagern(BASIS, DATEN, OKT, SONNE)
        assert cfg["peakshare_price"] == 0.098
        assert cfg["peakshare_price_night"] == 0.108
        assert cfg["peakshare_bezug_price"] == 0.137
        assert (cfg["peakshare_night_start"], cfg["peakshare_night_end"]) == ("17:00", "07:00")
        # Sache des Mitglieds bleibt
        assert cfg["peakshare_share_pct"] == 100
        assert cfg["peakshare_weight"] == 0.01
        assert cfg["peakshare_bezug_ebene"] == "regional"
        assert info[0]["verband_name"] == "Energiewende Ansfelden"
        # Das Original bleibt unverändert
        assert BASIS["peakshare_price"] == 0.12

    def test_ohne_nachtsatz_kein_alter_handwert(self):
        cfg, _ = gt.ueberlagern(BASIS, DATEN, date(2026, 8, 1), SONNE)
        assert cfg["peakshare_price"] == 0.09
        assert cfg["peakshare_price_night"] is None   # = wie am Tag
        assert cfg["peakshare_night_start"] == "20:00"  # Fenster unberührt

    def test_ohne_sonnenstand_bleibt_das_fenster(self):
        cfg, _ = gt.ueberlagern(BASIS, DATEN, OKT, None)
        assert cfg["peakshare_price_night"] == 0.108
        assert cfg["peakshare_night_start"] == "20:00"

    def test_schalter_aus(self):
        cfg, info = gt.ueberlagern({**BASIS, "gemeinschaftstarif_auto": False}, DATEN, OKT, SONNE)
        assert cfg["peakshare_price"] == 0.12 and info == []

    def test_unbekannte_gemeinschaft(self):
        cfg, info = gt.ueberlagern({**BASIS, "peakshare_community": "Marchtrenk"}, DATEN, OKT, SONNE)
        assert cfg["peakshare_price"] == 0.12 and info == []

    def test_zweite_gemeinschaft(self):
        cfg, info = gt.ueberlagern(
            {**BASIS, "peakshare_community": "Marchtrenk", "peakshare_community_2": "Pucking",
             "peakshare_price_2": 0.05}, DATEN, OKT, SONNE)
        assert cfg["peakshare_price"] == 0.12
        assert cfg["peakshare_price_2"] == 0.098
        assert cfg["peakshare_bezug_price_2"] == 0.137
        assert [i["name"] for i in info] == ["Pucking"]

    def test_ohne_daten(self):
        cfg, info = gt.ueberlagern(BASIS, {}, OKT, SONNE)
        assert cfg is BASIS and info == []

    def test_eeg_price_rechnet_mit_den_server_saetzen(self):
        from custom_components.eeg_energy_optimizer.eeg_price import gemeinschaften_aus_config

        cfg, _ = gt.ueberlagern(BASIS, DATEN, OKT, SONNE)
        (g,) = gemeinschaften_aus_config(cfg)
        assert abs(g.wert_tag - (0.098 + 0.01)) < 1e-9
        assert abs(g.wert_nacht - (0.108 + 0.01)) < 1e-9


class TestAbrufGewuenscht:
    def test_regeln(self):
        assert gt.abruf_gewuenscht(BASIS)
        assert not gt.abruf_gewuenscht({**BASIS, "enable_peakshare": False})
        assert not gt.abruf_gewuenscht({**BASIS, "gemeinschaftstarif_auto": False})
        assert not gt.abruf_gewuenscht({**BASIS, "peakshare_community": ""})
