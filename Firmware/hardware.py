"""Pin map for the frozen Jammer/shield schematics.
PCF8574 uses raw one-byte I2C transfers. Unsupported hardware stays inactive.
"""
from machine import Pin, I2C, SPI, PWM
from time import sleep_ms, ticks_ms, ticks_diff
import neopixel

class Expander:
    def __init__(self, bus, address=0x20):
        self.bus, self.address = bus, address
        self.bus.writeto(address, b"\xff")
    def read(self):
        return self.bus.readfrom(self.address, 1)[0]

class DisabledSelect:
    """GPIO21 belongs to the IR receiver; never drive it as nRF2 CS."""
    def __call__(self, value=None):
        if value == 0:
            raise OSError("nRF2 CS shares the IR receiver output in this schematic")
        return 1

class SharedSPI:
    def __init__(self, number, sck, mosi, miso, selects):
        self.selects = [DisabledSelect() if n is None else Pin(n, Pin.OUT, value=1)
                        for n in selects]
        self.spi = SPI(number, baudrate=1000000, polarity=0, phase=0,
                       sck=Pin(sck), mosi=Pin(mosi), miso=Pin(miso))
    def begin(self, cs, speed):
        for pin in self.selects: pin(1)
        self.spi.init(baudrate=speed, polarity=0, phase=0)
        cs(0)
    def end(self, cs): cs(1)

class Board:
    RADIOS_AVAILABLE = False
    RADIO_REASON = "Bare radio support circuits and switched RF matching are absent"
    BUTTON_BITS = (3, 4, 5, 6, 7, 2)  # Logical Bt_1 through Bt_6.
    def __init__(self):
        self.i2c = I2C(0, scl=Pin(9), sda=Pin(8), freq=100000)
        self.io_errors = []
        try: self.buttons = Expander(self.i2c)
        except OSError as e:
            self.buttons = None
            self.io_errors.append("PCF8574: " + str(e))
        self.radio_bus = SharedSPI(1, 12, 11, 13, (4, None, 48, 5, 10))
        self.display_bus = SharedSPI(2, 36, 35, 37, (16, 18))
        self.ce1, self.ce3 = Pin(15, Pin.OUT, value=0), Pin(47, Pin.OUT, value=0)
        self.backlight = Pin(7, Pin.OUT, value=0)
        # The card-detect switch is not connected to the MCU in this schematic.
        self.card_detect = lambda: 0
        # GPIO2 is both the battery-divider node and the buzzer drive.
        # Hold the buzzer off; do not report a fabricated battery voltage.
        self.buzzer_pin = Pin(2, Pin.OUT, value=0)
        self.rgb = neopixel.NeoPixel(Pin(1), 4)
        self.rgb.fill((0,0,0)); self.rgb.write()
        self.last_buttons = self.candidate = 0
        self.changed = ticks_ms()
    def set_band(self, band):
        if band not in (0,1,2,3): raise ValueError("RF selection must be 0..3")
        raise OSError(self.RADIO_REASON)
    def ce(self, index, value):
        if index == 0: self.ce1(value)
        elif index == 2: self.ce3(value)
        else: raise OSError("nRF2 CE shares GPIO14 with IR transmit")
    def usb500(self, granted=False):
        raise OSError("Programmable USB current-limit control is not connected")
    def battery(self, factor=1.0): return None
    def keys(self):
        if self.buttons is None: return []
        raw = self.buttons.read()
        state = sum((not (raw & (1 << bit))) << n for n,bit in enumerate(self.BUTTON_BITS))
        now = ticks_ms()
        if state != self.candidate: self.candidate,self.changed = state,now
        if ticks_diff(now,self.changed) >= 30 and state != self.last_buttons:
            pressed=state & ~self.last_buttons; self.last_buttons=state
            return [n+1 for n in range(6) if pressed & (1 << n)]
        return []
    def beep(self, milliseconds=40):
        tone = PWM(self.buzzer_pin, freq=2730, duty_u16=12000)
        try: sleep_ms(min(200,max(1,milliseconds)))
        finally:
            tone.deinit()
            self.buzzer_pin.init(Pin.OUT,value=0)
    def safe(self):
        self.buzzer_pin.init(Pin.OUT,value=0)
        self.backlight(0); self.ce1(0); self.ce3(0)
        for pin in self.radio_bus.selects + self.display_bus.selects: pin(1)
        self.rgb.fill((0,0,0)); self.rgb.write()
