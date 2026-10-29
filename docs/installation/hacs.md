# HACS auf Home Assistant installieren

[HACS](https://hacs.xyz/) (Home Assistant Community Store) ist eine Art „App-Store“ für Erweiterungen, die nicht fest in Home Assistant eingebaut sind. Du brauchst HACS, um den EEG Energy Optimizer zu installieren. Je nach Anlage kommen darüber auch weitere Integrationen, etwa Huawei Solar, SolaX Modbus, Sigenergy oder Solcast.

> [!NOTE]
> Diese Anleitung fasst die offiziellen Schritte zusammen. Bei Abweichungen gilt die offizielle Doku: [hacs.xyz/docs/use/download/download](https://hacs.xyz/docs/use/download/download/)

## Voraussetzungen

- Home Assistant **OS** (für andere Installationsarten siehe offizielle HACS-Doku)
- Ein **GitHub-Konto** (kostenlos) — HACS braucht es beim Aktivieren. GitHub ist die Plattform, auf der die Erweiterungen liegen.
- Zugriff auf die Home Assistant Oberfläche als Administrator

## 1. HACS herunterladen (per „Get HACS" Add-on — kein Terminal nötig)

1. Klicke auf diesen Link, um das HACS Add-on-Repository hinzuzufügen:<br>
   **[➕ Add-on-Repository in Home Assistant öffnen](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhacs%2Faddons)**<br>
   _Alternativ manuell: **Einstellungen → Add-ons → Add-on Store** (in neueren Home-Assistant-Versionen heißt der Bereich **Apps**) → Drei-Punkte-Menü oben rechts → **Repositories** → `https://github.com/hacs/addons` eintragen → **Hinzufügen**_
2. Suche im **Add-on Store** nach **„Get HACS"** (falls nötig, lade die Seite neu) und **installiere** das Add-on.
3. **Starte** das Add-on. Es lädt HACS automatisch herunter. Den Fortschritt siehst du im Log (Protokoll) des Add-ons.
4. Danach kannst du das Add-on **wieder deinstallieren**. Es wird nur einmal gebraucht.

## 2. Home Assistant neu starten

1. Gehe zu **Einstellungen → System**
2. Klicke oben rechts auf das **Power-Symbol → Home Assistant neu starten**

## 3. HACS-Integration hinzufügen

1. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**
2. Suche nach **„HACS"** und wähle es aus
3. Bestätige die Hinweise (Checkboxen) und klicke auf **Senden**
4. Es erscheint ein **Gerätecode** — öffne [github.com/login/device](https://github.com/login/device)
5. Melde dich bei GitHub an und gib den Code ein
6. Erlaube HACS den Zugriff („Authorize“). Zurück in Home Assistant schließt sich das Fenster dann von selbst.

## 4. Prüfen

- In der Seitenleiste erscheint der Eintrag **HACS**
- Unter **HACS** kannst du jetzt Community-Integrationen suchen und installieren

## Alternative: Installation per Terminal

Du kannst oder willst das Add-on nicht nutzen? Dann geht es auch über ein Terminal (Befehlszeile) mit einem Download-Script:

1. **Einstellungen → Add-ons → Add-on Store** → **„Advanced SSH & Web Terminal"** (oder „Terminal & SSH") installieren und starten
2. Im Terminal ausführen:

   ```bash
   wget -O - https://get.hacs.xyz | bash -
   ```

3. Weiter mit Schritt 2 (Neustart) oben

## Nächster Schritt

→ [EEG Energy Optimizer über HACS installieren](eeg-integration.md)

## Häufige Probleme

| Problem | Lösung |
|---|---|
| „Get HACS" taucht im Add-on Store nicht auf | Seite neu laden (Strg+F5); prüfen, ob das Repository `hacs/addons` unter Repositories eingetragen ist |
| HACS taucht nach Neustart nicht unter Integrationen auf | Browser-Cache leeren (Strg+F5); wurde der Neustart wirklich durchgeführt? |
| GitHub-Code abgelaufen | Integration erneut hinzufügen, neuen Code anfordern |
