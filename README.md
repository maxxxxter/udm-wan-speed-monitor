# UDM WAN Speed Monitor

[![Download MSI](https://img.shields.io/badge/Download-MSI%20Installer-00c853?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/maxxxxter/udm-wan-speed-monitor/raw/main/dist/UDM-WAN-Speed-Monitor-1.0.1.msi)
[![Download EXE](https://img.shields.io/badge/Download-Portable%20EXE-0078d4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/maxxxxter/udm-wan-speed-monitor/raw/main/dist/udm-wan-speed-monitor-1.0.1.exe)

Desktop-App fuer Windows, die den aktuellen WAN-Downlink und Uplink deiner UDM Pro Max anzeigt.

![UDM WAN Speed Monitor Screenshot](assets/UDM%20WAN%20Speed%20Monitor.png)

## Features

- Dark Theme
- Einstellungsfenster fuer Router-IP, Benutzername und Passwort
- Passwort kann dauerhaft gespeichert werden
- Passwort wird per Windows-DPAPI verschluesselt gespeichert, nicht im Klartext
- Abmelden-Funktion zum Wechseln von Router, Benutzer oder Passwort
- Kein API-Key noetig, solange die UDM-Login-Daten vorhanden sind
- Zertifikats-Pinning (Trust on First Use): Das selbstsignierte Zertifikat der UDM wird beim ersten Kontakt gemerkt. Meldet sich spaeter ein anderes Zertifikat, wird vor dem Senden des Passworts nachgefragt
- Live-Tray-Symbol: Im Tray zeigen zwei Balken Down- und Uplink, der Tooltip die aktuellen Werte und den Ping. Ein roter Rahmen signalisiert einen Ausfall
- Ausfallprotokoll: Leitungsausfaelle, Wiederherstellungen (mit Dauer) und Verbindungsabbrueche zur UDM landen in `ereignisse.csv` (Excel-freundlich) und als Windows-Benachrichtigung, solange die App im Tray liegt. Oeffnen ueber `Datei` oder das Tray-Menue
- Durchschnitt und Spitze der letzten 5 Minuten unter jedem Graphen
- Bufferbloat-Erkennung: Die App vergleicht den Ping bei ruhiger Leitung mit dem Ping unter Last. Steigt er um 30 ms oder mehr, erscheint im WAN-Titel `Last +X ms` mit Note (A+ bis D), ein Eintrag im Ausfallprotokoll und der Tipp, Smart Queues in der UDM zu aktivieren
- Top-Verbraucher: Unter den Graphen stehen die drei Geraete mit dem hoechsten aktuellen Durchsatz. Abschaltbar in den Einstellungen
- Falsches Passwort stoppt das Monitoring sofort, damit die UDM das Konto nicht wegen Fehlversuchen sperrt

## Start

1. `run-udm-wan-speed-monitor.bat` starten.
2. In `Einstellungen` Router-IP oder URL, Benutzername und Passwort eintragen.
3. Optional `Passwort dauerhaft speichern` aktiv lassen.
4. `Speichern` startet das Monitoring direkt.

## Dateien

- `app.py`: Anwendung
- `config.json`: wird unter `%LOCALAPPDATA%\UDM WAN Speed Monitor` automatisch angelegt
- `ereignisse.csv`: Ausfallprotokoll im selben Ordner
- `run-udm-wan-speed-monitor.bat`: Starter fuer Windows
- `build_release.ps1`: baut die Release-EXE
- `build_msi.ps1`: baut den klassischen MSI-Installer
- `sign_release.ps1`: signiert EXE und MSI mit signtool.exe
- `dist/UDM-WAN-Speed-Monitor-1.0.1.msi`: MSI-Installer fuer Windows
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
Get-FileHash .\UDM-WAN-Speed-Monitor-1.0.1.msi -Algorithm SHA256
Get-FileHash .\udm-wan-speed-monitor-1.0.1.exe -Algorithm SHA256
```
