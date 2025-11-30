"""
E-Paper Display-Helfer für das 2,9\" Pico Cap Touch Board.

Setzt voraus, dass der herstellerspezifische EPD-Treiber (z.B. waveshare 2.9\" touch)
im Modulpfad vorhanden ist und eine EPD-Klasse bereitstellt.
Falls die Methodennamen abweichen, passe sie in EInkDisplay.flush() an.
"""
import framebuf

# Auflösung des 2,9\"-Panels (296 x 128 Pixel).
EPD_WIDTH = 296
EPD_HEIGHT = 128


class EInkDisplay:
    def __init__(self, epd):
        self.epd = epd
        # 1 Bit pro Pixel (Schwarz/Weiß)
        self.buffer = bytearray(EPD_WIDTH * EPD_HEIGHT // 8)
        # MONO_VLSB passt zur Waveshare-Landscape-Implementierung
        self.fb = framebuf.FrameBuffer(self.buffer, EPD_WIDTH, EPD_HEIGHT, framebuf.MONO_VLSB)

    def clear(self):
        self.fb.fill(1)  # 1 = Weiß auf vielen E-Paper-Treibern
        self.flush()

    def flush(self):
        """Zeigt den aktuellen Buffer an, unterstützt mehrere Treibernamen."""
        if hasattr(self.epd, "display_frame"):
            self.epd.display_frame(self.buffer)
        elif hasattr(self.epd, "display"):
            self.epd.display(self.buffer)
        else:
            raise RuntimeError("EPD-Treiber nicht erkannt: erwarte display_frame() oder display()")

    def sleep(self):
        if hasattr(self.epd, "sleep"):
            self.epd.sleep()


def create_display():
    """
    Initialisiert EPD-Instanz.
    Der Waveshare-Treiber nutzt SPI1 mit den Standardpins des Pico (SCK=10, MOSI=11, MISO=12)
    sowie DC=8, CS=9, RST=12, BUSY=13. Falls dein Board andere Pins hat, passe epd2in9.py an.
    """
    epd = EPD()
    display = EInkDisplay(epd)
    display.clear()
    return display


# Import nach unten, damit create_display() funktioniert, wenn das Treibermodul vorhanden ist.
try:
    # Waveshare-Variante für Landscape (296 x 128)
    from epd2in9 import EPD_2in9_Landscape as EPD  # type: ignore
except ImportError as exc:
    raise ImportError(
        "EPD-Treiber (z.B. epd2in9.py aus dem Hersteller-Repo) fehlt im Modulpfad."
    ) from exc
