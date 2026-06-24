"""Vorlage für die lokale Konfiguration.

Kopieren nach ``config.py`` und Werte eintragen:

    cp config.example.py config.py
"""

HA_URL = "http://homeassistant.local:8123"
HA_TOKEN = "<langlebiges-Zugriffstoken-aus-HA>"

CALENDAR_ENTITY = "calendar.vinc"
REFRESH_SECONDS = 120     # Aktualisierungsintervall
LOOKAHEAD_DAYS = 7        # wie viele Tage in die Zukunft
