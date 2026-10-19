"""SD SPI block device. Every transaction restores mode/speed on shared bus.

Supports SDSC and SDHC/SDXC FAT cards, 512-byte single-block transfers.
Does not format cards. VfsFat requires a FAT-formatted card.
"""
from time import sleep_ms, ticks_ms, ticks_diff


class SDCard:
    def __init__(self, bus):
        self.bus, self.cs, self.speed = bus, bus.selects[4], 100000
        self.buf = bytearray(6)
        self.byte = bytearray(1)
        self.block = bytearray(512)
        bus.spi.init(baudrate=self.speed, polarity=0, phase=0)
        self.cs(1); bus.spi.write(b'\xff' * 10)
        for _ in range(10):
            if self.cmd(0, 0, 0x95)[0] == 1: break
            sleep_ms(10)
        else: raise OSError('SD did not enter idle state')
        r, tail = self.cmd(8, 0x1AA, 0x87, 4)
        v2 = r == 1
        if v2 and tail != b'\x00\x00\x01\xaa': raise OSError('SD voltage/check-pattern mismatch')
        end = ticks_ms()
        while ticks_diff(ticks_ms(), end) < 2000:
            self.cmd(55, 0)
            if self.cmd(41, 0x40000000 if v2 else 0)[0] == 0: break
            sleep_ms(10)
        else: raise OSError('SD initialization timed out')
        r, ocr = self.cmd(58, 0, extra=4)
        if r: raise OSError('SD OCR read failed')
        self.scale = 1 if v2 and ocr[0] & 0x40 else 512
        if self.cmd(16, 512)[0]: raise OSError('SD block length failed')
        csd = bytearray(16)
        self.read_data(9, 0, csd)
        if csd[0] >> 6 == 1:
            self.sectors = (((csd[7] & 63) << 16) | (csd[8] << 8) | csd[9]) + 1
            self.sectors *= 1024
        else:
            size = ((csd[6] & 3) << 10) | (csd[7] << 2) | (csd[8] >> 6)
            mult = ((csd[9] & 3) << 1) | (csd[10] >> 7)
            self.sectors = ((size+1) << (mult+2)) * (1 << (csd[5] & 15)) // 512
        self.speed = 8000000

    def finish(self):
        self.cs(1); self.bus.spi.write(b'\xff')

    def cmd(self, command, arg, crc=1, extra=0, keep=False):
        self.bus.begin(self.cs, self.speed)
        try:
            self.bus.spi.write(b'\xff')
            self.buf[0] = 0x40 | command
            self.buf[1] = arg >> 24 & 255; self.buf[2] = arg >> 16 & 255
            self.buf[3] = arg >> 8 & 255; self.buf[4] = arg & 255; self.buf[5] = crc
            self.bus.spi.write(self.buf)
            for _ in range(100):
                self.bus.spi.readinto(self.byte, 255)
                if not self.byte[0] & 128:
                    result = self.byte[0], self.bus.spi.read(extra, 255) if extra else b''
                    if not keep: self.finish()
                    return result
            raise OSError('SD command response timeout')
        except Exception:
            self.finish(); raise

    def wait(self, value, timeout=500):
        start = ticks_ms()
        while ticks_diff(ticks_ms(), start) < timeout:
            self.bus.spi.readinto(self.byte, 255)
            if self.byte[0] == value: return
        raise OSError('SD data/busy timeout')

    def read_data(self, command, arg, buf):
        try:
            if self.cmd(command, arg, keep=True)[0]: raise OSError('SD read command rejected')
            self.wait(0xFE)
            self.bus.spi.readinto(buf, 255); self.bus.spi.read(2, 255)
        finally:
            self.finish()

    def readblocks(self, block, buf):
        mv = memoryview(buf)
        for i in range(len(buf)//512): self.read_data(17, (block+i)*self.scale, mv[i*512:(i+1)*512])
        return 0

    def writeblocks(self, block, buf):
        mv = memoryview(buf)
        for i in range(len(buf)//512):
            try:
                if self.cmd(24, (block+i)*self.scale, keep=True)[0]: raise OSError('SD write rejected')
                self.bus.spi.write(b'\xff\xfe'); self.bus.spi.write(mv[i*512:(i+1)*512])
                self.bus.spi.write(b'\xff\xff'); self.bus.spi.readinto(self.byte, 255)
                if self.byte[0] & 31 != 5: raise OSError('SD data write not accepted')
                self.wait(255, 1000)
            finally:
                self.finish()
        return 0

    def ioctl(self, op, arg):
        if op == 4: return self.sectors
        if op == 5: return 512
        if op in (1, 2, 3): return 0
