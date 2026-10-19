"""ESP32DIV2 handheld application: menu, USB JSON console and event logging.

Firmware source is supplied with host-side checks. Actual hardware behaviour,
RF emissions, touch mapping and timing must be tested on the prototype.
"""
import sys, os, json, gc, select, network, bluetooth, vfs
from binascii import hexlify
from time import sleep_ms, ticks_ms, ticks_diff
from hardware import Board
from display import Display
from radios import NRF24, CC1101
from infrared import Infrared
from sdcard import SDCard

DEFAULTS={'battery_calibration':1.0,'touch_calibration':None,'nrf_channel':76,
          'nrf_address':'ESP32','cc_band':1,'wifi_ssid':'','wifi_password':'',
          'packet_message':'ESP32DIV2','logging':False}


def affine(raw, screen):
    """Three-point affine touch calibration; reject coincident calibration taps."""
    (x1,y1),(x2,y2),(x3,y3)=raw
    det=x1*(y2-y3)+x2*(y3-y1)+x3*(y1-y2)
    if abs(det)<1000:raise ValueError('Calibration taps too close; retry')
    def solve(v):
        a=(v[0]*(y2-y3)+v[1]*(y3-y1)+v[2]*(y1-y2))/det
        b=(v[0]*(x3-x2)+v[1]*(x1-x3)+v[2]*(x2-x1))/det
        c=(v[0]*(x2*y3-x3*y2)+v[1]*(x3*y1-x1*y3)+v[2]*(x1*y2-x2*y1))/det
        return [a,b,c]
    return solve([p[0] for p in screen])+solve([p[1] for p in screen])


class Application:
    MENU=('Device status','Wi-Fi discovery','BLE discovery','2.4GHz packets',
          'Sub-GHz packets','Change Sub-GHz band','Learn IR remote','Send saved IR',
          'SD card / logging','Calibrate touch','Lights / sound','Wi-Fi connection')

    def __init__(self):
        self.config=dict(DEFAULTS)
        try:
            with open('config.json') as f:self.config.update(json.load(f))
        except OSError:pass
        self.board=Board();self.display=None;self.nrf=[None]*3;self.cc=None
        self.ir=Infrared();self.last_ir=None;self.events=[];self.sd=None;self.mounted=False
        self.sd_error='';self.errors=[];self.selected=0;self.view='menu';self.lines=[]
        self.redraw=True;self.last_touch=0;self.last_poll=0;self.rx_events=0
        self.last_send=ticks_ms()-1000
        self.console_buffer='';self.poll=select.poll();self.poll.register(sys.stdin,select.POLLIN)
        try:self.display=Display(self.board)
        except Exception as e:self.errors.append('Display: '+str(e))
        self.errors.extend(self.board.io_errors)
        if self.board.RADIOS_AVAILABLE:
            for n in range(3):
                try:self.nrf[n]=NRF24(self.board,n,self.config['nrf_channel'],self.config['nrf_address'].encode())
                except Exception as e:self.errors.append('nRF'+str(n+1)+': '+str(e))
            try:self.cc=CC1101(self.board,self.config['cc_band'])
            except Exception as e:self.errors.append('CC: '+str(e))
        else:self.errors.append(self.board.RADIO_REASON)
        self.mount_sd()
        self.wifi=network.WLAN(network.STA_IF)
        self.ble=bluetooth.BLE();self.ble.active(True);self.ble.irq(self.ble_irq)
        self.ble_results=[];self.ble_pending=[];self.scanning=False;self.ble_draw_state=None
        self.event('boot',{'errors':self.errors,'sd':self.mounted})
        print('Jammer V1 ready. One JSON command per line; {"cmd":"help"}. Ctrl-C returns to REPL.')

    def save_config(self):
        with open('config.new','w') as f:json.dump(self.config,f)
        os.rename('config.new','config.json')

    def event(self,kind,data):
        item={'ms':ticks_ms(),'type':kind,'data':data}
        self.events.append(item)
        if len(self.events)>40:self.events.pop(0)
        if self.mounted and self.config['logging']:
            try:
                with open('/sd/esp32div2-events.jsonl','a') as f:f.write(json.dumps(item)+'\n')
            except OSError as e:self.sd_error=str(e);self.config['logging']=False

    def mount_sd(self):
        if self.board.card_detect():return False
        try:
            self.sd=SDCard(self.board.radio_bus)
            vfs.mount(vfs.VfsFat(self.sd),'/sd');self.mounted=True;self.sd_error=''
        except Exception as e:self.sd_error=str(e);self.sd=None;self.mounted=False
        return self.mounted

    def unmount_sd(self):
        if self.mounted:vfs.umount('/sd')
        self.mounted=False;self.sd=None

    def ble_irq(self,event,data):
        # BLE IRQ values are ephemeral. Copy bytes, defer UI/file work to loop.
        if event==5 and len(self.ble_pending)<80:
            addr_type,addr,adv_type,rssi,adv_data=data
            self.ble_pending.append((addr_type,bytes(addr),adv_type,rssi,bytes(adv_data)))
        elif event==6:self.scanning=False

    def scan_ble(self):
        self.ble_results=[];self.ble_pending=[];self.scanning=True
        self.ble.gap_scan(5000,30000,30000,False)
        self.show('BLE discovery',['Scanning for 5 seconds','Back returns to menu'])

    def process_ble(self):
        changed=bool(self.ble_pending) or self.ble_draw_state!=self.scanning
        while self.ble_pending:
            at,addr,typ,rssi,adv=self.ble_pending.pop(0)
            name=''
            n=0
            while n<len(adv):
                length=adv[n]
                if length==0 or n+length>=len(adv):break
                if adv[n+1] in (8,9):
                    try:name=adv[n+2:n+1+length].decode()
                    except UnicodeError:name=''
                n+=length+1
            mac=':'.join('%02x'%b for b in addr)
            old=next((i for i,v in enumerate(self.ble_results) if v['address']==mac),None)
            row={'address':mac,'name':name,'rssi':rssi,'address_type':at}
            if old is None:
                if len(self.ble_results)<40:self.ble_results.append(row);self.event('ble',row)
            else:self.ble_results[old]=row
        if self.view=='BLE discovery' and changed:
            self.lines=[('Scanning...' if self.scanning else 'Scan complete')]+[str(r['rssi'])+' '+(r['name'] or r['address']) for r in self.ble_results[:15]]
            self.redraw=True
            self.ble_draw_state=self.scanning

    def scan_wifi(self):
        self.wifi.active(True)
        results=[]
        for ssid,bssid,ch,rssi,security,hidden in self.wifi.scan():
            try:name=ssid.decode()
            except UnicodeError:name=repr(ssid)
            results.append({'ssid':name,'channel':ch,'rssi':rssi,'security':security,'hidden':hidden})
        results.sort(key=lambda v:v['rssi'],reverse=True)
        self.event('wifi_scan',results)
        self.show('Wi-Fi discovery',[str(r['rssi'])+' ch'+str(r['channel'])+' '+r['ssid'] for r in results[:15]])
        return results

    def status(self):
        return {'battery_v':None,'battery_note':'GPIO2 also drives buzzer',
                'charging':None,'sd_mounted':self.mounted,
                'sd_error':self.sd_error,'free_memory':gc.mem_free(),'radio_packets':self.rx_events,
                'cc_band_mhz':CC1101.FREQUENCIES[self.config['cc_band']],
                'nrf_connected':[r is not None for r in self.nrf],'cc_connected':self.cc is not None,
                'wifi_connected':self.wifi.isconnected(),'errors':self.errors}

    def show(self,title,lines):
        self.view=title;self.lines=[str(v) for v in lines];self.redraw=True

    def touch_calibrate(self):
        if self.display is None:raise OSError('Display unavailable')
        points=[(20,40),(220,40),(20,280)];samples=[]
        for x,y in points:
            self.display.clear();self.display.text('Tap the cross',30,10)
            self.display.fill(x-5,y,11,1,0xFFFF);self.display.fill(x,y-5,1,11,0xFFFF)
            begin=ticks_ms()
            while ticks_diff(ticks_ms(),begin)<15000:
                raw=self.display.touch_raw()
                if raw:
                    samples.append(raw)
                    while self.display.touch_raw() is not None and ticks_diff(ticks_ms(),begin)<15000:sleep_ms(20)
                    break
                sleep_ms(20)
            else:raise OSError('Touch calibration timed out')
        self.config['touch_calibration']=affine(samples,points);self.save_config()
        self.show('Touch calibration',['Saved for this display','Select items by tapping'])

    def learn_ir(self):
        self.show('Learn IR',['Press your remote now']);self.draw()
        self.last_ir=self.ir.learn()
        path='/sd/remote.json' if self.mounted else 'remote.json'
        with open(path,'w') as f:json.dump(self.last_ir,f)
        self.event('ir_learn',{'edges':len(self.last_ir['pulses_us']),'path':path})
        self.show('IR learned',[str(len(self.last_ir['pulses_us']))+' pulse intervals','Saved: '+path])

    def send_ir(self):
        if self.last_ir is None:
            path='/sd/remote.json' if self.mounted else 'remote.json'
            with open(path) as f:self.last_ir=json.load(f)
        self.ir.send(self.last_ir);self.event('ir_send',{'edges':len(self.last_ir['pulses_us'])})
        return 'One IR frame sent'

    def packet(self,radio,message,index=0):
        data=message.encode()
        # Minimum spacing prevents accidental repeat storms from UI or console.
        if ticks_diff(ticks_ms(),self.last_send)<500:raise ValueError('Wait 0.5 seconds between messages')
        self.last_send=ticks_ms()
        if radio=='nrf':
            if not 0<=index<3 or self.nrf[index] is None:raise OSError('nRF radio unavailable')
            ok=self.nrf[index].send(data)
        elif radio=='cc':
            if self.cc is None:raise OSError('CC1101 unavailable')
            ok=self.cc.send(data)
        else:raise ValueError('radio must be nrf or cc')
        self.event('packet_tx',{'radio':radio,'index':index,'message':message,'success':ok})
        return {'sent':ok}

    def activate(self):
        choice=self.selected
        if choice==0:self.show('Device status',[k+': '+str(v) for k,v in self.status().items()])
        elif choice==1:self.scan_wifi()
        elif choice==2:self.scan_ble()
        elif choice==3:raise OSError(self.board.RADIO_REASON)
        elif choice==4:raise OSError(self.board.RADIO_REASON)
        elif choice==5:
            band=(self.config['cc_band']+1)%4
            if self.cc is None:raise OSError('CC1101 unavailable')
            self.cc.select(band);self.config['cc_band']=band;self.save_config()
            self.show('Selected RF band',[str(CC1101.FREQUENCIES[band])+' MHz','Correct RF path selected'])
        elif choice==6:self.learn_ir()
        elif choice==7:self.show('Send saved IR',[self.send_ir()])
        elif choice==8:
            if not self.mounted:self.mount_sd()
            if self.mounted:self.config['logging']=not self.config['logging'];self.save_config()
            self.show('SD card / logging',['Mounted: '+str(self.mounted),'Logging: '+str(self.config['logging']),self.sd_error,'Eject via USB before removal'])
        elif choice==9:self.touch_calibrate()
        elif choice==10:
            self.board.rgb.fill((8,4,12));self.board.rgb.write();self.board.beep(80)
            self.show('Lights / sound',['Four LEDs lit; tone played','Back switches LEDs off'])
        elif choice==11:
            if not self.config['wifi_ssid']:self.show('Wi-Fi connection',['Set own network via USB','wifi_config command']);return
            self.wifi.active(True);self.wifi.connect(self.config['wifi_ssid'],self.config['wifi_password'])
            self.show('Wi-Fi connection',['Connecting to configured AP','Status updates via USB'])

    def key(self,n):
        if n==4:
            self.view='menu';self.redraw=True
            self.board.rgb.fill((0,0,0));self.board.rgb.write();return
        if self.view=='menu':
            if n in (1,3):self.selected=(self.selected+(-1 if n==1 else 1))%len(self.MENU);self.redraw=True
            if n==2:self.activate()
        elif self.view in ('2.4GHz packets','Sub-GHz packets'):
            if n==2:
                result=self.packet('nrf' if self.view=='2.4GHz packets' else 'cc',self.config['packet_message'])
                self.lines.append('Sent: '+str(result['sent']));self.redraw=True
            if n in (5,6) and self.view=='2.4GHz packets':
                ch=(self.config['nrf_channel']+(-1 if n==5 else 1))%126
                for radio in self.nrf:
                    if radio:radio.channel(ch)
                self.config['nrf_channel']=ch;self.save_config();self.lines=['Channel '+str(ch),'Listening; OK sends'];self.redraw=True

    def draw(self):
        if self.display is None or not self.redraw:return
        self.redraw=False;self.display.clear()
        self.display.text('Jammer V1',8,8,0x07FF)
        self.display.text(self.view if self.view!='menu' else 'Select a function',8,25)
        if self.view=='menu':
            for i,title in enumerate(self.MENU):
                self.display.text(('> ' if i==self.selected else '  ')+title,4,44+i*17,0xFFE0 if i==self.selected else 0xFFFF)
        else:
            for i,line in enumerate(self.lines[-16:]):self.display.text(line,4,44+i*15)
        self.display.text('Up OK Down / Back Left Right',4,302,0x8410)

    def command(self,request):
        cmd=request.get('cmd')
        if cmd=='help':return {'commands':['status','wifi_scan','ble_scan','ble_results','wifi_config','wifi_connect','wifi_disconnect','packet','radio_config','ir_learn','ir_send','ir_frame','sd_mount','sd_eject','sd_list','logging','touch_calibrate','touch_raw','rgb','beep','backlight','usb_limit','message','events','reset']}
        if cmd=='status':return self.status()
        if cmd=='wifi_scan':return self.scan_wifi()
        if cmd=='ble_scan':self.scan_ble();return {'scanning':True}
        if cmd=='ble_results':return self.ble_results
        if cmd=='wifi_config':
            self.config['wifi_ssid']=str(request['ssid']);self.config['wifi_password']=str(request['password']);self.save_config();return {'saved':True}
        if cmd=='wifi_connect':self.activate_wifi();return {'connecting':True}
        if cmd=='wifi_disconnect':self.wifi.disconnect();self.wifi.active(False);return {'disconnected':True}
        if cmd=='packet':return self.packet(request['radio'],request['message'],int(request.get('index',0)))
        if cmd=='radio_config':
            if 'band' in request:
                band=int(request['band'])
                if self.cc is None:raise OSError('CC1101 unavailable')
                self.cc.select(band);self.config['cc_band']=band
            if 'channel' in request:
                channel=int(request['channel'])
                for r in self.nrf:
                    if r:r.channel(channel)
                if not 0<=channel<=125:raise ValueError('channel must be 0..125')
                self.config['nrf_channel']=channel
            self.save_config();return {'band':self.config['cc_band'],'channel':self.config['nrf_channel']}
        if cmd=='ir_learn':self.learn_ir();return self.last_ir
        if cmd=='ir_send':return self.send_ir()
        if cmd=='ir_frame':return self.last_ir
        if cmd=='sd_mount':return {'mounted':self.mount_sd() if not self.mounted else True}
        if cmd=='sd_eject':self.unmount_sd();return {'ejected':True}
        if cmd=='sd_list':return os.listdir('/sd') if self.mounted else []
        if cmd=='logging':
            enabled=bool(request['enabled'])
            if enabled and not self.mounted:raise OSError('Insert and mount a FAT SD card first')
            self.config['logging']=enabled;self.save_config();return {'logging':enabled}
        if cmd=='touch_calibrate':self.touch_calibrate();return {'calibration':self.config['touch_calibration']}
        if cmd=='touch_raw':return self.display.touch_raw() if self.display else None
        if cmd=='rgb':
            color=tuple(int(v) for v in request['color'])
            if len(color)!=3 or any(v<0 or v>32 for v in color):raise ValueError('RGB components must be 0..32')
            self.board.rgb.fill(color);self.board.rgb.write();return {'color':color}
        if cmd=='beep':self.board.beep(int(request.get('ms',40)));return {'tone':True}
        if cmd=='backlight':self.board.backlight(bool(request['enabled']));return {'enabled':bool(request['enabled'])}
        if cmd=='usb_limit':
            high=request.get('ma')==500
            if high and request.get('source_granted_500ma') is not True:raise ValueError('Source must have granted 500mA')
            self.board.usb500(high);return {'ma':500 if high else 100}
        if cmd=='message':
            text=str(request['text'])
            if not 1<=len(text.encode())<=31:raise ValueError('Stored message must fit nRF 31-byte payload')
            self.config['packet_message']=text;self.save_config();return {'saved':True}
        if cmd=='events':return self.events
        if cmd=='reset':
            import machine
            self.board.safe();machine.reset()
        raise ValueError('Unknown command: '+str(cmd))

    def activate_wifi(self):
        if not self.config['wifi_ssid']:raise ValueError('Configure your own network first')
        self.wifi.active(True);self.wifi.connect(self.config['wifi_ssid'],self.config['wifi_password'])

    def usb_console(self):
        # Read only ready characters; a partial line never blocks radio polling.
        for _ in range(128):
            if not self.poll.poll(0):return
            char=sys.stdin.read(1)
            if char in ('\r','\n'):
                line=self.console_buffer;self.console_buffer=''
                if not line:continue
                try:answer={'ok':True,'result':self.command(json.loads(line))}
                except Exception as e:answer={'ok':False,'error':str(e)}
                print(json.dumps(answer))
            elif len(self.console_buffer)<1024:self.console_buffer+=char
            else:self.console_buffer='';print('{"ok":false,"error":"command exceeds 1024 characters"}')

    def loop(self):
        while True:
            self.usb_console()
            for key in self.board.keys():
                try:self.key(key)
                except Exception as e:self.show('Action failed',[str(e),'Back returns to menu'])
            now=ticks_ms()
            if self.display and ticks_diff(now,self.last_touch)>300:
                touch=self.display.touch(self.config['touch_calibration'])
                if touch:
                    x,y=touch;self.last_touch=now
                    if self.view=='menu' and 44<=y<44+len(self.MENU)*17:
                        self.selected=(y-44)//17
                        try:self.activate()
                        except Exception as e:self.show('Action failed',[str(e)])
                    elif y>=290:self.key(4)
            if ticks_diff(now,self.last_poll)>25:
                self.last_poll=now
                for n,r in enumerate(self.nrf):
                    if r:
                        try:
                            data=r.recv()
                            if data is not None:
                                encoded=hexlify(data).decode()
                                self.rx_events+=1;self.event('nrf_rx',{'index':n,'hex':encoded})
                                if self.view=='2.4GHz packets':self.lines=(self.lines+['RX '+str(n+1)+': '+encoded])[-16:];self.redraw=True
                        except OSError as e:self.errors.append(str(e));self.nrf[n]=None
                if self.cc:
                    try:
                        data=self.cc.recv()
                        if data:
                            data['payload']=hexlify(data['payload']).decode();self.rx_events+=1;self.event('cc_rx',data)
                            if self.view=='Sub-GHz packets':self.lines=(self.lines+['RX: '+data['payload'],str(data['rssi'])+' dBm'])[-16:];self.redraw=True
                    except OSError as e:self.errors.append(str(e));self.cc=None
                if self.mounted and self.board.card_detect():
                    try:self.unmount_sd()
                    except OSError: self.mounted=False
                    self.sd_error='Card removed; use SD eject before removal'
                if self.ble_pending or self.view=='BLE discovery':self.process_ble()
            self.draw();sleep_ms(10)


def main():
    app=None
    try:
        app=Application();app.loop()
    finally:
        if app:
            app.ble.gap_scan(None)
            for r in app.nrf:
                if r:r.standby()
            if app.cc:app.cc.standby()
            if app.mounted:app.unmount_sd()
            app.board.safe()


if __name__=='__main__':main()
