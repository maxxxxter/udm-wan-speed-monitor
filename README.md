# UDM WAN Speed Monitor

[![Download MSI](https://img.shields.io/badge/Download-MSI%20Installer-00c853?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/maxxxxter/udm-wan-speed-monitor/raw/main/dist/UDM-WAN-Speed-Monitor-1.0.msi)
[![Download EXE](https://img.shields.io/badge/Download-Portable%20EXE-0078d4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/maxxxxter/udm-wan-speed-monitor/raw/main/dist/udm-wan-speed-monitor-1.0.exe)

Desktop-App fuer Windows, die den aktuellen WAN-Downlink und Uplink deiner UDM Pro Max anzeigt.

## Features

- Dark Theme
- Einstellungsfenster fuer Router-IP, Benutzername und Passwort
- Passwort kann dauerhaft gespeichert werden
- Passwort wird per Windows-DPAPI verschluesselt gespeichert, nicht im Klartext
- Abmelden-Funktion zum Wechseln von Router, Benutzer oder Passwort
- Kein API-Key noetig, solange die UDM-Login-Daten vorhanden sind

## Start

1. `run-udm-wan-speed-monitor.bat` starten.
2. In `Einstellungen` Router-IP oder URL, Benutzername und Passwort eintragen.
3. Optional `Passwort dauerhaft speichern` aktiv lassen.
4. `Speichern` startet das Monitoring direkt.

## Dateien

- `app.py`: Anwendung
- `config.json`: wird unter `%LOCALAPPDATA%\UDM WAN Speed Monitor` automatisch angelegt
- `run-udm-wan-speed-monitor.bat`: Starter fuer Windows
- `build_release.ps1`: baut die Release-EXE
- `build_msi.ps1`: baut den klassischen MSI-Installer
- `sign_release.ps1`: signiert EXE und MSI mit signtool.exe
- `dist/UDM-WAN-Speed-Monitor-1.0.msi`: MSI-Installer fuer Windows
- `generate_checksums.ps1`: erzeugt SHA256-Pruefsummen fuer die Releases
- `dist/SHA256SUMS.txt`: SHA256-Pruefsummen der Release-Dateien


## Lizenz

CC BY 4.0


## Windows-Hinweise

- Die EXE enthaelt jetzt saubere Windows-Dateiinformationen fuer Produktname, Version und Hersteller.
- Fuer eine moeglichst warnfreie Ausfuehrung unter Windows 11 bleibt Code Signing der wichtigste naechste Schritt.
- Den Release-Build erzeugst du konsistent mit `build_release.ps1`.


## Integritaetspruefung

- Lade nach Moeglichkeit den MSI-Installer statt der losen EXE.
- Vergleiche die SHA256-Pruefsumme deiner Datei mit `dist/SHA256SUMS.txt`.
- Unter Windows kannst du das lokal so pruefen:

```powershell
Get-FileHash .\UDM-WAN-Speed-Monitor-1.0.msi -Algorithm SHA256
Get-FileHash .\udm-wan-speed-monitor-1.0.exe -Algorithm SHA256
```
