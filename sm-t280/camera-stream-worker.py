#!/usr/bin/env python3
"""Stream one SM-T280 camera to stdout: rear NV12 320x240, front BGGR8 400x300.

Usage: camera-stream-worker.py rear|front. Closing stdin stops the stream.
"""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tablet_camera_capture as rear
import tablet_camera_front as front

FRONT_WIDTH, FRONT_HEIGHT = 400, 300
FRONT_RAW_BYTES = 1616 * 1212 * 2
_front_native = None


def load_camera():
    spec = importlib.util.spec_from_file_location('tablet_camera', HERE / 'tablet-camera.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rear_nv12(frame):
    return bytes(frame[:320 * 240 * 3 // 2])


def front_bayer(frame):
    global _front_native
    if _front_native is None:
        path = HERE / 'front-bin.so'
        if not path.exists():
            return small_front_bayer(frame)
        _front_native = ctypes.CDLL(str(path))
        _front_native.front_bin.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        _front_native.front_bin.restype = ctypes.c_int
    if len(frame) < FRONT_RAW_BYTES:
        raise ValueError('Incomplete front preview frame')
    view = memoryview(frame)
    # ION mappings are uncached on this kernel. Copy once in a sequential
    # pass; strided halfword reads directly from DMA cost much more.
    source = (ctypes.c_char * FRONT_RAW_BYTES).from_buffer_copy(view[:FRONT_RAW_BYTES])
    output = (ctypes.c_ubyte * (FRONT_WIDTH * FRONT_HEIGHT))()
    if _front_native.front_bin(source, output, len(frame) // 2):
        raise ValueError('Front preview sample exceeds RAW10 range')
    return bytes(output)


def small_front_bayer(frame):
    """Pure-Python equivalent of front-bin.c: bin 16 same-color samples per 8x8 region."""
    if len(frame) < FRONT_RAW_BYTES:
        raise ValueError('Incomplete front preview frame')
    output = bytearray(FRONT_WIDTH * FRONT_HEIGHT)
    invalid_bits = int.from_bytes(b'\x00\xfc' * 1600, 'little')
    for row in range(FRONT_HEIGHT // 2):
        for dy in (0, 1):
            total = 0
            for sy in range(0, 8, 2):
                start = (row * 8 + dy + sy) * 1616 * 2
                packed = int.from_bytes(frame[start:start + 3200], 'little')
                if packed & invalid_bits:
                    raise ValueError('Front preview sample exceeds RAW10 range')
                total += packed
            total = total + (total >> 32) + (total >> 64) + (total >> 96)
            values = (total >> 6).to_bytes(3200, 'little')
            target = (row * 2 + dy) * FRONT_WIDTH
            output[target:target + FRONT_WIDTH:2] = values[0::16]
            output[target + 1:target + FRONT_WIDTH:2] = values[2::16]
    # Average Gr/Gb like the still converter; the sensor has a visible offset.
    for row in range(0, FRONT_HEIGHT, 2):
        for column in range(0, FRONT_WIDTH, 2):
            i = row * FRONT_WIDTH + column
            green = (output[i + 1] + output[i + FRONT_WIDTH] + 1) // 2
            output[i + 1] = output[i + FRONT_WIDTH] = green
    return bytes(output)


class Output:
    """Non-blocking frame sink: drops whole frames when the reader is slow."""

    def __init__(self, fd, convert, max_fps=None):
        self.fd = fd
        self.convert = convert
        self.interval = 1 / max_fps if max_fps else 0
        self.pending = memoryview(b'')
        self.last_write = time.monotonic()
        self.next_frame = 0
        self.sent = self.drops = self.skipped = 0

    def flush(self):
        if self.pending:
            try:
                count = os.write(self.fd, self.pending)
            except BlockingIOError:
                return
            self.pending = self.pending[count:]
            self.last_write = time.monotonic()
            if not self.pending:
                self.sent += 1

    def __call__(self, frame):
        if select.select([0], [], [], 0)[0]:
            return False
        try:
            self.flush()
            if self.pending:
                self.drops += 1
                return time.monotonic() - self.last_write < 2
            now = time.monotonic()
            if now < self.next_frame:
                self.skipped += 1
                return True
            self.next_frame = now + self.interval
            self.pending = memoryview(self.convert(frame))
            self.flush()
            return True
        except BrokenPipeError:
            return False

    def finish(self):
        deadline = time.monotonic() + 1
        while self.pending and time.monotonic() < deadline:
            try:
                if select.select([], [self.fd], [], 0.05)[1]:
                    self.flush()
            except BrokenPipeError:
                break


def stream(camera, output, is_front):
    camera.abi_check()
    for node in ('/dev/sprd_sensor', '/dev/sprd_image', '/dev/ion'):
        camera.require_device(node)
    profiles = HERE / 'private' / 'camera'
    sensor = camera.Sensor(int(is_front))
    with camera.exclusive():
        try:
            with sensor:
                identity = sensor.identify()
                if is_front:
                    result = front.capture_one(sensor, camera, identity,
                                               front.load_profile(profiles / 'front-profile.json'),
                                               timeout=12, initialize_exposure=True, gain=camera.FRONT_GAIN,
                                               frame_sink=output)
                else:
                    result = rear.capture_one(sensor, identity, rear.load_profile(profiles / 'profile.json'),
                                              timeout=16, frame_sink=output)
        finally:
            cleanup = {'errors': sensor.cleanup_errors, 'power_down_ioctl_ok': sensor.power_off_completed,
                       'board_address_restored': sensor.address_restored}
            print(json.dumps({'sensor_cleanup': cleanup}), flush=True)
        if cleanup['errors'] or not cleanup['power_down_ioctl_ok'] or not cleanup['board_address_restored']:
            raise RuntimeError('Sensor cleanup incomplete')
    return result


def main():
    if sys.argv[1:] not in (['rear'], ['front']):
        sys.exit(__doc__)
    is_front = sys.argv[1] == 'front'
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fd = os.dup(1)
    os.dup2(2, 1)
    os.set_blocking(fd, False)
    output = Output(fd, front_bayer if is_front else rear_nv12, max_fps=15 if is_front else None)
    try:
        print(json.dumps({'stream_complete': stream(load_camera(), output, is_front)}), flush=True)
    finally:
        output.finish()
        os.close(fd)
        print(f'Sent {output.sent} frames, dropped {output.drops}, rate-limited {output.skipped}',
              file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
