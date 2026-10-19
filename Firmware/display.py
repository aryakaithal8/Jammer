"""ILI9341 portrait display and XPT2046 touch; small text buffers, no PSRAM.

Touch calibration is supplied by the user on the real fitted module.
"""
from machine import Pin
from time import sleep_ms
import framebuf


class Display:
    WIDTH, HEIGHT = 240, 320

    def __init__(self, board):
        self.board, self.bus = board, board.display_bus
        self.cs, self.touch_cs = self.bus.selects
        self.dc = Pin(17, Pin.OUT)
        # J5 RESET is the MCU EN line: use the ILI9341 software reset only.
        sleep_ms(120)
        for command, data in ((0x01, b''), (0x28, b''), (0xCF, b'\x00\xC1\x30'),
                              (0xED, b'\x64\x03\x12\x81'), (0xE8, b'\x85\x00\x78'),
                              (0xCB, b'\x39\x2C\x00\x34\x02'), (0xF7, b'\x20'),
                              (0xEA, b'\x00\x00'), (0xC0, b'\x23'), (0xC1, b'\x10'),
                              (0xC5, b'\x3E\x28'), (0xC7, b'\x86'), (0x36, b'\x48'),
                              (0x3A, b'\x55'), (0xB1, b'\x00\x18'), (0xB6, b'\x08\x82\x27')):
            self.command(command, data)
            if command == 1: sleep_ms(120)
        self.command(0x11); sleep_ms(120); self.command(0x29)
        self.clear(); board.backlight(1)

    def command(self, cmd, data=b''):
        self.bus.begin(self.cs, 20000000)
        try:
            self.dc(0); self.bus.spi.write(bytes((cmd,)))
            if data:
                self.dc(1); self.bus.spi.write(data)
        finally:
            self.bus.end(self.cs)

    def window(self, x, y, w, h):
        self.command(0x2A, bytes((x >> 8, x & 255, (x+w-1) >> 8, (x+w-1) & 255)))
        self.command(0x2B, bytes((y >> 8, y & 255, (y+h-1) >> 8, (y+h-1) & 255)))
        self.command(0x2C)

    def pixels(self, data):
        self.bus.begin(self.cs, 20000000)
        try:
            self.dc(1); self.bus.spi.write(data)
        finally:
            self.bus.end(self.cs)

    def fill(self, x, y, w, h, color=0):
        self.window(x, y, w, h)
        row = bytes((color >> 8, color & 255)) * w
        for _ in range(h): self.pixels(row)

    def clear(self):
        self.fill(0, 0, 240, 320)

    def text(self, value, x, y, color=0xFFFF, bg=0):
        value = str(value)[:max(0, (240-x)//8)]
        if not value: return
        data = bytearray(len(value) * 8 * 10 * 2)
        fb = framebuf.FrameBuffer(data, len(value)*8, 10, framebuf.RGB565)
        fb.fill(bg); fb.text(value, 0, 1, color)
        # framebuf is native little-endian; ILI9341 requires big-endian RGB565.
        for i in range(0, len(data), 2): data[i], data[i+1] = data[i+1], data[i]
        self.window(x, y, len(value)*8, 10); self.pixels(data)

    def touch_raw(self):
        self.bus.begin(self.touch_cs, 1000000)
        try:
            tx, rx = bytes((0xB0, 0, 0)), bytearray(3)
            self.bus.spi.write_readinto(tx, rx)
            pressure = ((rx[1] << 8) | rx[2]) >> 3
            if pressure < 100: return None
            samples = []
            for _ in range(5):
                vals = []
                for cmd in (0xD0, 0x90):
                    tx, rx = bytes((cmd, 0, 0)), bytearray(3)
                    self.bus.spi.write_readinto(tx, rx)
                    vals.append(((rx[1] << 8) | rx[2]) >> 3)
                samples.append(vals)
            return sorted(v[0] for v in samples)[2], sorted(v[1] for v in samples)[2]
        finally:
            self.bus.end(self.touch_cs)

    def touch(self, calibration):
        raw = self.touch_raw()
        if raw is None or not calibration: return None
        # Affine matrix: [a,b,c,d,e,f], supports axis swap and reversal.
        a,b,c,d,e,f = calibration
        x,y = raw
        return max(0, min(239, int(a*x+b*y+c))), max(0, min(319, int(d*x+e*y+f)))
