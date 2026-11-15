"""SR259 revision-03 RAW10 capture through the vendor ION/DCAM ABI.

Caller owns exclusive camera access and an open Sensor(1), checks its fresh
identity, and publishes a processed image only after outer sensor cleanup.
Exact sensor register tables remain in a separate private hash-checked profile.
1616x1212 halfword frame acquisition and ordered cleanup have been verified.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import sys
import time

import tablet_camera_capture as core

PROFILE_SHA256='dc92e4f848e49ebfda457287966380571917e29d97596ec82b161e2a83a80706'
TABLE_SHA256='9357e467dd10a5099054d90ec5de62950772bbcf530db758180537aff35ce9fd'
W,H=1616,1212
GREY=int.from_bytes(b'GREY','little')
DCAM_SENSOR_RAW=3 # Kernel dcam_cap_sensor_mode; SPI is1, vendor RAW format is4.
RAW_BYTES=W*H*2
UV_OFFSET=((RAW_BYTES+4095)//4096)*4096
BUFFER_BYTES=UV_OFFSET+4096
GAINS={1:(0xef,0),2:(0x6f,0),4:(0x2f,0),8:(0x0f,0)}
EXPOSURES=(333,1296) # Observed stock mode and verified frame-4 integration.


def phase(name,**values):
    core.emit(name,**values)
    # Supervisors retain regular private logs. Pipes/GUI captures can only flush.
    try:
        descriptor=sys.stdout.fileno()
    except (AttributeError,OSError,ValueError):
        return
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.fsync(descriptor)


def validate_profile(p):
    try:
        expected=(core.HAL_SHA256,'SR259',0xe1,3,1,W,H,26,702,4,1,10,2)
        actual=tuple(p[k] for k in ('hal_sha256','sensor','id','revision','mode_index',
                     'width','height','xclk_mhz','bps_per_lane','phy_id','lanes','bits','image_pattern'))
        if actual!=expected:
            raise core.CaptureError('Unrecognized SR259 profile metadata')
        table=p['registers']
        if len(table)!=676 or any(len(pair)!=2 or any(type(v) is not int or not 0<=v<=255
                                                     for v in pair) for pair in table):
            raise core.CaptureError('Invalid 8-bit SR259 register profile')
        digest=hashlib.sha256(b''.join(struct.pack('<HH',*r) for r in table)).hexdigest()
        if digest!=TABLE_SHA256 or p['register_sha256']!=TABLE_SHA256:
            raise core.CaptureError('SR259 register profile digest mismatch')
        if p['stream_on']!=[[3,0],[1,0]] or p['stream_off']!=[[3,0],[1,1]]:
            raise core.CaptureError('Unrecognized SR259 stream protocol')
    except (KeyError,TypeError,ValueError,struct.error) as error:
        raise core.CaptureError('Malformed SR259 profile') from error
    return p


def load_profile(path):
    data=Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest()!=PROFILE_SHA256:
        raise core.CaptureError('Private SR259 profile file digest mismatch')
    return validate_profile(json.loads(data))


def validate_output(output):
    output=Path(output)
    if (not output.is_absolute() or '..' in output.parts or 'private' not in output.parts
            or output.suffix!='.raw16' or not output.parent.is_dir()
            or output.parent.is_symlink() or os.path.lexists(output)
            or os.path.lexists(output.with_suffix('.json'))):
        raise core.CaptureError('New absolute .raw16 path in an existing private directory required')
    info=output.parent.stat()
    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise core.CaptureError('Front raw output directory must be account-owned and mode0700')
    return output


def require_front(sensor, identity):
    if (sensor.sensor != 1 or sensor.fd is None or identity.get('sensor') != 'front'
            or identity.get('chip_id') != '0xe1' or identity.get('version') != '0x3'
            or identity.get('match') is not True):
        raise core.CaptureError('Fresh powered SR259 revision03 identity required')

def write8(sensor, control, address, value):
    if not 0 <= address <= 255 or not 0 <= value <= 255:
        raise core.CaptureError('8-bit register/value required')
    sensor.ioctl(12, control.Register(address, value, 0))

def read8(sensor, address):
    value = sensor.read(address, 0)
    if not 0 <= value <= 255:
        raise core.CaptureError('Out-of-range SR259 readback')
    return value

def read_controls(sensor, control, identity):
    """Exact page/register reads reached by stock callbacks; bounded 8 reads."""
    require_front(sensor, identity)
    write8(sensor, control, 3, 0)
    line_h, line_l = read8(sensor, 0x4c), read8(sensor, 0x4d)
    frame_h, frame_l = read8(sensor, 0x4e), read8(sensor, 0x4f)
    write8(sensor, control, 3, 0x20)
    exp_h, exp_l = read8(sensor, 0x22), read8(sensor, 0x23)
    gain_h, gain_l = read8(sensor, 0x60), read8(sensor, 0x61)
    gain_code = 2 * gain_h + (gain_l & 1)
    return {'line_length': 256*line_h+line_l, 'frame_length': 256*frame_h+frame_l,
            'exposure_lines': 256*exp_h+exp_l, 'gain_registers': [gain_h,gain_l],
            'gain_code': gain_code, 'gain_multiplier_from_stock_formula': 512/(gain_code+34)}

def write_initial_exposure(sensor, control, identity, before, journal=None, gain=1,
                           exposure_lines=1296):
    """One verified exposure endpoint and a stock gain from GAINS.

    Preserves mode1 frame length1300; no exposure loop inside the DMA owner.
    Writes mirror writeAE followed by writegain callbacks, including their
    page0/reg1f update begin/release sequence. Returns target for later readback.
    """
    require_front(sensor, identity)
    if type(gain) is not int or gain not in GAINS:
        raise core.CaptureError('Analog gain must be exactly 1, 2, 4 or 8')
    if type(exposure_lines) is not int or exposure_lines not in EXPOSURES:
        raise core.CaptureError('Exposure must be exactly the verified 333 or1296 lines')
    if before['frame_length'] != 1300:
        raise core.CaptureError('Expected exact mode1 frame length1300 before exposure trial')
    exposure = exposure_lines
    journal = {} if journal is None else journal
    journal.update(before=dict(before), target_exposure_lines=exposure,
                   target_gain_units128=128*gain, hold_attempted=False,
                   hold_released=False, write_error=None, release_error=None)
    sequence = ((3,0), (0x1f,1), (3,0x20), (0x22,exposure >> 8),
                (0x23,exposure & 255), (3,0x20), (0x60,GAINS[gain][0]), (0x61,GAINS[gain][1]),
                )
    try:
        for address,value in sequence:
            if address == 0x1f and value == 1:
                journal['hold_attempted'] = True
            write8(sensor,control,address,value)
    except BaseException as error:
        journal['write_error'] = str(error)
        raise
    finally:
        if journal['hold_attempted']:
            try:
                write8(sensor,control,3,0)
                write8(sensor,control,0x1f,0)
                journal['hold_released'] = True
            except BaseException as error:
                journal['release_error'] = str(error)
                raise
    return {'exposure_lines': exposure, 'gain_units128':128*gain,
            'gain_registers':list(GAINS[gain]), 'frame_length':1300}

class RawBuffer(core.Buffer):
    def addresses(self):
        # _dcam_path_set_next_frm unconditionally panics on uaddr==0 even for
        # path0 RAW HALF_WORD. The RAW branch does not program its UV register.
        # Supply an address in our extra owned guard page, never an arbitrary
        # nonzero address or an address beyond the mapping.
        if self.length<BUFFER_BYTES or self.mmu.iova_size<BUFFER_BYTES:
            raise core.CaptureError('Front allocation has no owned UV guard page')
        return core.Address(self.mmu.iova_addr,self.mmu.iova_addr+UV_OFFSET,0)

def completed(op,buffers):
    if op.evt!=0:
        raise core.CaptureError('Front DCAM event '+hex(op.evt))
    frame=core.Frame.from_buffer_copy(op.payload)
    if (frame.channel_id,frame.width,frame.height,frame.img_fmt)!=(0,W,H,GREY):
        raise core.CaptureError('Unexpected front frame geometry/channel/format')
    for buffer in buffers:
        owned=buffer.addresses()
        if (frame.yaddr,frame.uaddr,frame.vaddr)==(owned.y,owned.u,owned.v):
            return frame,buffer
    raise core.CaptureError('Unowned front DMA completion')

def capture_one(sensor,control,identity,profile,private_raw_output=None,timeout=20,initialize_exposure=False,
                gain=1,exposure_lines=1296,frame_sink=None):
    """Save private raw evidence after DMA cleanup; caller owns sensor teardown.

    initialize_exposure writes a verified exposure endpoint and one gain from
    GAINS (1,2,4,8); both take effect on the first completed frame.
    With frame_sink, stream frames to it instead and save nothing.
    """
    require_front(sensor,identity)
    p=validate_profile(profile)
    if type(timeout) not in (int,float) or not 1<=timeout<=60 or type(initialize_exposure) is not bool:
        raise core.CaptureError('Bounded timeout1..60 and boolean exposure option required')
    if type(gain) is not int or gain not in GAINS or (gain!=1 and not initialize_exposure):
        raise core.CaptureError('Gain 1,2,4 or 8 requires exposure initialization for gain above 1')
    if type(exposure_lines) is not int or exposure_lines not in EXPOSURES:
        raise core.CaptureError('Exposure must be exactly the verified 333 or1296 lines')
    output=None if frame_sink else validate_output(private_raw_output)
    streamed=0
    core.abi_check(runtime=True)
    controls={'before':None,'initialization':None,'after':None}
    image=ion=None
    buffers=[]
    csi=stream=False
    result=None
    handled=(signal.SIGALRM,signal.SIGTERM,signal.SIGINT)
    previous={s:signal.getsignal(s) for s in handled}
    def interrupted(*_):
        raise core.CaptureError('Front capture interrupted; ordered cleanup')
    for sig in handled: signal.signal(sig,interrupted)
    signal.setitimer(signal.ITIMER_REAL,timeout)
    native=core.Native()
    def write8(a,v): sensor.ioctl(12,control.Register(a,v,0))
    try:
        phase('front-mode',registers=676,width=W,height=H)
        for a,v in p['registers']: write8(a,v)
        # Exact stock mode dispatch: literal 0x13880 at 0x77d44 -> usleep80ms.
        time.sleep(0.080)
        controls['before']=read_controls(sensor,control,identity)
        phase('front-controls-before',controls=controls['before'])
        if initialize_exposure:
            journal={}
            controls['initialization']=journal
            try:
                write_initial_exposure(sensor,control,identity,controls['before'],journal,gain,exposure_lines)
            finally:
                phase('front-exposure-initialization',journal=journal)
            controls['after']=read_controls(sensor,control,identity)
            phase('front-controls-after',controls=controls['after'])
            if (controls['after']['exposure_lines']!=exposure_lines
                    or controls['after']['frame_length']!=1300
                    or controls['after']['gain_registers']!=list(GAINS[gain])):
                raise core.CaptureError('SR259 initialized exposure/gain readback mismatch')
        # Exact vendor stream-off callback; hold the sensor while queuing DMA.
        for a,v in p['stream_off']: write8(a,v)
        image=os.open('/dev/sprd_image',os.O_RDWR|os.O_CLOEXEC)
        ion=os.open('/dev/ion',os.O_RDWR|os.O_CLOEXEC)
        csi=True
        phase('front-csi-open',phy_id=4,lanes=1,bps_per_lane=702)
        sensor.ioctl(19,core.CsiIf(0,1,4,1,702))
        interface=core.SensorIf(1,DCAM_SENSOR_RAW,2,0) # CSI2, RAW, pattern2.
        interface.mipi[:]=(0,10,1,1,702) # Expand RAW10 to halfwords in DCAM.
        phase('front-dcam-config',sensor_mode=interface.img_fmt,bits=10,loose=1)
        native.ioctl(image,'Z',9,interface)
        native.ioctl(image,'Z',0,core.U32(1))
        native.ioctl(image,'Z',2,core.Size(W,H))
        native.ioctl(image,'Z',3,core.Rect(0,0,W,H))
        parm=core.Parm(channel_id=0,pixel_fmt=GREY,camera_id=1,
                       crop_rect=core.Rect(0,0,W,H),dst_size=core.Size(W,H))
        native.ioctl(image,'Z',7,parm)
        selected=native.ioctl(image,'Z',17,core.U32(0xffffffff),2)
        if selected.value!=0: raise core.CaptureError('Expected raw DCAM path0')
        native.ioctl(image,'Z',5,parm)
        fmt=native.ioctl(image,'Z',19,core.Format(0,W,H,GREY,0,0,0,0),3)
        phase('front-format',endian=list(bytes(fmt.reserved)[:2]),loose=1,bits=10,
              sensor_mode=interface.img_fmt)
        native.ioctl(image,'Z',4,core.Parm(channel_id=0,frame_base_id=0x200))
        native.ioctl(image,'Z',11,core.Parm(channel_id=0,deci=0))
        native.ioctl(image,'Z',1,core.U32(1))
        length=BUFFER_BYTES
        phase('front-ion',count=4,bytes_each=length,master=1,heap_mask=2,uv_guard=True)
        for _ in range(4): buffers.append(RawBuffer(native,ion,length))
        for index,buffer in enumerate(buffers):
            native.ioctl(image,'Z',10,core.Parm(channel_id=0,is_reserved_buf=int(index==3),
                                               frame_addr=buffer.addresses(),buf_flag=0))
        stream=True
        phase('front-stream-on')
        native.ioctl(image,'Z',14,core.U32(1))
        phase('front-sensor-stream-on')
        for a,v in p['stream_on']: write8(a,v)
        if frame_sink:
            streamed=core.stream(native,image,ion,buffers,0,completed,frame_sink)
        else:
            for _ in range(8):
                op=core.ReadOp(1,0xffffffff)
                ret=native.libc.read(image,C.byref(op),C.sizeof(op))
                if ret<0: raise OSError(C.get_errno(),'Front descriptor read')
                if ret!=0: raise core.CaptureError('Unexpected front read result')
                if op.evt==4: continue
                result=completed(op,buffers[:3])
                phase('front-frame',width=W,height=H)
                break
            if result is None: raise core.CaptureError('No completed front frame')
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        try:
            if image is not None and stream:
                phase('front-stream-off')
                native.ioctl(image,'Z',15,core.U32(0))
            for a,v in p['stream_off']: write8(a,v)
            if image is not None:
                stop=(core.U32*3)(2,0,0)
                ret=native.libc.write(image,C.byref(stop),C.sizeof(stop))
                if ret not in (0,1): raise core.CaptureError('Front cancel failed')
                os.close(image)
                image=None
            if csi:
                phase('front-csi-close')
                sensor.ioctl(19,core.CsiIf(1,1,4,1,702))
        except BaseException as error:
            if image is not None or csi or stream:
                core.park('front-stop',error)
            raise # No DMA/CSI was opened; outer Sensor cleanup remains responsible.
        try:
            if result is not None:
                frame,buffer=result
                native.ioctl(ion,'I',6,core.IonCustom(13,buffer.fd),3)
                raw=bytes(buffer.mapping[:W*H*2])
                result={'width':W,'height':H,'bits':10,'halfword':True,
                        'endian_enum':list(bytes(fmt.reserved)[:2]),
                        'timestamp':[frame.sec,frame.usec],
                        'identity':dict(identity),'gain':gain,'sensor_controls':controls,'sample_storage':'le16',
                        'raw_sha256':hashlib.sha256(raw).hexdigest(),'raw':raw}
            for buffer in reversed(buffers): buffer.close()
            if ion is not None: os.close(ion)
        except BaseException as error:
            core.park('front-ion-release',error)
        for sig,handler in previous.items(): signal.signal(sig,handler)
        phase('front-resources-released')
    if frame_sink:
        return {'frames':streamed}
    raw=result.pop('raw')
    for name,data in ((output,raw),(output.with_suffix('.json'),(json.dumps(result,indent=2)+'\n').encode())):
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as file: file.write(data)
    return result
