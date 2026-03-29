# UDM WAN Speed Monitor

[![Download EXE](https://img.shields.io/badge/Download-UDM%20WAN%20Speed%20Monitor%201.0-00c853?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/maxxxxter/udm-wan-speed-monitor/raw/main/dist/udm-wan-speed-monitor-1.0.exe)

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


## Lizenz

CC BY 4.0
