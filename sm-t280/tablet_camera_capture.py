"""S5K4ECGA bank-1 still capture with the exact vendor ION/DCAM ABI.

Manager-reviewed caller owns tablet-camera.exclusive() and an open Sensor(0).
Only its fresh successful identify() result (including 0xa044 variant) is accepted.
Vendor register tables stay in a separate private, hash-checked profile.
Live 320x240 NV12 capture and ordered teardown have been verified.
"""
import ctypes as C
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import signal
import struct
import sys
import time

U32 = C.c_uint32
I32 = C.c_int32
# Bank-1 preview records: (record, variant, register count, table SHA-256).
# All share 344 Mbps/lane CSI timing and full-size trim in the HAL profile.
MODES = {
    (320, 240): (5, 107, 33, '1600bfba0099786bd4bef627eaeb30bd7c1311d1663be63f13e9d6094417a4a6'),
    (640, 480): (2, 105, 30, 'f7bfe365be7fd6bdf2a5c3a685d7d83a1129b1b45cfa452c1e0ddbbe8d1f5689'),
    (1280, 720): (1, 97, 33, 'd2048ec7f2117017829fdd7db4bbb6788f3f07ad52c7002d791a59c254ca812c'),
}
W, H = 320, 240
NV12 = int.from_bytes(b'NV12', 'little')
HAL_SHA256 = '0bfbb8eb5db3cbe596b97d0a81bcaca9102728e2ab6d17e7e7cf8e335040cee7'
INIT_SHA256 = '2eb4f9c47db3f8b1181d3a2e931e7070c1e1d09e844c93b24d4f12435a17218b'


def select_mode(width, height):
    """Choose a verified preview record before gate(); 320x240 is the default."""
    global W, H
    if (width, height) not in MODES:
        raise CaptureError('Unsupported rear mode')
    W, H = width, height
PROFILE_SHA256 = 'ca34af790c5946313a5f34ee4ae208bea35fb0e3af8e48eac53e3d1b30ebb08f'


def load_profile(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROFILE_SHA256:
        raise CaptureError('Private sensor profile digest mismatch')
    profile = json.loads(raw)
    gate(profile, {'sensor':'rear', 'chip_id':'0x4ec0', 'version':'0x11',
                   'variant_id':'0x1414', 'mode_bank':1, 'match':True})
    abi_check()
    return profile


def publish_image(ppm, output):
    """Encode only after hardware cleanup, then exclusively create the output."""
    output = Path(output)
    suffix = output.suffix.lower()
    if suffix == '.ppm':
        data = Path(ppm).read_bytes()
    elif suffix in ('.jpg', '.jpeg', '.png'):
        import gi
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        image = GdkPixbuf.Pixbuf.new_from_file(str(ppm))
        encoded = Path(ppm).with_suffix(suffix)
        kind = 'png' if suffix == '.png' else 'jpeg'
        keys, values = ([], []) if kind == 'png' else (['quality'], ['90'])
        if not image.savev(str(encoded), kind, keys, values):
            raise CaptureError('Image encoding failed')
        data = encoded.read_bytes()
    else:
        raise CaptureError('Output must use .jpg, .jpeg, .png or .ppm')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()}


class CaptureError(Exception):
    pass


class Size(C.LittleEndianStructure):
    _fields_ = [('w', U32), ('h', U32)]


class Rect(C.LittleEndianStructure):
    _fields_ = [('x', U32), ('y', U32), ('w', U32), ('h', U32)]


class Address(C.LittleEndianStructure):
    _fields_ = [('y', U32), ('u', U32), ('v', U32)]


class Parm(C.LittleEndianStructure):
    _fields_ = [(k, U32) for k in ('channel_id', 'frame_base_id', 'pixel_fmt',
               'need_isp_tool', 'deci', 'shrink', 'index', 'need_isp', 'camera_id',
               'is_reserved_buf', 'buf_flag')] + [
               ('crop_rect', Rect), ('dst_size', Size), ('frame_addr', Address),
               ('frame_addr_vir', Address), ('reserved', U32*4)]


class Format(C.LittleEndianStructure):
    _fields_ = [(k, U32) for k in ('channel_id', 'width', 'height', 'fourcc',
               'need_isp', 'need_binning', 'bytesperline', 'is_lightly')] + [('reserved', U32*4)]


class SensorIf(C.LittleEndianStructure):
    _fields_ = [(k, U32) for k in ('if_type', 'img_fmt', 'img_ptn', 'frm_deci')] + [
               ('reserved', U32*4), ('mipi', U32*5)]


class CsiIf(C.LittleEndianStructure):
    _fields_ = [(k, U32) for k in ('is_open', 'if_type', 'phy_id', 'lanes', 'bps_per_lane')]


class Frame(C.LittleEndianStructure):
    _fields_ = [(k, U32) for k in ('channel_id', 'width', 'height', 'length', 'sec',
               'usec', 'frm_base_id', 'index', 'real_index', 'img_fmt', 'yaddr',
               'uaddr', 'vaddr', 'yaddr_vir', 'uaddr_vir', 'vaddr_vir')] + [('reserved', U32*4)]


class ReadOp(C.LittleEndianStructure):
    # 172-byte union (path capability) is larger than frame80.
    _fields_ = [('cmd', U32), ('evt', U32), ('payload', C.c_ubyte*172)]


class Alloc(C.LittleEndianStructure):
    _fields_ = [('length', U32), ('align', U32), ('heap_mask', U32), ('flags', U32), ('handle', I32)]


class IonFd(C.LittleEndianStructure):
    _fields_ = [('handle', I32), ('fd', I32)]


class IonCustom(C.LittleEndianStructure):
    _fields_ = [('cmd', U32), ('argument', U32)]


class IonMmu(C.LittleEndianStructure):
    _fields_ = [('master_id', I32), ('fd_buffer', I32), ('iova_addr', U32), ('iova_size', U32)]


def abi_check(runtime=False):
    for kind, size in ((Parm,108),(Format,48),(SensorIf,52),(CsiIf,20),
                       (Frame,80),(ReadOp,180),(Alloc,20),(IonFd,8),(IonCustom,8),(IonMmu,16)):
        if C.sizeof(kind) != size:
            raise CaptureError(f'ABI size mismatch {kind.__name__}')
    if Parm.frame_addr.offset != 68 or ReadOp.payload.offset != 8:
        raise CaptureError('ABI offset mismatch')
    if runtime and (sys.platform != 'linux' or struct.calcsize('P') != 4 or sys.byteorder != 'little'
                    or not platform.machine().startswith('arm')):
        raise CaptureError('Runtime requires ARM32 little-endian Linux')


def ioc(magic, nr, obj, direction=1):
    return (direction << 30) | (C.sizeof(obj) << 16) | (ord(magic) << 8) | nr


class Native:
    def __init__(self):
        self.libc = C.CDLL(None, use_errno=True)
        self.libc.ioctl.argtypes = [C.c_int,C.c_ulong,C.c_void_p]
        self.libc.ioctl.restype = C.c_int
        self.libc.read.argtypes = [C.c_int,C.c_void_p,C.c_size_t]
        self.libc.read.restype = C.c_ssize_t
        self.libc.write.argtypes = [C.c_int,C.c_void_p,C.c_size_t]
        self.libc.write.restype = C.c_ssize_t

    def ioctl(self, fd, magic, nr, obj, direction=1):
        ret = self.libc.ioctl(fd, ioc(magic,nr,obj,direction), C.byref(obj))
        if ret < 0:
            raise OSError(C.get_errno(), f'{magic} ioctl {nr}')
        if ret != 0:
            raise CaptureError(f'{magic} ioctl {nr} returned {ret}')
        return obj


def gate(profile, identity):
    if profile.get('hal_sha256') != HAL_SHA256 or profile.get('resolution_stride') != 24:
        raise CaptureError('Unrecognized private profile provenance/layout')
    if (identity.get('sensor') != 'rear' or identity.get('chip_id') != '0x4ec0'
            or identity.get('version') not in ('0x11','0x0011')
            or identity.get('variant_id') != '0x1414' or identity.get('mode_bank') != 1
            or identity.get('match') is not True):
        raise CaptureError('Fresh S5K4ECGA bank1 identity required')
    bank = profile['rear_banks'][1]
    record, variant, count, mode_sha256 = MODES[W, H]
    init, mode = bank[0], bank[record]
    tables = profile['rear_private_table_variants'][1]
    if init['registers'] != tables[2]['registers'] or mode['registers'] != tables[variant]['registers']:
        raise CaptureError('Mode/variant dictionary disagreement')
    if (init['count'],mode['count'],mode['width'],mode['height'],mode['xclk_mhz']) != (3254,count,W,H,24):
        raise CaptureError('Unrecognized mode')
    if profile['rear_trims'][record] != {'trim':[0,0,W,H], 'line_time':0, 'bps_per_lane':344,
                                   'frame_line':0, 'scaler_trim':[0,0,W,H]}:
        raise CaptureError('Unrecognized receiver timing')
    for item in (init,mode):
        if len(item['registers']) != item['count']:
            raise CaptureError('Register profile length/count mismatch')
        for address,value in item['registers']:
            if not 0 <= address <= 65535 or not 0 <= value <= 65535:
                raise CaptureError('Invalid register profile')
            if address == 65535 and value > 1000:
                raise CaptureError('Unbounded register-table delay')
    for item,expected in ((init,INIT_SHA256),(mode,mode_sha256)):
        digest = hashlib.sha256(b''.join(struct.pack('<HH',*x) for x in item['registers'])).hexdigest()
        if digest != expected:
            raise CaptureError('Register profile digest mismatch')
    return init['registers'], mode['registers']


def emit(phase, **values):
    print(json.dumps({'capture_phase':phase, **values}), flush=True)


class Buffer:
    def __init__(self, native, ion, length):
        self.native,self.ion,self.length = native,ion,length
        self.handle,self.fd,self.mapping,self.mmu = None,None,None,None
        try:
            # Matches HAL system heap mask2 on IOMMU-enabled MM; uncached0 is
            # writecombined by ion_mmap. No guessed physical address fallback.
            alloc = native.ioctl(ion,'I',0,Alloc(length,4096,2,0,0),3)
            self.handle = alloc.handle
            if alloc.handle <= 0:
                raise CaptureError('Invalid ION handle')
            shared = native.ioctl(ion,'I',4,IonFd(alloc.handle,-1),3)
            self.fd = shared.fd
            if self.fd < 0:
                raise CaptureError('Invalid ION export fd')
            self.mapping = mmap.mmap(self.fd,length,flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ|mmap.PROT_WRITE)
            self.mmu = IonMmu(1,self.fd,0,0) # ION_MM, not ION_DCAM.
            native.ioctl(ion,'I',6,IonCustom(9,C.addressof(self.mmu)),3)
            if not self.mmu.iova_addr or self.mmu.iova_size < length:
                raise CaptureError('Invalid MM IOVA allocation')
            if self.mmu.iova_addr + length > 2**32:
                raise CaptureError('IOVA range exceeds ARM32')
            self.mapping[:] = b'\xa5'*length
        except BaseException:
            try:
                self.close()
            except BaseException as error:
                park('partial-ion-allocation-release',error)
            raise

    def close(self):
        if self.mmu is not None and self.mmu.iova_addr:
            self.native.ioctl(self.ion,'I',6,IonCustom(10,C.addressof(self.mmu)),3)
            self.mmu = None
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.handle is not None:
            self.native.ioctl(self.ion,'I',1,I32(self.handle),3)
            self.handle = None

    def addresses(self):
        return Address(self.mmu.iova_addr,self.mmu.iova_addr+W*H,0)


def frame_buffer(read_op, buffers):
    if read_op.evt != 0:
        raise CaptureError(f'DCAM event {read_op.evt:#x}, not frame completion')
    frame = Frame.from_buffer_copy(read_op.payload)
    if (frame.channel_id,frame.width,frame.height,frame.img_fmt) != (1,W,H,NV12):
        raise CaptureError('Unexpected completed frame geometry/channel/format')
    for buffer in buffers:
        addresses = buffer.addresses()
        if (frame.yaddr,frame.uaddr,frame.vaddr) == (addresses.y,addresses.u,addresses.v):
            return frame,buffer
    raise CaptureError('DCAM completed an unowned DMA address')


def nv12_ppm(raw, width=None, height=None):
    width, height = width or W, height or H
    if width <= 0 or height <= 0 or width % 2 or height % 2 or len(raw) != width*height*3//2:
        raise CaptureError('Malformed NV12 geometry/length')
    out = bytearray(width*height*3)
    clamp = lambda x: max(0,min(255,x))
    for y in range(height):
        for x in range(width):
            luminance = raw[y*width+x]-16
            chroma = width*height+(y//2)*width+(x//2)*2
            u,v = raw[chroma]-128,raw[chroma+1]-128
            pos = (y*width+x)*3
            out[pos:pos+3] = bytes((clamp((298*luminance+409*v+128)>>8),
                                   clamp((298*luminance-100*u-208*v+128)>>8),
                                   clamp((298*luminance+516*u+128)>>8)))
    return f'P6\n{width} {height}\n255\n'.encode()+out


STOCK_15FPS_SHA256 = 'a787daf41d088d0adae1bb284be8ac8aa74dc95fce5559ec309c9d4b4ae48e84'


def apply_stock_15fps(sensor, profile):
    """Write the stock GA 15 fps table from the private profile after the mode table."""
    fps = profile['rear_private_table_variants'][1][71]
    if (fps['id'], fps['name'], fps['table_va'], fps['count']) != (71, 'regs_s5k4ecga_15_FPS', '0x13b690', 17):
        raise CaptureError('Unrecognized stock GA 15 FPS table metadata')
    registers = fps['registers']
    digest = hashlib.sha256(b''.join(struct.pack('<HH', *pair) for pair in registers)).hexdigest()
    if len(registers) != 17 or digest != STOCK_15FPS_SHA256:
        raise CaptureError('Unrecognized stock GA 15 FPS register payload')
    emit('apply-stock-rear-frame-rate', table_id=71, nominal_fps=15)
    for address, value in registers:
        sensor.write(address, value)


def stream(native, image, ion, buffers, channel, completed, frame_sink):
    """Pass each completed frame to frame_sink and requeue it until frame_sink returns False."""
    frames = busy = 0
    while True:
        op = ReadOp(1, 0xffffffff)
        ret = native.libc.read(image, C.byref(op), C.sizeof(op))
        if ret < 0:
            raise OSError(C.get_errno(), 'DCAM frame descriptor read')
        if ret != 0:
            raise CaptureError(f'Unexpected vendor descriptor-read result {ret}')
        if op.evt == 4:
            busy += 1
            if busy == 8:
                raise CaptureError('DCAM queue busy for eight consecutive reads')
            continue
        busy = 0
        frame, buffer = completed(op, buffers[:3])
        native.ioctl(ion, 'I', 6, IonCustom(13, buffer.fd), 3)
        with memoryview(buffer.mapping) as view:
            keep = frame_sink(view)
        frames += 1
        signal.setitimer(signal.ITIMER_REAL, 10)
        if not keep:
            return frames
        native.ioctl(image, 'Z', 10, Parm(channel_id=channel, buf_flag=1, frame_addr=buffer.addresses()))


def park(phase, error):
    # Process must retain its Sensor/exclusive lock and DMA objects. Never let
    # a cleanup exception unwind a caller context while DMA state is uncertain.
    emit('quarantined', failed_phase=phase, error=str(error),
         instruction='Do not retry or kill this worker; review retained resources.')
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGALRM):
        signal.signal(sig,signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_REAL,0)
    while True:
        signal.pause()


def capture_one(sensor, identity, profile, output=None, timeout=12, frame_sink=None):
    """Capture private NV12+PPM after fresh manager-controlled rear identification.

    With frame_sink, stream at the stock 15 fps instead and save nothing.
    Caller MUST own exclusive() throughout, and MUST NOT SIGKILL an unfinished
    worker. Interrupted capture attempts cleanup. Uncertain teardown parks with
    all mappings, fds, Sensor and camera ownership retained for manager review.
    """
    init,mode = gate(profile,identity) # Must reject before any device activation.
    abi_check(runtime=True)
    if sensor.sensor != 0 or sensor.fd is None or not 1 <= timeout <= 60:
        raise CaptureError('Open rear Sensor and bounded timeout required')
    if frame_sink is None:
        output = Path(output)
        raw_path = output.with_suffix('.nv12')
        metadata_path = output.with_suffix('.json')
        if output.suffix != '.ppm' or any(p.exists() for p in (output,raw_path,metadata_path)):
            raise CaptureError('New private .ppm path and absent sidecars required')
        if 'private' not in output.parts or not output.parent.is_dir():
            raise CaptureError('Existing private output directory required')
    streamed = 0
    native = Native()
    image = ion = None
    buffers = []
    csi_attempted = stream_attempted = False
    handled_signals = (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)
    old_handlers = {sig:signal.getsignal(sig) for sig in handled_signals}
    def deadline(*_):
        raise CaptureError('Capture interrupted/deadline expired; attempting ordered cleanup')
    for sig in handled_signals:
        signal.signal(sig,deadline)
    signal.setitimer(signal.ITIMER_REAL,timeout)
    result = None
    try:
        for name,table in (('common-init',init),(f'mode-{W}x{H}',mode)):
            emit(name,register_count=len(table))
            for i,(address,value) in enumerate(table):
                if address == 0xffff:
                    time.sleep(value/1000)
                else:
                    sensor.write(address,value)
                if (i+1) % 512 == 0:
                    emit('register-progress',table=name,completed=i+1)
            time.sleep(.010) # HAL mode dispatch sleeps10ms after table write.
        if frame_sink is not None:
            apply_stock_15fps(sensor, profile)
        emit('open-dcam')
        image = os.open('/dev/sprd_image',os.O_RDWR|os.O_CLOEXEC)
        ion = os.open('/dev/ion',os.O_RDWR|os.O_CLOEXEC)
        csi_attempted = True
        emit('open-csi',phy_id=1,lanes=2,bps_per_lane=344)
        sensor.ioctl(19,CsiIf(0,1,1,2,344))
        interface = SensorIf(1,0,2,0) # CSI2, YUV, sensor descriptor image_pattern2.
        interface.mipi[:] = (0,8,1,2,344)
        native.ioctl(image,'Z',9,interface)
        native.ioctl(image,'Z',0,U32(1)) # Continuous; stop immediately after first frame.
        native.ioctl(image,'Z',2,Size(W,H))
        native.ioctl(image,'Z',3,Rect(0,0,W,H))
        parm = Parm(channel_id=1,pixel_fmt=NV12,camera_id=0,
                    crop_rect=Rect(0,0,W,H),dst_size=Size(W,H))
        native.ioctl(image,'Z',7,parm)
        chosen = native.ioctl(image,'Z',17,U32(0),2)
        if chosen.value != 1:
            raise CaptureError(f'Expected unused path1, got{chosen.value}')
        native.ioctl(image,'Z',5,parm)
        fmt = native.ioctl(image,'Z',19,Format(1,W,H,NV12,0,0,0,0),3)
        if fmt.need_binning or tuple(bytes(fmt.reserved)[:2]) != (1,1):
            raise CaptureError('Unexpected DCAM binning/endian result')
        native.ioctl(image,'Z',4,Parm(channel_id=1,frame_base_id=0x100))
        native.ioctl(image,'Z',11,Parm(channel_id=1,deci=0))
        native.ioctl(image,'Z',1,U32(5)) # Exact sensor descriptor preview skip5.
        length = ((W*H*3//2+4095)//4096)*4096
        emit('allocate-ion',heap_mask=2,master_id=1,buffer_bytes=length,count=4)
        for _ in range(4):
            buffers.append(Buffer(native,ion,length))
        for i,buffer in enumerate(buffers):
            native.ioctl(image,'Z',10,Parm(channel_id=1,is_reserved_buf=int(i==3),
                                          frame_addr=buffer.addresses(),buf_flag=0))
        emit('stream-on')
        stream_attempted = True
        native.ioctl(image,'Z',14,U32(1))
        # Rear stream_on callback is no-op when private capture flag is0; mode
        # table already enables preview. Do not send Capture_Start for preview.
        if frame_sink is not None:
            streamed = stream(native, image, ion, buffers, 1, frame_buffer, frame_sink)
        else:
            for _ in range(8):
                op = ReadOp(1,0xffffffff)
                ret = native.libc.read(image,C.byref(op),C.sizeof(op))
                if ret < 0:
                    raise OSError(C.get_errno(),'DCAM frame descriptor read')
                if ret != 0:
                    raise CaptureError(f'Unexpected vendor descriptor-read result{ret}')
                if op.evt == 4:
                    continue # Bounded retry of queue-busy only; not hardware errors.
                frame,buffer = frame_buffer(op,buffers[:3])
                emit('frame-complete',channel=frame.channel_id,width=frame.width,height=frame.height)
                # No CPU read while DMA runs; stop first, then invalidate+copy below.
                result = (frame,buffer)
                break
            if result is None:
                raise CaptureError('No completed owned frame')
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        try:
            if image is not None and stream_attempted:
                emit('stream-off')
                native.ioctl(image,'Z',15,U32(0))
            if image is not None:
                # Explicit cancel descriptor event before releasing the fd;
                # no separate read thread exists, so no waiter remains here.
                stop = (U32*3)(2,1,0)
                ret = native.libc.write(image,C.byref(stop),C.sizeof(stop))
                if ret not in (0,1):
                    raise CaptureError(f'DCAM cancel returned{ret}')
                os.close(image)
                image = None
            if csi_attempted:
                emit('close-csi')
                sensor.ioctl(19,CsiIf(1,1,1,2,344))
        except BaseException as error:
            park('dcam/csi-stop',error)
        try:
            if result is not None:
                frame,buffer = result
                # INVALIDATE's custom argument is the fd value, NOT a pointer.
                native.ioctl(ion,'I',6,IonCustom(13,buffer.fd),3)
                raw = bytes(buffer.mapping[:W*H*3//2])
                result = {'width':W,'height':H,'fourcc':'NV12',
                          'timestamp':{'sec':frame.sec,'usec':frame.usec},
                          'identity':identity,'raw_sha256':hashlib.sha256(raw).hexdigest(),
                          'raw':raw}
            for buffer in reversed(buffers):
                buffer.close()
            if ion is not None:
                os.close(ion)
        except BaseException as error:
            park('ion-release',error)
        for sig,handler in old_handlers.items():
            signal.signal(sig,handler)
        emit('capture-resources-released')
    if frame_sink is not None:
        return {'frames': streamed}
    raw = result.pop('raw')
    ppm = nv12_ppm(raw)
    result['ppm_sha256'] = hashlib.sha256(ppm).hexdigest()
    for path,data in ((raw_path,raw),(output,ppm),
                      (metadata_path,(json.dumps(result,indent=2)+'\n').encode())):
        fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as file:
            file.write(data)
    emit('image-saved',width=W,height=H,raw_sha256=result['raw_sha256'],ppm_sha256=result['ppm_sha256'])
    return result
