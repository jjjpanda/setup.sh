#!/usr/bin/env python3
"""SM-T280 sensor IDs and rear/front still capture.

No vendor tables are redistributed. Each capture needs its separate matching
private sensor profile. Streaming is camera-stream-worker.py.
Capture workers request ordered cleanup on interruption; they are never killed
to meet a deadline. A retained DMA owner must be reviewed before another test.
"""
import argparse
import contextlib
import ctypes
import json
import os
from pathlib import Path
import platform
import signal
import struct
import subprocess
import sys
import tempfile
import time


class CameraError(Exception):
    pass


FRONT_GAIN = 8
FRONT_BLACK_LEVEL = 64
FRONT_INITIALIZE_EXPOSURE = True


# Exact stock HAL sensor descriptors and its runtime I2C_ADDR override, rather
# than the initial addresses in the board DTS. A matching live ID is still
# required; selecting this profile does not establish that a sensor responded.
SENSOR_PROFILES = {
    0: {'name': 'rear', 'model': 'S5K4ECGA', 'address': 0x56, 'board_address': 0x3c},
    1: {'name': 'front', 'model': 'SR259', 'address': 0x30, 'board_address': 0x21},
}


def ioc(direction, number, size):
    return (direction << 30) | (size << 16) | (ord('R') << 8) | number


class Register(ctypes.LittleEndianStructure):
    _fields_ = [('address', ctypes.c_uint16), ('value', ctypes.c_uint16),
                ('bits', ctypes.c_uint32)]


class Power(ctypes.LittleEndianStructure):
    _fields_ = [('on', ctypes.c_uint32), ('sensor', ctypes.c_uint32),
                ('unused_sensor_power', ctypes.c_uint32 * 21)]


def abi_check():
    # sensor_drv_k.h and actual sensor_drv_k.c copy lengths, ARM EABI.
    assert ctypes.sizeof(Register) == 8 and Register.bits.offset == 4
    assert ctypes.sizeof(Power) == 92 and Power.unused_sensor_power.offset == 8
    if (sys.platform != 'linux' or sys.byteorder != 'little'
            or struct.calcsize('P') != 4 or not platform.machine().startswith('arm')):
        raise CameraError('Hardware operations require little-endian ARM32 Linux Python.')


def require_device(path):
    if not Path(path).exists():
        raise CameraError(f'{path}: device is missing')
    if not os.access(path, os.R_OK | os.W_OK):
        raise CameraError(f'{path}: read/write permission denied; ask the supervisor '
                          'to apply the device-specific video-group rule')


@contextlib.contextmanager
def exclusive():
    import fcntl
    # Never unlink a lock: waiters and an uninterruptible worker must share its inode.
    directory = Path.home() / '.cache' / 'tablet-camera'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CameraError('Camera busy: another probe/capture/preview owns the lock.') from error
        yield
    finally:
        os.close(fd)


class Sensor:
    def __init__(self, sensor, trace=False):
        if type(sensor) is not int or sensor not in SENSOR_PROFILES:
            raise CameraError('Only the audited rear (0) and front (1) selectors are supported.')
        self.sensor = sensor
        self.profile = SENSOR_PROFILES[sensor]
        self.trace = trace
        self.fd = None
        self.initialized = False
        self.address_attempted = False
        self.power_attempted = False
        self.power_off_completed = False
        self.address_restored = False
        self.cleanup_errors = []

    def phase(self, operation, **details):
        if self.trace:
            print(json.dumps({'camera_step': operation, 'sensor': self.profile['name'],
                              **details}), file=sys.stderr, flush=True)

    def ioctl(self, number, argument, direction=1):
        # libc takes the actual address of the full allocation, never a guessed
        # encoded-size buffer (SENSOR_IO_RST encodes 4 but copies 8; unused here).
        libc = ctypes.CDLL(None, use_errno=True)
        libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        libc.ioctl.restype = ctypes.c_int
        self.phase('ioctl-begin', number=number)
        result = libc.ioctl(self.fd, ioc(direction, number, ctypes.sizeof(argument)),
                            ctypes.byref(argument))
        if result < 0:
            code = ctypes.get_errno()
            if code == 1 and number in (11, 12):
                raise CameraError(f'I2C ioctl {number} failed for register '
                                  f'0x{argument.address:04x}: vendor transfer failure '
                                  'returns -1/EPERM; device-node access alone does '
                                  'not diagnose the bus/power failure')
            raise OSError(code, f'sensor ioctl {number}: {os.strerror(code)}')
        if result:
            raise CameraError(f'sensor ioctl {number} returned {result}, expected zero')
        self.phase('ioctl-complete', number=number)
        return argument

    def scalar(self, number, value, kind=ctypes.c_uint32):
        self.ioctl(number, kind(value))

    def power(self, on):
        # POWER_CFG dispatches to the compiled sensor_power_gte.c implementation;
        # its three sensor_power arguments are unused in the GTEXSWIFI branch.
        # Each GTE on/off function changes selectors internally but finishes at
        # op_sensor_id, equal to the mutex selected on entry. Do not call this
        # with a selector different from the file's current SET_ID selection.
        self.phase('power-on' if on else 'power-off')
        self.ioctl(21, Power(int(on), self.sensor), direction=3)

    def __enter__(self):
        require_device('/dev/sprd_sensor')
        try:
            # Open enables multimedia resources even before any ioctl.
            self.phase('open')
            self.fd = os.open('/dev/sprd_sensor', os.O_RDWR | os.O_CLOEXEC)
            # SET_ID updates sensor_id BEFORE sensor_k_ioctl takes sync_lock.
            # I2C_INIT updates it AFTER taking that lock, but unlock uses the
            # updated index. Calling I2C_INIT to switch 0 -> 1 leaks mutex 0.
            # Every selection change MUST use SET_ID first.
            self.scalar(8, self.sensor)
            self.scalar(6, self.sensor)  # I2C_INIT selects SENSOR_DEV_0/1
            self.initialized = True
            # The stock HAL installs the descriptor's 7-bit address before
            # POWER_CFG/identify. It does not shift this byte or probe a range.
            self.address_attempted = True
            self.phase('install-address', address=hex(self.profile['address']))
            self.scalar(10, self.profile['address'], ctypes.c_uint16)
            self.power_attempted = True
            self.power(True)
            return self
        except BaseException:
            self.close()
            raise

    def write(self, address, value):
        self.ioctl(12, Register(address, value, 3))  # BE16 register and BE16 value

    def read(self, address, bits):
        return self.ioctl(11, Register(address, 0, bits), direction=3).value

    def identify(self):
        if self.sensor == 0:
            self.write(0xfcfc, 0xd000)
            self.write(0x002c, 0x7000)
            self.write(0x002e, 0x01a4)
            chip = self.read(0x0f12, 3)
            result = {'sensor': 'rear', 'model': 'S5K4ECGA/GX', 'chip_id': hex(chip),
                      'expected_id': '0x4ec0', 'expected_version': '0x11', 'match': False}
            if chip != 0x4ec0:
                return result
            self.write(0x002c, 0x7000)
            self.write(0x002e, 0x01a6)
            version = self.read(0x0f12, 3)
            result['version'] = hex(version)
            if version != 0x11:
                return result
            # Exact HAL identify callback at 0x3fa44..0x3fb24 distinguishes
            # the GX and GA mode banks. Product/version alone cannot do this.
            # This bounded diagnostic sequence does not stream or write the
            # vendor's sysfs/cache files; power-off follows in the context exit.
            self.phase('rear-variant')
            for address, value in (
                    (0xfcfc, 0xd000), (0x0028, 0xd000), (0x002a, 0x0012),
                    (0x0f12, 1), (0x002a, 0x007a), (0x0f12, 0),
                    (0x002a, 0xa000), (0x0f12, 4), (0x002a, 0xa002),
                    (0x0f12, 0x000f), (0x002a, 0xa000), (0x0f12, 1)):
                self.write(address, value)
            time.sleep(0.0002)  # Vendor usleep(200), not a guessed mode delay.
            variant = self.read(0xa044, 3)
            bank = {0x0404: 0, 0x1414: 1}.get(variant)
            result.update(variant_id=hex(variant), mode_bank=bank, match=bank is not None,
                          model={0: 'S5K4ECGX', 1: 'S5K4ECGA'}.get(bank, 'unrecognized'))
            return result
        chip, version = self.read(0x04, 0), self.read(0x05, 0)
        return {'sensor': 'front', 'model': 'SR259', 'chip_id': hex(chip),
                'version': hex(version), 'expected_id': '0xe1', 'match': chip == 0xe1,
                'revision_kind': 'new' if version == 3 else 'old (vendor classification)'}

    def close(self):
        if self.fd is None:
            return
        try:
            if self.power_attempted:
                try:
                    self.power(False)
                    self.power_off_completed = True
                except (OSError, CameraError) as error:
                    self.cleanup_errors.append(str(error))
            if self.address_attempted:
                try:
                    self.phase('restore-board-address', address=hex(self.profile['board_address']))
                    self.scalar(10, self.profile['board_address'], ctypes.c_uint16)
                    self.address_restored = True
                except (OSError, CameraError) as error:
                    self.cleanup_errors.append(str(error))
            if self.initialized:
                try:
                    self.scalar(7, self.sensor)
                except (OSError, CameraError) as error:
                    self.cleanup_errors.append(str(error))
        finally:
            self.phase('close')
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *_):
        self.close()


def probe(indices=(0, 1), trace=False):
    abi_check()
    results = []
    with exclusive():
        for index in indices:
            sensor = Sensor(index, trace=trace)
            try:
                with sensor:
                    result = sensor.identify()
            except (OSError, CameraError) as error:
                result = {'sensor': 'rear' if index == 0 else 'front',
                          'match': False, 'error': str(error)}
            result['i2c_address'] = hex(sensor.profile['address'])
            # Completion is logical driver evidence, not a physical rail check:
            # the compiled GTE helper ignores individual regulator errors.
            result['power_down_ioctl_ok'] = sensor.power_off_completed
            result['board_address_restored'] = sensor.address_restored
            result['cleanup_errors'] = sensor.cleanup_errors
            results.append(result)
            if not result['match'] or sensor.cleanup_errors:
                break  # Diagnose any failed ID/cleanup before another activation.
    print(json.dumps({'operation': 'chip-id-probe', 'sensors': results}), flush=True)
    return 0 if len(results) == len(indices) and all(
        r['match'] and r['power_down_ioctl_ok'] and r['board_address_restored']
        and not r['cleanup_errors'] for r in results) else 1


def capture_preflight(output, front=False, size=(320, 240)):
    if output.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.ppm'):
        raise CameraError('Output must use .jpg, .jpeg, .png or .ppm')
    if os.path.lexists(output) or not output.parent.is_dir():
        raise CameraError('Output must be a new file in an existing directory')
    for path in ('/dev/sprd_sensor', '/dev/sprd_image', '/dev/ion'):
        require_device(path)
    try:
        import tablet_camera_capture as backend
        root = Path(__file__).resolve().parent / 'private' / 'camera'
        if front:
            import tablet_camera_front
            profile = tablet_camera_front.load_profile(root / 'front-profile.json')
        else:
            backend.select_mode(*size)
            profile = backend.load_profile(root / 'profile.json')
    except (ImportError, OSError, ValueError) as error:
        raise CameraError('Capture needs the installed backend and matching private '
                          'private/camera profile: ' + str(error)) from error
    except backend.CaptureError as error:
        raise CameraError(str(error)) from error
    return backend, root, profile


def capture_sensor_frame(backend, profile, directory, front, timeout, gain,
                         exposure_lines=1296, raw_name='frame.raw16'):
    """A fresh powered identity and fully checked cleanup for every still."""
    sensor = Sensor(int(front))
    try:
        with sensor:
            identity = sensor.identify()
            if front:
                import tablet_camera_front
                result = tablet_camera_front.capture_one(
                    sensor, sys.modules[__name__], identity, profile, directory / raw_name,
                    timeout, FRONT_INITIALIZE_EXPOSURE, gain, exposure_lines)
            else:
                result = backend.capture_one(sensor, identity, profile,
                                             directory / 'frame.ppm', timeout)
    finally:
        backend.emit('sensor-cleanup', errors=sensor.cleanup_errors,
                     power_down_ioctl_ok=sensor.power_off_completed,
                     board_address_restored=sensor.address_restored)
    if (sensor.cleanup_errors or not sensor.power_off_completed or not sensor.address_restored):
        raise CameraError('Sensor cleanup incomplete: ' + '; '.join(sensor.cleanup_errors))
    return result


def capture_worker(output, timeout, directory, front=False, gain=FRONT_GAIN, white_balance='auto',
                   size=(320, 240),
                   *, auto_exposure=False):
    backend, _, profile = capture_preflight(output, front, size)
    abi_check()
    if directory.is_symlink():
        raise CameraError('Capture work directory cannot be a symlink')
    directory = directory.resolve()
    private = (Path(__file__).resolve().parent / 'private' / 'camera').resolve()
    if directory.parent != private or not directory.is_dir():
        raise CameraError('Capture worker requires its supervisor-owned private directory')
    def interrupted(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        exposure_report = None
        raw_path = directory / 'frame.raw16'
        with exclusive():
            if auto_exposure:
                if not front:
                    raise CameraError('Automatic exposure is available only for front stills')
                import tablet_camera_ae
                def acquire(exposure_lines, trial_gain, attempt):
                    name = 'ae-%02d.raw16' % attempt
                    result = capture_sensor_frame(backend, profile, directory, True, timeout,
                                                  trial_gain, exposure_lines, name)
                    return {'raw': (directory / name).read_bytes(), 'width': result['width'],
                            'height': result['height'], 'result': result, 'path': directory / name}
                frame, exposure_report = tablet_camera_ae.run(acquire, gain)
                result, raw_path = frame['result'], frame['path']
                backend.emit('front-auto-exposure', report=exposure_report)
                report_path = directory / 'automatic-exposure.json'
                fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(exposure_report, stream, indent=2)
            else:
                result = capture_sensor_frame(backend, profile, directory, front, timeout, gain)
        # Conversion, publication and image encoding happen after DMA and sensor cleanup.
        if front:
            import tablet_camera_raw
            started = time.monotonic()
            ppm, conversion = tablet_camera_raw.raw10_bggr_to_ppm(
                raw_path.read_bytes(), result['width'], result['height'],
                black_level=FRONT_BLACK_LEVEL, white_balance=white_balance == 'auto')
            backend.emit('front-conversion', seconds=round(time.monotonic() - started, 3),
                         report=conversion)
            fd = os.open(directory / 'frame.ppm', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(ppm)
        published = backend.publish_image(directory / 'frame.ppm', output)
        if front:
            # Keep bounded AE trial RAW files as private diagnostic evidence.
            for name in (('frame.ppm',) if auto_exposure else ('frame.raw16', 'frame.ppm')):
                (directory / name).unlink()
        print(json.dumps({'capture_complete': True, 'sensor': 'front' if front else 'rear',
                          'width': result['width'], 'height': result['height'],
                          'cleanup_errors': [], 'auto_exposure': exposure_report, **published}), flush=True)
        return 0
    except backend.CaptureError as error:
        raise CameraError(str(error)) from error


def supervised_capture(output, timeout, front=False, gain=FRONT_GAIN, white_balance='auto',
                       size=(320, 240), *, auto_exposure=False):
    _, root, _ = capture_preflight(output, front, size)
    directory = Path(tempfile.mkdtemp(prefix='capture-', dir=root))
    log_path = directory / 'worker.log'
    child = None
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with log_path.open('xb') as log:
            log_path.chmod(0o600)
            child = subprocess.Popen([
                sys.executable, '-B', str(Path(__file__).resolve()), '--capture',
                *(('--front', '--gain', str(gain), '--white-balance', white_balance) if front
                  else ('--rear', '--size', '%dx%d' % size)),
                *(('--auto-exposure' if auto_exposure else '--no-auto-exposure',) if front else ()),
                '-o', str(output), '--timeout', str(timeout), '--worker',
                '--work-dir', str(directory)], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                status = child.wait(timeout=timeout * (3 if auto_exposure else 1) + (60 if front else 20))
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                child.terminate()
                try:
                    status = child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(json.dumps({'capture_complete': False, 'worker_retained': child.pid,
                                      'private_log': str(log_path), 'sigkill_requested': False,
                                      'error': 'Camera remains owned; do not retry or kill the worker. '
                                               'Review its log and retained resources.'}), flush=True)
                    return 124
        print(log_path.read_bytes()[-65536:].decode('utf-8', errors='replace'), end='', flush=True)
        return status
    finally:
        # Keep the phase log and raw evidence. Do not unlink the camera lock or
        # free resources belonging to a surviving worker.
        signal.signal(signal.SIGTERM, previous)


def supervised_probe(timeout, indices=(0, 1), trace=False):
    def interrupted(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    child = None
    # A stuck worker must not retain the caller's SSH/GUI stdout pipe, otherwise
    # waiting for EOF can defeat the supervisor deadline even after its exit.
    log = tempfile.TemporaryFile()
    def copy_log():
        # pread does not move the file offset shared with a surviving worker.
        # The fallback is only for offline tests on hosts without POSIX pread.
        if hasattr(os, 'pread'):
            data = os.pread(log.fileno(), 65536, 0)
        else:
            log.seek(0)
            data = log.read(65536)
        print(data.decode('utf-8', errors='replace'), end='', flush=True)
    try:
        command = [sys.executable, str(Path(__file__).resolve()), '--probe', '--worker']
        if indices == (0,):
            command.append('--rear')
        elif indices == (1,):
            command.append('--front')
        if trace:
            command.append('--trace')
        child = subprocess.Popen(command, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        result = child.wait(timeout=timeout)
        copy_log()
        return result
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if child is None:
            return 130
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # Include the last flushed ioctl boundary even if the worker is stuck.
        # The caller can retain this bounded journal in its private test log.
        copy_log()
        print(f'Camera probe worker {child.pid} interrupted/timed out. Termination was requested; an '
              'uninterruptible vendor kernel wait can retain power/resources and '
              'the lock. Do not retry until the worker exits and cleanup is reviewed.', file=sys.stderr)
        return 124
    finally:
        log.close()
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--probe', action='store_true', help='Read chip IDs; --rear/--front bounds the test to one camera')
    action.add_argument('--capture', action='store_true', help='Rear 320x240 (or --size) or front 808x606 still; needs the matching private profile')
    sensor = parser.add_mutually_exclusive_group()
    sensor.add_argument('--rear', action='store_true')
    sensor.add_argument('--front', action='store_true')
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--size', choices=('320x240', '640x480', '1280x720'),
                        help='Rear preview mode (default 320x240)')
    parser.add_argument('--gain', type=int, choices=(1, 2, 4, 8), help='Front analog gain')
    parser.add_argument('--white-balance', choices=('auto', 'off'),
                        help='Front software gray-world color balance (default auto; off preserves sensor color)')
    parser.add_argument('--auto-exposure', action=argparse.BooleanOptionalAction, default=None,
                        help='Front stills: bounded exposure search (default on; --no-auto-exposure for manual gain)')
    parser.add_argument('--timeout', type=float, default=15, help='Probe worker deadline in seconds')
    parser.add_argument('--trace', action='store_true', help='Flush probe operation boundaries to the worker log')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--work-dir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error('timeout must be 1..120 seconds')
    if args.capture and (not args.output or not (args.rear or args.front)):
        parser.error('--capture requires --rear or --front and -o FILE')
    if args.probe and args.output:
        parser.error('--probe does not accept -o')
    if args.size and not (args.capture and args.rear):
        parser.error('--size requires --capture --rear')
    if args.gain is not None and not (args.capture and args.front):
        parser.error('--gain requires --capture --front')
    if args.white_balance is not None and not (args.capture and args.front):
        parser.error('--white-balance requires --capture --front')
    if args.auto_exposure is not None and not (args.capture and args.front):
        parser.error('--auto-exposure requires --capture --front')
    if args.trace and not args.probe:
        parser.error('--trace requires --probe')
    if args.output and not args.capture:
        parser.error('-o is only valid with --capture')
    if args.work_dir and not (args.capture and args.worker):
        parser.error('--work-dir is only valid for a capture worker')
    if args.capture and args.worker and not args.work_dir:
        parser.error('capture workers require --work-dir')
    try:
        if args.probe:
            indices = (0,) if args.rear else (1,) if args.front else (0, 1)
            if args.worker:
                signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                return probe(indices, trace=args.trace)
            return supervised_probe(args.timeout, indices, trace=args.trace)
        if args.capture:
            output = args.output.absolute()
            options = (args.front, FRONT_GAIN if args.gain is None else args.gain,
                       args.white_balance or 'auto')
            size = tuple(map(int, (args.size or '320x240').split('x')))
            if args.worker:
                return capture_worker(output, args.timeout, args.work_dir, *options, size=size,
                                      auto_exposure=args.front and args.auto_exposure is not False)
            return supervised_capture(output, args.timeout, *options, size=size,
                                      auto_exposure=args.front and args.auto_exposure is not False)
    except (CameraError, OSError, ValueError) as error:
        print(f'Camera: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
