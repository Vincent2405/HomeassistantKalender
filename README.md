# HA Calendar Display

Apple-inspirierte Wochen-Agenda für einen Home-Assistant-Kalender auf einem
ILI9488-TFT (480×320), betrieben an einem Raspberry Pi Zero 2 W.

![Layout](docs/preview.png)

## Aufbau

| Bereich            | Datei / Abschnitt        | Aufgabe                                   |
| ------------------ | ------------------------ | ----------------------------------------- |
| Konfiguration      | `config.py`              | HA-URL, Token, Kalender, Intervall        |
| Daten              | `build_events`, `fetch_events` | Termine holen, parsen, **Abgelaufene filtern**, gruppieren |
| Theme              | `Color`, `PALETTE`, `font()` | Farben, Schrift, Abstände – das Aussehen |
| Rendering          | `render`, `_draw_rail`, `_draw_agenda`, `_draw_card` | baut das PIL-Bild |
| Hardware           | `init_device`            | **unveränderter** ILI9488-Treiberblock    |

Der Treiber-/Hardware-Teil ist bewusst 1:1 aus der laufenden Version
übernommen. Alles Visuelle steckt im Theme- und Rendering-Teil.

## Features

- Linke „Hero"-Leiste: großes Datum, Mini-Wochenstrip (heute hervorgehoben,
  Punkte an Tagen mit Terminen), Uhr und KW.
- Rechte Agenda: nach Tagen gruppierte Karten (`HEUTE` / `MORGEN` / Wochentag),
  Akzentbalken pro Termin, Start-/Endzeit, Ort/Beschreibung, `+N weitere`.
- **Abgelaufene Termine** (Ende in der Vergangenheit) werden ausgeblendet.
- Rollendes Fenster der nächsten `LOOKAHEAD_DAYS` Tage.
- Saubere Leer- und Fehlerzustände auf dem Display.

## Einrichtung

```bash
cp config.example.py config.py     # Werte eintragen
python3 ha_calendar_display.py     # direkt starten
```

### Vorschau ohne Hardware

```bash
python3 ha_calendar_display.py --preview out.png          # echte Daten
python3 ha_calendar_display.py --preview out.png --demo   # Beispieldaten
```

### Als Dienst

```bash
sudo cp ha-calendar-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ha-calendar-display.service
```

## Schrift

[Inter](https://rsms.me/inter/) (SIL Open Font License) liegt unter
`fonts/Inter.ttf`. Fehlt die Datei, fällt die Anzeige automatisch auf DejaVu
zurück.
# HomeassistentKalender
