"""TSOP1838 capture and bounded 38kHz IR frame transmission.

Demodulated edges are recorded into a preallocated buffer in the IRQ handler.
Captures require a physical remote; timing quality needs oscilloscope testing.
"""
from machine import Pin
from time import ticks_us, ticks_ms, ticks_diff, sleep_ms
from array import array
import micropython, esp32
micropython.alloc_emergency_exception_buf(100)


class Infrared:
    def __init__(self):
        self.rx=Pin(21,Pin.IN,Pin.PULL_UP)
        self.tx=Pin(14,Pin.OUT,value=0)
        self.times=array('I',[0]*600)
        self.count=0;self.last=0;self.overflow=False;self.recording=False

    def edge(self,pin):
        now=ticks_us()
        if not self.recording:return
        if self.count==0:
            if pin()==0:self.last=now;self.count=1
            return
        dt=ticks_diff(now,self.last);self.last=now
        if dt>30000:
            self.recording=False;return
        if self.count<len(self.times):
            self.times[self.count-1]=dt;self.count+=1
        else:
            self.overflow=True;self.recording=False

    def learn(self,timeout_ms=10000):
        self.count=0;self.overflow=False;self.recording=True
        self.rx.irq(handler=self.edge,trigger=Pin.IRQ_RISING|Pin.IRQ_FALLING,hard=True)
        start=ticks_ms()
        try:
            while ticks_diff(ticks_ms(),start)<timeout_ms:
                if self.count>1 and ticks_diff(ticks_us(),self.last)>30000:break
                if not self.recording:break
                sleep_ms(2)
        finally:
            self.rx.irq(handler=None);self.recording=False
        if self.overflow:raise ValueError('IR frame exceeded 600 edges')
        pulses=list(self.times[:max(0,self.count-1)])
        if len(pulses)<4:raise OSError('No complete IR frame captured')
        # TSOP idle is high; the first captured low interval is a carrier mark.
        return {'carrier_hz':38000,'start_mark':True,'pulses_us':pulses}

    def send(self,frame):
        pulses=frame['pulses_us']
        carrier=int(frame.get('carrier_hz',38000))
        if not 30000<=carrier<=60000:raise ValueError('IR carrier out of range')
        if not 4<=len(pulses)<=600 or any(not 1<=p<=30000 for p in pulses):
            raise ValueError('Invalid IR pulse train')
        if sum(pulses)>1000000:raise ValueError('IR frame duration exceeds 1 second')
        r=esp32.RMT(pin=self.tx,resolution_hz=1000000,tx_carrier=(carrier,33,1),idle_level=False)
        try:
            r.write_pulses(pulses,bool(frame.get('start_mark',True)))
            if not r.wait_done(timeout=1200):raise OSError('IR transmit timed out')
        finally:
            r.deinit();self.tx.init(Pin.OUT,value=0)
