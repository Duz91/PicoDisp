"""
BTC-Ticker mit Verlaufsgrafik für Raspberry Pi Pico 2 W + 2,9\" Cap-Touch E-Paper.

Voraussetzungen:
- MicroPython-Firmware auf dem Pico 2 W.
- Hersteller-Treiberdatei für das E-Paper (z.B. epd2in9.py) im Dateisystem.
- secrets.py mit WIFI_SSID / WIFI_PASSWORD (siehe secrets.example.py).
"""
import math
import time

import framebuf
import network
import usocket
import ujson as json
import urequests
from machine import Pin

from display import EPD_HEIGHT, EPD_WIDTH, create_display
from secrets import WIFI_PASSWORD, WIFI_SSID

FG_COLOR = 0  # Schwarz auf dem getesteten Treiber
BG_COLOR = 1  # Weiß auf dem getesteten Treiber


DEBUG = True
REQUEST_TIMEOUT = 8  # Sekunden für HTTP-Requests
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur"
)
COINGECKO_HISTORY_24H_URL = (
    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=eur&days=1"
)  # 24h
COINGECKO_HISTORY_365D_URL = (
    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=eur&days=365"
)  # 365 Tage
PRICE_REFRESH_SECONDS = 60  # Update-Intervall für Live-Preis
SCREEN_SWAP_SECONDS = 60  # Wechselintervall zwischen 24h- und 365d-Ansicht
LOOP_SLEEP_SECONDS = 5  # Hauptschleifen-Schlafdauer, um Screens zu wechseln
HISTORY_LENGTH_24H = 96  # ~96 Punkte (15min Raster)
HISTORY_LENGTH_365D = 365  # Grobe Tagespunkte (oder weniger)
HISTORY_FETCH_TIMEOUT = 8  # Sekunden, um Startblocker zu vermeiden
NTP_RETRIES = 3
NTP_HOST = "pool.ntp.org"


class RingBuffer:
    def __init__(self, length):
        self.length = length
        self.data = []

    def append(self, value):
        if len(self.data) >= self.length:
            self.data.pop(0)
        self.data.append(value)

    def minmax(self):
        if not self.data:
            return 0, 1
        return min(self.data), max(self.data)

    def extend(self, values):
        for value in values:
            self.append(value)


def log(msg):
    if DEBUG:
        try:
            now = time.ticks_ms()
            print(f"[{now:>8}] {msg}")
        except Exception:
            print(msg)


def _set_default_socket_timeout(seconds):
    try:
        usocket.setdefaulttimeout(seconds)
    except Exception as exc:
        log(f"Konnte Socket-Timeout nicht setzen: {exc}")


# --- Zeitzone Europe/Berlin (MEZ/MESZ) ---
def _weekday(year, month, day):
    """Wochentag nach Sakamoto (0=Sonntag, 1=Montag, ... 6=Samstag)."""
    t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4]
    if month < 3:
        year -= 1
    return (year + year // 4 - year // 100 + year // 400 + t[month - 1] + day) % 7


def _last_sunday(year, month):
    """Tag des letzten Sonntags im Monat."""
    last_day = 31
    while last_day > 27:  # alle relevanten Monate haben >=28 Tage
        if _weekday(year, month, last_day) == 0:
            return last_day
        last_day -= 1
    return last_day


def _berlin_offset_seconds(utc_tuple):
    """UTC-Offset (Sekunden) für Europe/Berlin inkl. Sommerzeit."""
    year, month, day, hour = utc_tuple[0], utc_tuple[1], utc_tuple[2], utc_tuple[3]
    last_sun_mar = _last_sunday(year, 3)
    last_sun_oct = _last_sunday(year, 10)
    dst = False
    if 4 <= month <= 9:
        dst = True
    elif month == 3:
        if day > last_sun_mar or (day == last_sun_mar and hour >= 2):
            dst = True
    elif month == 10:
        if day < last_sun_oct or (day == last_sun_oct and hour < 3):
            dst = True
    return 7200 if dst else 3600


def _now_local_berlin():
    """Lokale Zeit Europe/Berlin als time.localtime()-Tuple."""
    utc = time.localtime()
    offset = _berlin_offset_seconds(utc)
    ts = time.mktime(utc) + offset
    return time.localtime(ts)


def sync_time():
    """Synchronisiert die RTC per NTP, falls verfügbar."""
    try:
        import ntptime  # type: ignore
    except ImportError:
        log("ntptime-Modul nicht verfügbar, überspringe Zeitabgleich")
        return

    ntptime.host = NTP_HOST
    for attempt in range(1, NTP_RETRIES + 1):
        try:
            ntptime.settime()
            log(f"NTP-Sync erfolgreich (Versuch {attempt})")
            return
        except Exception as exc:
            log(f"NTP-Sync fehlgeschlagen (Versuch {attempt}): {exc}")
            time.sleep(1)
    log("NTP-Sync endgültig fehlgeschlagen, verwende lokale Zeit")


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    log("WLAN aktivieren")
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for tries in range(20):  # ~10 Sekunden
            if wlan.isconnected():
                break
            time.sleep(0.5)
            log(f"WLAN noch nicht verbunden (Versuch {tries + 1})")
    if not wlan.isconnected():
        raise RuntimeError("WLAN-Verbindung fehlgeschlagen")
    log(f"WLAN verbunden: {wlan.ifconfig()}")
    return wlan


def _get_json(url, timeout=None):
    """HTTP GET mit optionalem Timeout (fällt bei fehlender Timeout-Unterstützung zurück)."""
    log(f"HTTP GET (urequests): {url}")
    response = None
    try:
        try:
            response = urequests.get(url, timeout=timeout)
        except TypeError:
            # Ältere urequests-Version kennt kein timeout-Argument.
            if timeout is not None:
                try:
                    usocket.setdefaulttimeout(timeout)
                except Exception:
                    pass
            response = urequests.get(url)
        data = response.json()
        return data
    except Exception as exc:
        log(f"urequests fehlgeschlagen: {exc}")
        raise
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _manual_get_json(url, timeout=8):
    """Kleiner HTTP-Client mit explizitem Socket-Timeout (Fallback für MicroPython ohne Timeout)."""
    log(f"HTTP GET (manual): {url}")
    proto_split = url.split("://", 1)
    if len(proto_split) != 2:
        raise ValueError("Ungültige URL")
    scheme, rest = proto_split
    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host, path = rest, "/"
    port = 443 if scheme == "https" else 80

    addr = usocket.getaddrinfo(host, port)[0][-1]
    s = usocket.socket()
    try:
        s.settimeout(timeout)
        s.connect(addr)
        if scheme == "https":
            import ussl

            s = ussl.wrap_socket(s, server_hostname=host)
        req = (
            f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: pico\r\n"
            "Connection: close\r\n\r\n"
        )
        s.write(req.encode())
        # Header überspringen
        while True:
            line = s.readline()
            if not line or line == b"\r\n":
                break
        chunks = []
        while True:
            chunk = s.read(512)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        return json.loads(body)
    finally:
        try:
            s.close()
        except Exception:
            pass


def fetch_price_eur():
    data = _get_json(COINGECKO_URL, timeout=REQUEST_TIMEOUT)
    return float(data["bitcoin"]["eur"])


def fetch_history(url, target_len, label):
    start_ms = time.ticks_ms()
    log(f"Starte Historie-Download ({label})...")
    try:
        data = _get_json(url, timeout=HISTORY_FETCH_TIMEOUT)
    except Exception:
        log("Fallback: manuelles HTTP wegen Timeout/Fehler")
        data = _manual_get_json(url, timeout=HISTORY_FETCH_TIMEOUT)
    if "status" in data and "error_message" in data["status"]:
        msg = data["status"].get("error_message", "API-Fehler")
        log(f"API-Fehler vom History-Endpoint: {msg}")
        data = _manual_get_json(url, timeout=HISTORY_FETCH_TIMEOUT)
    prices = [float(p[1]) for p in data.get("prices", [])]
    if not prices:
        log("Historie leer")
        return []
    # Begrenze, um RAM zu schonen, bevor wir auf target_len resamplen
    if len(prices) > 2 * target_len:
        prices = prices[-2 * target_len :]
    resampled = _resample(prices, target_len)
    dur = time.ticks_diff(time.ticks_ms(), start_ms)
    log(f"Historie geladen: {len(resampled)} Punkte ({label}), Dauer {dur} ms")
    return resampled


def draw_chart(fb, history, span_label):
    # Linken Rand vergrößern für Y-Achsen-Beschriftung
    chart_left = 55
    chart_top = 34
    chart_width = EPD_WIDTH - chart_left - 6
    chart_height = 64
    points = len(history.data)

    fb.rect(chart_left - 1, chart_top - 1, chart_width + 2, chart_height + 2, FG_COLOR)
    if not history.data:
        fb.text("keine Daten", chart_left + 4, chart_top + chart_height // 2, FG_COLOR)
        return

    min_val, max_val = history.minmax()
    span = max(max_val - min_val, 1e-6)
    step_x = chart_width / max(1, points - 1)
    y_step = _nice_step(span)
    _draw_y_grid(fb, chart_left, chart_top, chart_width, chart_height, min_val, max_val, y_step)
    _draw_x_grid(fb, chart_left, chart_top, chart_height, step_x, points, span_label)

    prev_x = chart_left
    prev_y = chart_top + chart_height - _scale(history.data[0], min_val, span, chart_height)

    for idx, value in enumerate(history.data[1:], start=1):
        x = int(chart_left + idx * step_x)
        y = chart_top + chart_height - _scale(value, min_val, span, chart_height)
        fb.line(prev_x, prev_y, x, y, FG_COLOR)
        prev_x, prev_y = x, y

    fb.text(f"Min {min_val:.0f} EUR", chart_left, chart_top + chart_height + 4, FG_COLOR)
    fb.text(f"Max {max_val:.0f} EUR", chart_left + 110, chart_top + chart_height + 4, FG_COLOR)
    fb.text(span_label, chart_left + chart_width - 36, chart_top - 9, FG_COLOR)


def _scale(value, min_val, span, height):
    return int(((value - min_val) / span) * (height - 1))


def _nice_step(span):
    # Wähle sinnvolles Raster (1,2,5 x 10^n) für 3-5 Linien
    rough = span / 4
    magnitude = 10 ** math.floor(math.log10(rough)) if rough > 0 else 1
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if span / step <= 6:
            return step
    return magnitude


def _resample(values, target_len):
    """Skaliert die Liste linear auf target_len (füllt volle X-Achse)."""
    n = len(values)
    if n == 0:
        return []
    if n == target_len:
        return list(values)
    if target_len == 1:
        return [values[-1]]
    result = []
    for i in range(target_len):
        pos = i * (n - 1) / (target_len - 1)
        low = int(pos)
        high = min(low + 1, n - 1)
        frac = pos - low
        val = values[low] * (1 - frac) + values[high] * frac
        result.append(val)
    return result


def _format_k(value):
    if abs(value) >= 1000:
        return f"{value/1000:.1f}k"
    return f"{value:.0f}"


def _draw_y_grid(fb, left, top, width, height, min_val, max_val, step):
    start = math.ceil(min_val / step) * step
    y = start
    label_x = 4  # fester linker Rand für Y-Beschriftung, unabhängig vom Chart-Offset
    while y <= max_val:
        offset = _scale(y, min_val, max(max_val - min_val, 1e-6), height)
        _hline_dashed(fb, left, top + height - offset, width, FG_COLOR)
        label = _format_k(y)
        fb.text(label, label_x, top + height - offset - 4, FG_COLOR)
        y += step


def _draw_x_grid(fb, left, top, height, step_x, points, span_label):
    # Zeichne vertikale Gitterlinien:
    # - 365d: 12 Linien (~1 pro Monat)
    # - sonst: ~6 Linien
    if points < 2:
        return
    desired_lines = 12 if span_label == "365d" else 6
    step_points = max(1, points // desired_lines)
    for idx in range(step_points, points, step_points):
        x = int(left + idx * step_x)
        _vline_dashed(fb, x, top, height, FG_COLOR)


def _hline_dashed(fb, x, y, length, color, dash=3, gap=2):
    """Zeichnet gestrichelte horizontale Linie."""
    pos = x
    end = x + length
    while pos < end:
        run = min(dash, end - pos)
        fb.hline(pos, y, run, color)
        pos += dash + gap


def _vline_dashed(fb, x, y, length, color, dash=3, gap=2):
    """Zeichnet gestrichelte vertikale Linie."""
    pos = y
    end = y + length
    while pos < end:
        run = min(dash, end - pos)
        fb.vline(x, pos, run, color)
        pos += dash + gap


def draw_text_scaled(fb, text, x, y, scale=2, color=FG_COLOR, bg=None):
    """Rendert den 8x8-Standardfont skaliert."""
    tmp_w = 8 * len(text)
    tmp_h = 8
    buf = bytearray(tmp_w * tmp_h // 8)
    tmp = framebuf.FrameBuffer(buf, tmp_w, tmp_h, framebuf.MONO_HLSB)
    if bg is not None:
        tmp.fill(bg)
    tmp.text(text, 0, 0, color)
    for ty in range(tmp_h):
        for tx in range(tmp_w):
            px = tmp.pixel(tx, ty)
            if px:
                fb.fill_rect(x + tx * scale, y + ty * scale, scale, scale, color)
            elif bg is not None and bg != color:
                fb.fill_rect(x + tx * scale, y + ty * scale, scale, scale, bg)


def draw_header(fb, price):
    fb.fill_rect(0, 0, EPD_WIDTH, 23, FG_COLOR)
    fb.text("BTC/EUR", 6, 8, BG_COLOR)
    # Bewusst weißer Hintergrund, damit der Preis unabhängig von Display-Inversion sichtbar ist
    draw_text_scaled(fb, f"{price:,.0f} EUR", 120, 5, scale=2, color=FG_COLOR, bg=BG_COLOR)


def draw_footer(fb, last_update):
    if last_update:
        yyyy, mo, dd, hh, mm, ss = (
            last_update[0], last_update[1], last_update[2],
            last_update[3], last_update[4], last_update[5],
        )
        fb.text(f"Stand: {yyyy:04}-{mo:02}-{dd:02} {hh:02}:{mm:02}:{ss:02}", 6, EPD_HEIGHT - 14, FG_COLOR)

def show_message(display, lines):
    display.fb.fill(BG_COLOR)
    y = 10
    for line in lines:
        display.fb.text(line, 6, y, FG_COLOR)
        y += 12
    display.flush()


def main():
    _set_default_socket_timeout(HISTORY_FETCH_TIMEOUT)
    wlan = connect_wifi()
    sync_time()
    display = create_display()
    history_24h = RingBuffer(HISTORY_LENGTH_24H)
    history_365d = RingBuffer(HISTORY_LENGTH_365D)
    led = Pin("LED", Pin.OUT)
    last_update = None
    show_message(display, ["Lade Historie 24h...", "Bitte warten"])
    try:
        history_24h.extend(fetch_history(COINGECKO_HISTORY_24H_URL, HISTORY_LENGTH_24H, "24h"))
    except Exception as exc:
        # Falls der historische Abruf fehlschlägt, weiter mit Live-Daten
        show_message(display, ["Historie fehlgeschlagen", repr(exc)[:26]])
        log(f"Historie fehlgeschlagen: {exc}")
        log("Direkt mit Live-Daten starten.")
    else:
        show_message(display, ["24h Historie geladen", f"Punkte: {len(history_24h.data)}"])

    # 365d Laden
    try:
        history_365d.extend(
            fetch_history(COINGECKO_HISTORY_365D_URL, HISTORY_LENGTH_365D, "365d")
        )
        show_message(display, ["365d Historie geladen", f"Punkte: {len(history_365d.data)}"])
    except Exception as exc:
        show_message(display, ["365d fehlgeschlagen", repr(exc)[:26]])
        log(f"365d Historie fehlgeschlagen: {exc}")

    # Startpreis holen, damit die Anzeige gefüllt ist
    current_price = fetch_price_eur()
    now_ms = time.ticks_ms()
    last_price_ms = now_ms
    last_update = _now_local_berlin()
    history_24h.append(current_price)
    history_365d.append(current_price)

    is_long_view = False  # False=24h, True=365d
    last_swap = time.ticks_ms()

    refresh_display = True  # Initial einmal rendern

    while True:
        led.on()
        try:
            now_ms = time.ticks_ms()
            swap_due = time.ticks_diff(now_ms, last_swap) >= SCREEN_SWAP_SECONDS * 1000
            price_due = time.ticks_diff(now_ms, last_price_ms) >= PRICE_REFRESH_SECONDS * 1000

            if price_due or swap_due:
                current_price = fetch_price_eur()
                last_price_ms = time.ticks_ms()
                last_update = _now_local_berlin()
                history_24h.append(current_price)
                history_365d.append(current_price)
                log(
                    f"Preis aktualisiert: {current_price:.2f} EUR "
                    f"(24h: {len(history_24h.data)}, 365d: {len(history_365d.data)})"
                )
                refresh_display = True

            if swap_due:
                is_long_view = not is_long_view
                last_swap = now_ms
                log(f"Wechsel auf {'365d' if is_long_view else '24h'}-Ansicht")
                refresh_display = True

            if refresh_display:
                active_history = history_365d if is_long_view else history_24h
                span_label = "365d" if is_long_view else "24h"

                display.fb.fill(BG_COLOR)
                draw_header(display.fb, current_price)
                draw_chart(display.fb, active_history, span_label)
                draw_footer(display.fb, last_update)
                display.flush()
                refresh_display = False
        except Exception as exc:
            display.fb.fill(BG_COLOR)
            display.fb.text("Fehler:", 6, 6, FG_COLOR)
            display.fb.text(repr(exc)[:EPD_WIDTH // 6], 6, 22, FG_COLOR)
            display.flush()
        finally:
            led.off()
        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
