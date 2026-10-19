"""Packet transceivers for three nRF24L01+ and three band-matched CC1101 ICs.

CC1101 uses 38.4kbit/s GFSK, variable packet length and hardware CRC.
The RF switch and decoder select one matched CC1101 path at a time.
Transmit functions send one bounded packet; there is no continuous carrier.
"""
from machine import Pin
from time import sleep_us, sleep_ms, ticks_ms, ticks_diff


def cc_frequency_word(mhz):
    return int(mhz * 65536 / 26 + 0.5)


class NRF24:
    def __init__(self, board, index, channel=76, address=b'ESP32'):
        if not 0 <= channel <= 125 or len(address) != 5:
            raise ValueError('nRF channel/address invalid')
        self.board, self.index, self.bus = board, index, board.radio_bus
        # Distinct pipe addresses avoid simultaneous auto-ACKs from the three
        # radios fitted to this board. Paired nodes must match their own pipe.
        address=address[:4]+bytes(((address[4]+index)&255,))
        self.cs = self.bus.selects[index]
        board.ce(index, 0)
        self.reg(0x00, 0x0C)  # CRC16, powered down during configuration.
        self.reg(0x01, 1); self.reg(0x02, 1); self.reg(0x03, 3)
        self.reg(0x04, 0x23)  # 750us delay, three retransmissions.
        self.reg(0x05, channel); self.reg(0x06, 0x00)  # 1Mbit/s, -18dBm.
        self.reg(0x0A, address); self.reg(0x10, address); self.reg(0x11, 32)
        self.reg(0x1C, 0); self.reg(0x1D, 0); self.reg(0x07, 0x70)
        if self.read(0x05)[0] != channel: raise OSError('nRF SPI readback failed: '+str(index+1))
        self.command(0xE1); self.command(0xE2)
        self.receive_mode()

    def command(self, cmd, data=b'', read=0):
        self.bus.begin(self.cs, 4000000)
        try:
            status = bytearray(1)
            self.bus.spi.write_readinto(bytes((cmd,)), status)
            if data: self.bus.spi.write(data)
            result = self.bus.spi.read(read, 255) if read else b''
            return status[0], result
        finally:
            self.bus.end(self.cs)

    def reg(self, reg, value):
        data = bytes((value,)) if isinstance(value, int) else value
        self.command(0x20 | reg, data)

    def read(self, reg, count=1):
        return self.command(reg & 31, read=count)[1]

    def receive_mode(self):
        self.board.ce(self.index, 0); self.reg(0, 0x0F)
        sleep_ms(2); self.board.ce(self.index, 1)

    def recv(self):
        if self.read(0x17)[0] & 1: return None
        payload = self.command(0x61, read=32)[1]
        self.reg(7, 0x40)
        size = payload[0]
        return payload[1:1+size] if size <= 31 else None

    def send(self, payload):
        if not 1 <= len(payload) <= 31: raise ValueError('nRF message length must be 1..31 bytes')
        self.board.ce(self.index, 0)
        try:
            self.reg(0, 0x0E); self.reg(7, 0x70); self.command(0xE1)
            self.command(0xA0, bytes((len(payload),))+payload+b'\x00'*(31-len(payload)))
            self.board.ce(self.index, 1); sleep_us(20); self.board.ce(self.index, 0)
            start=ticks_ms()
            while ticks_diff(ticks_ms(), start) < 50:
                status=self.command(0xFF)[0]
                if status & 0x20: return True
                if status & 0x10: return False
                sleep_ms(1)
            raise OSError('nRF packet transmission timeout')
        finally:
            self.command(0xE1); self.reg(7, 0x70); self.receive_mode()

    def channel(self, value):
        if not 0 <= value <= 125: raise ValueError('nRF channel must be 0..125')
        self.board.ce(self.index, 0); self.reg(5, value); self.board.ce(self.index, 1)

    def standby(self):
        self.board.ce(self.index, 0); self.reg(0, 0x0C)


class CC1101:
    FREQUENCIES=(315.0, 433.92, 868.3, 915.0)
    # Register fields derived from the CC1101 datasheet equations, 26MHz XTAL.
    # MDMCFG4/3 = 38,383.5bit/s; ~101.6kHz RX BW, deviation ~20.6kHz.
    PROFILE={0x00:0x0E,0x02:0x06,0x03:0x47,0x04:0xD3,0x05:0x91,
             0x06:60,0x07:0x04,0x08:0x05,0x09:0,0x0A:0,0x0B:0x06,
             0x0C:0,0x10:0xCA,0x11:0x83,0x12:0x13,0x13:0x22,0x14:0xF8,
             0x15:0x35,0x16:0x07,0x17:0x0C,0x18:0x18,0x19:0x16,
             0x1A:0x6C,0x1B:0x43,0x1C:0x40,0x1D:0x91,0x20:0xFB,
             0x21:0x56,0x22:0x10,0x23:0xE9,0x24:0x2A,0x25:0x00,
             0x26:0x1F,0x2C:0x81,0x2D:0x35,0x2E:0x09}

    def __init__(self, board, band=1):
        self.board, self.bus = board, board.radio_bus
        self.cs=self.bus.selects[3]; self.miso=Pin(13, Pin.IN)
        self.active=None
        self.select(band)

    @staticmethod
    def path(band):
        if band not in (0,1,2,3): raise ValueError('band: 0=315, 1=433, 2=868, 3=915MHz')
        return (0,1,2,2)[band]

    def transfer(self, command, data=b'', read=0):
        self.bus.begin(self.cs, 4000000)
        try:
            start=ticks_ms()
            while self.miso():
                if ticks_diff(ticks_ms(), start) > 10: raise OSError('CC1101 SPI ready timeout')
            status=bytearray(1); self.bus.spi.write_readinto(bytes((command,)),status)
            if data:self.bus.spi.write(data)
            return self.bus.spi.read(read,255) if read else status[0]
        finally:
            self.bus.end(self.cs)

    def write(self, reg, value):
        self.transfer(reg, bytes((value,)))

    def read(self, reg, status=False):
        return self.transfer(reg | (0xC0 if status else 0x80),read=1)[0]

    def select(self, band):
        path=self.path(band)
        if self.active is not None:
            self.transfer(0x36); self.transfer(0x39)  # Old path goes to power-down.
        self.board.set_band(path); self.active=band
        self.cs(1); sleep_us(40); self.cs(0); sleep_us(40); self.cs(1); sleep_us(40)
        self.transfer(0x30); sleep_ms(2)
        version=self.read(0x31,True)
        if version in (0,255): raise OSError('CC1101 version/readback invalid')
        for reg,val in self.PROFILE.items():self.write(reg,val)
        f=cc_frequency_word(self.FREQUENCIES[band])
        self.write(0x0D,f>>16);self.write(0x0E,(f>>8)&255);self.write(0x0F,f&255)
        self.transfer(0x3E,bytes((0x12 if band<2 else 0x03,))) # Nominal -30dBm.
        self.transfer(0x33);sleep_ms(3)
        self.transfer(0x3A);self.transfer(0x34)

    def recover_rx(self):
        self.transfer(0x36);self.transfer(0x3A);self.transfer(0x34)

    def recv(self):
        count=self.read(0x3B,True)
        if count&128:self.recover_rx();return None
        # IOCFG0 asserts until the whole packet is received. FIFO bytes alone
        # can include a partial packet, which must not be read prematurely.
        if Pin(6,Pin.IN)() or count<3:return None
        count2=self.read(0x3B,True)
        if count2 != count:return None
        data=self.transfer(0xFF,read=count)
        size=data[0]
        if size>60 or len(data)!=size+3 or not data[-1]&128:
            self.recover_rx();return None
        raw=data[-2];rssi=(raw-256 if raw>=128 else raw)/2-74
        return {'payload':data[1:1+size],'rssi':rssi,'lqi':data[-1]&127,'band':self.active}

    def send(self, payload):
        if not 1<=len(payload)<=60:raise ValueError('CC message length must be 1..60 bytes')
        self.transfer(0x36);self.transfer(0x3B)
        try:
            self.transfer(0x7F,bytes((len(payload),))+payload)
            self.transfer(0x35)
            start=ticks_ms();sent=False
            while ticks_diff(ticks_ms(),start)<150:
                state=self.read(0x35,True)&31
                if state==19:raise OSError('CC TX FIFO underflow')
                if self.read(0x3A,True)&127==0 and state not in (13,14,15,16,17,18):
                    sent=True;break
                sleep_ms(1)
            if not sent:raise OSError('CC packet TX timeout or channel busy')
            return True
        finally:
            self.recover_rx()

    def rssi(self):
        r=self.read(0x34,True)
        return (r-256 if r>=128 else r)/2-74

    def standby(self):
        self.transfer(0x36);self.transfer(0x39)
