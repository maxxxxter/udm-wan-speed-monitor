# Unveroeffentlicht

- TLS-Zertifikats-Pinning (Trust on First Use) schuetzt das UDM-Passwort vor abgefangenen Verbindungen
- Live-Tray-Symbol mit Down-/Uplink-Balken und Tooltip mit aktuellen Werten und Ping
- Ausfallprotokoll `ereignisse.csv` mit Ausfalldauer und Windows-Benachrichtigungen im Tray
- WAN-Leitung offline wird rot als OFFLINE angezeigt
- Durchschnitt und Spitze der letzten 5 Minuten pro Graph
- Abgelehnte Anmeldung stoppt die Wiederholungen statt das Konto zu sperren
- Pro Abfrage nur noch 2 statt 3 API-Aufrufe, und nach dem ersten Abruf wird nur noch das Gateway statt aller Geraete geladen
- Laufzeiten (`uptime`), Paketzaehler und Speedtest-Werte werden nicht mehr faelschlich als Durchsatz gelesen
- Speichern der Einstellungen setzt Fenstergroesse und -position nicht mehr zurueck
- Kurze Aussetzer lassen die letzten Werte stehen statt sofort `--` anzuzeigen
- Beenden haengt nicht mehr bis zu 5 Sekunden, wenn die UDM nicht erreichbar ist
- Konfiguration wird atomar gespeichert

# UDM WAN Speed Monitor 1.0.1

- Prevents the window from freezing during slow logout or connection failures
- Retries temporary UDM/API failures with capped backoff
- Ignores callbacks from stopped or superseded monitor sessions

- Dark desktop monitor for UDM WAN throughput
- Separate WAN 1 and optional WAN 2 live graphs
- Settings window with saved credentials
- Optional Windows autostart
- Minimize to tray and restore from tray
- CC BY 4.0 licensing
