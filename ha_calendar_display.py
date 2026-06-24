#!/usr/bin/env python3
"""
HA Calendar Display — ILI9488 (480×320)
=======================================

Eine an iOS/Apple angelehnte Agenda für einen Home-Assistant-Kalender.

Aufbau
------
* ``config``        – Verbindungsdaten (HA_URL, Token …), liegt NICHT im Repo.
* ``data``          – Termine holen, parsen, abgelaufene filtern, gruppieren.
* ``theme``         – Farben, Abstände, Schrift (eine Stelle für das Aussehen).
* ``render``        – baut ein PIL-Image; weiß nichts von der Hardware.
* ``device``        – der originale, funktionierende ILI9488-Treiberblock.

Wichtig: Der Treiber-/Hardware-Teil (SPI-Init, Register, Rotation,
``device.display``) ist bewusst unverändert aus der laufenden Version
übernommen – nur in ``init_device()`` gekapselt.

Benutzung
---------
    ha_calendar_display.py                    # normal auf dem Gerät
    ha_calendar_display.py --preview out.png  # ein Bild rendern, ohne Hardware
    ha_calendar_display.py --preview out.png --demo   # mit Beispieldaten
"""

import sys
import gc
import time
import json
import signal
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, date, timedelta

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(BASE))
try:
    import config
    HA_URL = config.HA_URL
    HA_TOKEN = config.HA_TOKEN
    CALENDAR_ENTITY = getattr(config, "CALENDAR_ENTITY", "calendar.vinc")
    REFRESH_SECONDS = getattr(config, "REFRESH_SECONDS", 120)
    LOOKAHEAD_DAYS = getattr(config, "LOOKAHEAD_DAYS", 7)
except Exception:  # noqa: BLE001  – ohne config laufen nur Demo-Previews
    HA_URL = HA_TOKEN = None
    CALENDAR_ENTITY = "calendar.vinc"
    REFRESH_SECONDS = 120
    LOOKAHEAD_DAYS = 7


# ─────────────────────────────────────────────────────────────────────────────
#  Theme  –  alles Visuelle an einer Stelle (iOS-Dark inspiriert)
# ─────────────────────────────────────────────────────────────────────────────
W, H = 480, 320          # Display-Auflösung (vom Treiber vorgegeben, nicht ändern)
RAIL_W = 150             # Breite der linken Datums-Leiste
PAD = 16                 # Standard-Innenabstand


class Color:
    bg        = (8, 8, 11)        # fast schwarzer Hintergrund
    rail      = (19, 19, 23)      # erhöhte linke Leiste
    card      = (28, 28, 32)      # Event-Karte
    hairline  = (48, 48, 54)      # 1px-Trennlinien / Konturen
    text      = (240, 240, 246)   # primär
    text2     = (152, 152, 162)   # sekundär
    text3     = (104, 104, 114)   # tertiär / dezent
    accent    = (255, 69, 58)     # Apple-Calendar-Rot (heute)


# Pro Termin eine stabile Farbe (iOS-System-Palette).
PALETTE = [
    (10, 132, 255),   # blau
    (48, 209, 88),    # grün
    (255, 159, 10),   # orange
    (191, 90, 242),   # violett
    (100, 210, 255),  # cyan
    (255, 55, 95),    # pink
    (94, 92, 230),    # indigo
    (255, 214, 10),   # gelb
    (102, 212, 207),  # mint
]

WEEKDAYS  = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
             "Freitag", "Samstag", "Sonntag"]
WD_SHORT  = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS    = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember"]


# ─────────────────────────────────────────────────────────────────────────────
#  Schrift  –  Inter (variabel) mit Fallback auf DejaVu
# ─────────────────────────────────────────────────────────────────────────────
FONT_PATH = BASE / "fonts" / "Inter.ttf"
_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf"
_font_cache: dict = {}


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """Gecachte Schrift in der gewünschten Größe/Stärke."""
    key = (size, weight)
    cached = _font_cache.get(key)
    if cached is None:
        try:
            f = ImageFont.truetype(str(FONT_PATH), size)
            f.set_variation_by_name(weight.encode())
        except Exception:  # noqa: BLE001  – Fallback, falls Inter fehlt
            bold = "-Bold" if weight in ("Medium", "SemiBold", "Bold") else ""
            f = ImageFont.truetype(_DEJAVU.format(bold), size)
        _font_cache[key] = f
        cached = f
    return cached


# ─────────────────────────────────────────────────────────────────────────────
#  Zeichen-Helfer
# ─────────────────────────────────────────────────────────────────────────────
def ellipsize(draw, text, fnt, max_w):
    """Text auf max_w kürzen und mit … abschließen."""
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return (text + "…") if text else "…"


def draw_tracked(draw, xy, text, fnt, fill, tracking=1.0):
    """Text mit etwas Buchstabenabstand (für dezente Versal-Labels)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking
    return x


# ─────────────────────────────────────────────────────────────────────────────
#  Datenmodell
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Event:
    start: datetime
    end: datetime
    all_day: bool
    summary: str
    location: str
    color: tuple


def _parse_dt(value):
    """ISO-String -> (datetime, is_all_day). Liefert naive Lokalzeit."""
    if not value:
        return None, False
    if "T" in value:                      # Zeitpunkt mit Uhrzeit
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt, False
        except ValueError:
            return None, False
    try:                                  # reines Datum -> ganztägig
        d = date.fromisoformat(value[:10])
        return datetime(d.year, d.month, d.day), True
    except ValueError:
        return None, False


def _color_for(summary: str) -> tuple:
    """Deterministische Farbe pro Termin-Titel."""
    h = 0
    for ch in summary:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PALETTE[h % len(PALETTE)]


def build_events(raw, now):
    """Roh-JSON von HA -> sortierte, gefilterte Event-Liste (ohne Abgelaufene)."""
    events = []
    for item in raw:
        start, sad = _parse_dt(item.get("start", ""))
        end, ead = _parse_dt(item.get("end", ""))
        if start is None:
            continue
        all_day = sad or ead
        if end is None:
            end = start + timedelta(hours=1)
        summary = (item.get("summary") or "Termin").strip()
        location = (item.get("location") or item.get("description") or "")
        location = location.strip().replace("\n", " ")
        events.append(Event(start, end, all_day, summary, location,
                            _color_for(summary)))

    # Abgelaufenes raus: alles, dessen Ende in der Vergangenheit liegt.
    events = [e for e in events if e.end > now]
    # Ganztägige zuerst, sonst nach Startzeit.
    events.sort(key=lambda e: (e.start, not e.all_day))
    return events


def group_by_day(events):
    """[(date, [events])] in chronologischer Reihenfolge."""
    groups = []
    for ev in events:
        day = ev.start.date()
        if groups and groups[-1][0] == day:
            groups[-1][1].append(ev)
        else:
            groups.append((day, [ev]))
    return groups


def fetch_events(now):
    """Termine der nächsten LOOKAHEAD_DAYS Tage von Home Assistant holen."""
    if not HA_TOKEN:
        raise RuntimeError("config.py fehlt oder unvollständig")
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=LOOKAHEAD_DAYS)
    body = json.dumps({
        "entity_id": CALENDAR_ENTITY,
        "start_date_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date_time": end.strftime("%Y-%m-%d %H:%M:%S"),
    }).encode()
    req = urllib.request.Request(
        f"{HA_URL}/api/services/calendar/get_events?return_response",
        data=body,
        headers={"Authorization": f"Bearer {HA_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return (data.get("service_response", {})
                .get(CALENDAR_ENTITY, {})
                .get("events", []))


# ─────────────────────────────────────────────────────────────────────────────
#  Rendering
# ─────────────────────────────────────────────────────────────────────────────
def render(now, events, error=None):
    """Komplettes 480×320-Bild zusammensetzen."""
    img = Image.new("RGB", (W, H), Color.bg)
    draw = ImageDraw.Draw(img)
    _draw_rail(draw, now, events)
    _draw_agenda(draw, now, events, error)
    return img


def _draw_rail(draw, now, events):
    """Linke Leiste: großes Datum, Wochenstrip, Uhr."""
    draw.rectangle([0, 0, RAIL_W - 1, H], fill=Color.rail)
    draw.line([(RAIL_W, 0), (RAIL_W, H)], fill=Color.hairline)

    x = PAD
    draw_tracked(draw, (x, 22), WEEKDAYS[now.weekday()].upper(),
                 font(12, "SemiBold"), Color.accent, tracking=1.5)
    draw.text((x - 2, 32), str(now.day), font=font(60, "Bold"),
              fill=Color.text, anchor="lt")
    draw.text((x, 100), f"{MONTHS[now.month - 1]} {now.year}",
              font=font(14, "Medium"), fill=Color.text2, anchor="lt")
    draw.line([(x, 130), (RAIL_W - x, 130)], fill=Color.hairline)

    _draw_week_strip(draw, now, events, top=152)

    draw.line([(x, 214), (RAIL_W - x, 214)], fill=Color.hairline)
    draw.text((x, H - 28), now.strftime("%H:%M"), font=font(30, "Bold"),
              fill=Color.text, anchor="lb")
    today = now.date()
    count = sum(1 for e in events if e.start.date() == today)
    kw = now.isocalendar()[1]
    sub = f"KW {kw}  ·  {count} heute" if count else f"KW {kw}"
    draw.text((x, H - 23), sub, font=font(11, "Medium"),
              fill=Color.text3, anchor="lt")


def _draw_week_strip(draw, now, events, top):
    """Mini-Wochenstrip Mo–So mit hervorgehobenem Heute + Event-Punkten."""
    monday = now.date() - timedelta(days=now.weekday())
    today = now.date()
    days_with_events = {e.start.date() for e in events}

    left, right = 12, RAIL_W - 12
    col = (right - left) / 7
    for i in range(7):
        cx = left + col * i + col / 2
        day = monday + timedelta(days=i)
        draw.text((cx, top), WD_SHORT[i], font=font(9, "Medium"),
                  fill=Color.text3, anchor="mt")
        cy = top + 22
        if day == today:
            draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=Color.accent)
            draw.text((cx, cy), str(day.day), font=font(11, "Bold"),
                      fill=Color.text, anchor="mm")
        else:
            fill = Color.text3 if day < today else Color.text2
            draw.text((cx, cy), str(day.day), font=font(11, "Medium"),
                      fill=fill, anchor="mm")
            if day in days_with_events:
                draw.ellipse([cx - 1.5, top + 36, cx + 1.5, top + 39],
                             fill=Color.accent)


CARD_H = 50      # einheitliche Kartenhöhe für ruhigen Rhythmus


def _card_height(ev) -> int:
    return CARD_H


def _draw_card(draw, x, y, w, ev):
    """Eine Termin-Karte: Akzentbalken, Zeit, Titel, Zusatzzeile."""
    if ev.all_day:
        sub = f"Ganztägig · {ev.location}" if ev.location else "Ganztägig"
        time_col = None
    else:
        sub = ev.location or None
        time_col = (ev.start.strftime("%H:%M"), ev.end.strftime("%H:%M"))

    h = CARD_H
    draw.rounded_rectangle([x, y, x + w, y + h], radius=12,
                           fill=Color.card, outline=Color.hairline, width=1)
    draw.rounded_rectangle([x + 11, y + 11, x + 14, y + h - 11],
                           radius=2, fill=ev.color)

    if time_col:
        tx = x + 26
        draw.text((tx, y + 11), time_col[0], font=font(14, "SemiBold"),
                  fill=Color.text, anchor="lt")
        draw.text((tx, y + 29), time_col[1], font=font(11, "Regular"),
                  fill=Color.text2, anchor="lt")
        title_x = tx + 50
    else:
        title_x = x + 26

    avail = (x + w - 14) - title_x
    if sub:
        draw.text((title_x, y + 11),
                  ellipsize(draw, ev.summary, font(15, "SemiBold"), avail),
                  font=font(15, "SemiBold"), fill=Color.text, anchor="lt")
        draw.text((title_x, y + 31),
                  ellipsize(draw, sub, font(12, "Regular"), avail),
                  font=font(12, "Regular"), fill=Color.text2, anchor="lt")
    else:
        draw.text((title_x, y + h // 2),
                  ellipsize(draw, ev.summary, font(15, "SemiBold"), avail),
                  font=font(15, "SemiBold"), fill=Color.text, anchor="lm")


def _day_label(day, today):
    delta = (day - today).days
    if delta == 0:
        return "HEUTE"
    if delta == 1:
        return "MORGEN"
    return f"{WEEKDAYS[day.weekday()].upper()} · {day.day}.{day.month}."


def _draw_message(draw, x0, w, title, subtitle):
    """Zentrierte Meldung (leer / Fehler) im Agenda-Bereich."""
    cx, cy = x0 + w // 2, H // 2
    draw.text((cx, cy - 12), title, font=font(17, "SemiBold"),
              fill=Color.text, anchor="mm")
    draw.text((cx, cy + 14), ellipsize(draw, subtitle, font(12, "Regular"), w),
              font=font(12, "Regular"), fill=Color.text2, anchor="mm")


def _draw_agenda(draw, now, events, error):
    """Rechter Bereich: nach Tagen gruppierte Termin-Karten."""
    x0 = RAIL_W + PAD
    w = W - PAD - x0

    if error:
        _draw_message(draw, x0, w, "Keine Verbindung",
                      "Home Assistant nicht erreichbar")
        return
    if not events:
        _draw_message(draw, x0, w, "Keine Termine",
                      f"Die nächsten {LOOKAHEAD_DAYS} Tage sind frei")
        return

    today = now.date()
    y = 18
    shown = 0
    stop = False
    for day, day_events in group_by_day(events):
        if y > H - 46:               # kein Platz mehr für Kopf + Karte
            break
        draw.text((x0, y), _day_label(day, today), font=font(11, "SemiBold"),
                  fill=Color.accent if day == today else Color.text2)
        y += 22
        for ev in day_events:
            ch = _card_height(ev)
            if y + ch > H - 16:
                stop = True
                break
            _draw_card(draw, x0, y, w, ev)
            y += ch + 8
            shown += 1
        if stop:
            break

    remaining = len(events) - shown
    if remaining > 0 and y < H - 14:
        draw.text((x0, y + 2), f"+ {remaining} weitere",
                  font=font(11, "SemiBold"), fill=Color.text3)


# ─────────────────────────────────────────────────────────────────────────────
#  Demo-Daten (für --preview --demo)
# ─────────────────────────────────────────────────────────────────────────────
def demo_events(now):
    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    today = now.date()
    base = now.replace(minute=0, second=0, microsecond=0)
    raw = [
        {"summary": "Geburtstag Mama",
         "start": today.isoformat(),
         "end": (today + timedelta(days=1)).isoformat()},
        {"summary": "Zahnarzt Dr. Meyer", "location": "Hauptstraße 12",
         "start": iso(base + timedelta(hours=2)),
         "end": iso(base + timedelta(hours=3))},
        {"summary": "Lauftraining im Park",
         "start": iso(base + timedelta(hours=5)),
         "end": iso(base + timedelta(hours=6))},
        {"summary": "Daily Standup", "location": "Microsoft Teams",
         "start": iso(base + timedelta(days=1, hours=-base.hour + 9)),
         "end": iso(base + timedelta(days=1, hours=-base.hour + 9, minutes=30))},
        {"summary": "Mittagessen mit Lisa", "location": "Café Central",
         "start": iso(base + timedelta(days=1, hours=-base.hour + 12)),
         "end": iso(base + timedelta(days=1, hours=-base.hour + 13))},
        {"summary": "Konzert Philharmonie — Beethoven 9. Sinfonie",
         "start": iso(base + timedelta(days=2, hours=-base.hour + 20)),
         "end": iso(base + timedelta(days=2, hours=-base.hour + 22))},
        {"summary": "Urlaub",
         "start": (today + timedelta(days=4)).isoformat(),
         "end": (today + timedelta(days=7)).isoformat()},
    ]
    return build_events(raw, now)


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware  –  ORIGINAL-Treiber, unverändert (nur gekapselt)
# ─────────────────────────────────────────────────────────────────────────────
def init_device():
    from luma.lcd.device import ili9488
    from luma.core.interface.serial import spi

    serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=24, bus_speed_hz=32000000)
    device = ili9488(serial, rotate=0, width=480, height=320, gpio_LIGHT=18, active_low=False)
    device.show()
    for c, d in [(0xB1, [0xA0, 0x11]), (0xB6, [0x02, 0x22, 0x3B]), (0xC5, [0x00, 0x48, 0x44]), (0xC0, [0x17, 0x15]), (0xC1, [0x44])]:
        device.command(c); device.data(d)
    device.command(0x20); device.command(0x38)
    device.command(0x36); device.data([0x68])
    time.sleep(0.05)
    return device


# ─────────────────────────────────────────────────────────────────────────────
#  Ablauf
# ─────────────────────────────────────────────────────────────────────────────
def run():
    device = init_device()
    print("HA Calendar Display gestartet", flush=True)
    while True:
        now = datetime.now()
        events, error = [], None
        try:
            events = build_events(fetch_events(now), now)
        except Exception as exc:  # noqa: BLE001  – Fehler auf dem Display zeigen
            error = str(exc)
            print(f"Fehler: {exc}", flush=True)
        try:
            device.display(render(now, events, error))
        except Exception as exc:  # noqa: BLE001
            print(f"Display-Fehler: {exc}", flush=True)
        gc.collect()
        time.sleep(REFRESH_SECONDS)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    args = sys.argv[1:]
    if "--preview" in args:
        i = args.index("--preview")
        out = args[i + 1] if i + 1 < len(args) else "preview.png"
        now = datetime.now()
        if "--demo" in args:
            events, error = demo_events(now), None
        else:
            try:
                events, error = build_events(fetch_events(now), now), None
            except Exception as exc:  # noqa: BLE001
                events, error = [], str(exc)
        render(now, events, error).save(out)
        print(f"Vorschau gespeichert: {out}", flush=True)
        return

    run()


if __name__ == "__main__":
    main()
