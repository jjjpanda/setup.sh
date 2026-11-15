#!/usr/bin/env python3
"""Serve an on-demand camera preview over HTTP on localhost."""

import argparse
from contextlib import contextmanager
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
BOUNDARY = 'camera-frame'

PAGE = '''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CAMERA_TITLE</title>
<style>
body { font: 1rem system-ui, sans-serif; color-scheme: dark; background: #141820;
       color: #f2f4f8; margin: 3rem auto; padding: 0 1rem; max-width: 44rem; }
button { font: inherit; padding: .55rem 1rem; margin-right: .5rem; }
img { display: block; width: 100%; aspect-ratio: PREVIEW_ASPECT; background: #080a0e;
      object-fit: contain; margin-top: 1rem; }
</style>
<main>
<h1>CAMERA_TITLE</h1>
<p>CAMERA_DETAILS</p>
<button id="start">Start preview</button><button id="stop" disabled>Stop</button>
CAMERA_CONTROLS
<p id="status" role="status">Camera idle.</p>
<img id="preview" data-stream="STREAM_URL" data-control="CONTROL_URL" alt="Live camera preview">
</main>
<script>
const start = document.getElementById('start');
const stop = document.getElementById('stop');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
const switchCamera = document.getElementById('switch-camera');
let stateTimer;
async function showState() {
  if (!switchCamera || !preview.hasAttribute('src')) return;
  try {
    const response = await fetch(preview.dataset.control);
    const state = await response.json();
    status.textContent = state.error || (state.switching ?
      'Switching to ' + state.camera + '… Holding the last frame.' : 'Live ' + state.camera + ' preview');
  } catch (_) {}
}
if (switchCamera) switchCamera.onclick = async () => {
  switchCamera.disabled = true;
  try {
    const response = await fetch(preview.dataset.control, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera: switchCamera.dataset.next})
    });
    if (!response.ok) throw new Error('Switch failed');
    const state = await response.json();
    switchCamera.dataset.next = state.camera === 'rear' ? 'front' : 'rear';
    switchCamera.textContent = 'Switch to ' + switchCamera.dataset.next;
    status.textContent = 'Selected ' + state.camera + '. Switching may take several seconds.';
  } catch (_) { status.textContent = 'Could not switch camera. Try again.'; }
  finally { switchCamera.disabled = false; }
};
function reset(message) {
  clearInterval(stateTimer);
  preview.removeAttribute('src');
  start.disabled = false;
  stop.disabled = true;
  status.textContent = message;
}
start.onclick = () => {
  start.disabled = true;
  stop.disabled = false;
  status.textContent = 'Starting the camera. The first frame may take several seconds.';
  preview.src = preview.dataset.stream;
  if (switchCamera) stateTimer = setInterval(showState, 1000);
};
stop.onclick = () => reset('Preview stopped.');
preview.onload = () => {
  if (preview.hasAttribute('src') && !switchCamera) status.textContent = 'Live preview';
};
preview.onerror = () => {
  if (preview.hasAttribute('src')) reset('Preview unavailable. Wait a moment, then try again.');
};
</script></html>
'''


def camera_payload(camera='rear'):
    client = HERE / 'camera-source-client.py'
    if not client.exists():
        client = HERE.parent / 'share/camera-preview/source-client.py'
    return f'CAMERA = {camera!r}\n'.encode() + client.read_bytes()


def ssh_command(host, command):
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
            '-o', 'Compression=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '--', host, command]


def payload_loader(payload):
    return f'''import os
payload = bytearray()
while len(payload) < {len(payload)}:
    chunk = os.read(0, {len(payload)} - len(payload))
    if not chunk:
        raise EOFError('Incomplete preview worker')
    payload.extend(chunk)
exec(compile(payload, '<rear-preview>', 'exec'))
'''


def ssh_camera(host, payload):
    return ssh_command(host, 'python3 -B -c ' + shlex.quote(payload_loader(payload)))


def laptop_client(host):
    client = '''import re
import sys
import threading
from urllib.request import urlopen

base = 'http://127.0.0.1:8765'
try:
    with urlopen(base, timeout=5) as page:
        path = re.search(rb'data-stream="([^"]+)"', page.read()).group(1).decode()
    stream = urlopen(base + path, timeout=15)
except OSError as error:
    raise RuntimeError('Open Camera Stream from the laptop dock first.') from error
def stop():
    sys.stdin.buffer.readline()
    stream.close()
threading.Thread(target=stop, daemon=True).start()
try:
    while True:
        chunk = stream.read1(32768)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
except (OSError, ValueError):
    pass
finally:
    stream.close()
'''
    return ssh_command(host, 'python3 -B -c ' + shlex.quote(client))


def drain(stream):
    while stream.read1(32768):
        pass


def ffmpeg_cmd(*args):
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', *args]


def wallclock(rtsp):
    return ['-use_wallclock_as_timestamps', '1'] if rtsp else []


def stream_outputs(rtsp=None, copy=False):
    encoding = ['-c:v', 'copy'] if copy else ['-c:v', 'mjpeg', '-q:v', '5']
    if copy and rtsp:
        encoding += ['-bsf:v', 'setts=pts=N:dts=N']
    outputs = ['-an', *encoding, '-flush_packets', '1', '-f', 'mpjpeg',
               '-boundary_tag', BOUNDARY, 'pipe:1']
    if rtsp:
        outputs += ['-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                    '-pix_fmt', 'yuv420p', '-g', '30', '-f', 'rtsp', '-rtsp_transport', 'tcp', rtsp]
    return outputs


def rtsp_reachable(url):
    address = urlsplit(url)
    try:
        socket.create_connection((address.hostname, address.port or 554), timeout=3).close()
    except OSError:
        return False
    return True


def v4l2_command(args):
    input_format, size = args.input_format, args.size
    if not input_format:
        try:
            formats = subprocess.run(ffmpeg_cmd('-loglevel', 'info', '-f', 'v4l2', '-list_formats', 'all', '-i', args.device),
                                     capture_output=True, text=True, timeout=5).stderr
            for line in formats.splitlines():
                if 'Compressed:' not in line or 'mjpeg' not in line:
                    continue
                sizes = re.findall(r'\b\d+x\d+\b', line)
                if size and size not in sizes:
                    continue
                input_format = 'mjpeg'
                if not size and sizes:
                    size = min(sizes, key=lambda value: abs(int(value.split('x')[0]) * int(value.split('x')[1]) - 640*480))
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
    cmd = ['-f', 'v4l2']
    if size:
        cmd += ['-video_size', size]
    if input_format:
        cmd += ['-input_format', input_format]
    return ffmpeg_cmd(*cmd, '-framerate', str(args.fps), '-i', args.device,
                      *stream_outputs(args.rtsp, copy=input_format == 'mjpeg'))


def relay_command(rtsp):
    return ffmpeg_cmd('-nostdin', '-f', 'mpjpeg', *wallclock(rtsp), '-i', 'pipe:0',
                      *stream_outputs(rtsp, copy=True))


@contextmanager
def pipe_capture(command, encode=None):
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as camera:
        def stop():
            try:
                if encode is None:
                    camera.stdin.write(b'q\n')
                    camera.stdin.flush()
                camera.stdin.close()
            except (OSError, ValueError):
                pass

        try:
            if encode is None:
                yield camera.stdout, stop
            else:
                with subprocess.Popen(encode, stdin=camera.stdout, stdout=subprocess.PIPE) as encoder:
                    camera.stdout.close()
                    try:
                        yield encoder.stdout, stop
                    finally:
                        stop()
                        drain(encoder.stdout)
        finally:
            stop()
            if encode is None:
                drain(camera.stdout)
            camera.stdout.close()


def front_color_filter(gains=(1, 1, 1)):
    def srgb(gain):
        linear = f'clip({gain}*(val-16)/239,0,1)'
        return f'255*if(lte({linear},0.0031308),12.92*{linear},1.055*pow({linear},1/2.4)-0.055)'
    return 'format=rgb24,lutrgb=' + ':'.join(f"{channel}='{srgb(gain)}'" for channel, gain in zip('rgb', gains))


def tablet_converter(sensor, gains):
    pixel_format, size = ('bayer_bggr8', '400x300') if sensor == 'front' else ('nv12', '320x240')
    filters = [front_color_filter(gains)] if sensor == 'front' else []
    rotation = 'cclock' if sensor == 'front' else 'clock'
    filters += ['scale=320:240:flags=area', 'transpose=' + rotation, 'format=yuv420p']
    return ffmpeg_cmd('-nostdin', '-f', 'rawvideo', '-pixel_format', pixel_format,
                      '-video_size', size, '-framerate', '15', '-i', 'pipe:0',
                      '-vf', ','.join(filters), '-an', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1')


class TabletCapture:
    """One publisher survives switches; only one sensor pipeline runs at a time."""

    frame_bytes = 240 * 320 * 3 // 2

    def __init__(self, host, camera, gains, rtsp):
        self.host, self.camera, self.gains, self.rtsp = host, camera, gains, rtsp
        self.stopping = threading.Event()
        self.guard = threading.Lock()
        self.control = None
        self.latest = None
        self.active_camera = None
        self.error = None
        self.generation = 0

    def switch(self, camera):
        with self.guard:
            if camera != self.camera:
                self.generation += 1
            self.camera = camera
            if self.control is not None and camera != self.active_camera:
                self.control.close()

    def stop(self):
        self.stopping.set()
        with self.guard:
            if self.control is not None:
                self.control.close()

    def collect(self):
        try:
            while not self.stopping.is_set():
                with self.guard:
                    sensor = self.camera
                    generation = self.generation
                    self.active_camera = sensor
                payload = camera_payload(sensor)
                with subprocess.Popen(ssh_camera(self.host, payload),
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) as source:
                    with self.guard:
                        source.stdin.write(payload)
                        source.stdin.flush()
                        self.control = source.stdin
                        if self.stopping.is_set() or self.camera != sensor:
                            source.stdin.close()
                    try:
                        with subprocess.Popen(tablet_converter(sensor, self.gains),
                                              stdin=source.stdout, stdout=subprocess.PIPE) as convert:
                            source.stdout.close()
                            while True:
                                frame = convert.stdout.read(self.frame_bytes)
                                if len(frame) < self.frame_bytes:
                                    break
                                with self.guard:
                                    if self.camera == sensor and not self.stopping.is_set():
                                        self.latest = (frame, generation)
                            convert.stdout.close()
                        if convert.returncode:
                            raise RuntimeError('Camera conversion failed')
                    finally:
                        source.stdout.close()
                        with self.guard:
                            source.stdin.close()
                            self.control = None
                if source.returncode:
                    raise RuntimeError('Camera source ended with an error')
                if self.generation == generation and not self.stopping.is_set():
                    break
        except Exception as error:
            self.error = str(error)
            print(self.error, file=sys.stderr, flush=True)
        finally:
            self.stopping.set()

    def pump(self, publisher):
        deadline = time.monotonic()
        try:
            while not self.stopping.is_set():
                with self.guard:
                    latest = self.latest
                if latest is not None:
                    publisher.stdin.write(latest[0])
                    publisher.stdin.flush()
                deadline = max(deadline + 1 / 15, time.monotonic())
                self.stopping.wait(max(0, deadline - time.monotonic()))
        except (OSError, ValueError):
            self.stop()
        finally:
            publisher.stdin.close()

    @contextmanager
    def capture(self):
        command = ffmpeg_cmd('-nostdin', '-f', 'rawvideo', '-pixel_format', 'yuv420p',
                             '-video_size', '240x320', '-framerate', '15',
                             *wallclock(self.rtsp), '-i', 'pipe:0', *stream_outputs(self.rtsp))
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as publisher:
            collector = threading.Thread(target=self.collect)
            pump = threading.Thread(target=self.pump, args=(publisher,))
            collector.start()
            pump.start()
            try:
                yield publisher.stdout, self.stop
            finally:
                self.stop()
                drain(publisher.stdout)
                publisher.stdout.close()
                collector.join()
                pump.join()


class Handler(BaseHTTPRequestHandler):
    def allowed_hosts(self):
        port = self.server.server_port
        return (f'127.0.0.1:{port}', f'localhost:{port}')

    def camera_state(self):
        with self.server.selection_lock:
            camera = self.server.selected_camera
            session = self.server.tablet_capture
            if session is None:
                return {'camera': camera, 'switching': False, 'error': None}
            with session.guard:
                return {'camera': camera,
                        'switching': not session.latest or session.latest[1] != session.generation,
                        'error': session.error}

    def do_POST(self):
        hosts = self.allowed_hosts()
        if (self.headers.get('Host', '').lower() not in hosts
                or self.path != self.server.control_path
                or self.headers.get('Content-Type') != 'application/json'
                or self.headers.get('Origin') not in (None, *(f'http://{host}' for host in hosts))):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 128:
                raise ValueError('Invalid body length')
            camera = json.loads(self.rfile.read(length))['camera']
            if camera not in ('rear', 'front') or not self.server.tablet_host:
                raise ValueError('Choose rear or front')
        except (ValueError, KeyError, TypeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        with self.server.selection_lock:
            self.server.selected_camera = camera
            if self.server.tablet_capture is not None:
                self.server.tablet_capture.switch(camera)
        self.send_body(json.dumps({'camera': camera}).encode(), 'application/json')

    def do_GET(self):
        if self.headers.get('Host', '').lower() not in self.allowed_hosts():
            self.send_error(HTTPStatus.FORBIDDEN)
        elif self.path == '/':
            self.send_body(self.server.page, 'text/html; charset=utf-8')
        elif self.path == '/health':
            self.send_body(b'ok\n', 'text/plain; charset=utf-8')
        elif self.path == self.server.control_path and self.server.tablet_host:
            self.send_body(json.dumps(self.camera_state()).encode(), 'application/json')
        elif self.path == self.server.stream_path:
            if not self.server.camera_lock.acquire(blocking=False):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, 'Camera already in use')
                return
            try:
                self.server.busy.set()
                self.stream()
            finally:
                self.server.active_stop = None
                self.server.busy.clear()
                self.server.camera_lock.release()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def send_body(self, body, content_type):
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.connection.settimeout(5)
        if self.server.rtsp and not rtsp_reachable(self.server.rtsp):
            self.send_error(HTTPStatus.BAD_GATEWAY, 'RTSP server unreachable')
            return
        with self.server.capture() as (frames, stop):
            self.server.active_stop = stop
            first = frames.read1(32768)
            if not first:
                self.send_error(HTTPStatus.BAD_GATEWAY, 'No camera frames received')
                return
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={BOUNDARY}')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(first)
                for chunk in iter(lambda: frames.read1(32768), b''):
                    self.wfile.write(chunk)
            except OSError:
                stop()


def create_server(args):
    rtsp = args.rtsp
    if args.tablet_host:
        gains = args.front_wb or (1, 1, 1)
        title = 'Tablet camera'
        details = '240 × 320 · up to 15 fps · switching holds the last frame'
        if gains != (1, 1, 1):
            details += f' · white balance R {gains[0]:g} B {gains[2]:g}'

        @contextmanager
        def capture():
            with server.selection_lock:
                session = TabletCapture(args.tablet_host, server.selected_camera, gains, rtsp)
                server.tablet_capture = session
            try:
                with session.capture() as output:
                    yield output
            finally:
                with server.selection_lock:
                    server.tablet_capture = None
    else:
        if not args.ssh and sys.platform != 'linux':
            raise RuntimeError('Local V4L2 capture requires Linux. Use --ssh for a remote Linux webcam.')
        if args.ssh:
            command = laptop_client(args.ssh)
            details = 'Settings come from the laptop Camera Stream application'
        else:
            command = None
            details = f'{args.size + " · " if args.size else ""}requested {args.fps} fps'
        title = 'Laptop webcam'

        def capture():
            nonlocal command
            if command is None:
                command = v4l2_command(args)
            return pipe_capture(command, relay_command(rtsp) if args.ssh and rtsp else None)

    if rtsp:
        details += ' · also publishes RTSP while previewing'
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.capture = capture
    server.rtsp = rtsp
    server.stream_path = '/camera.mjpg?token=' + secrets.token_urlsafe(18)
    server.control_path = server.stream_path.replace('/camera.mjpg', '/camera-control')
    server.tablet_host = args.tablet_host
    server.selected_camera = args.camera
    server.selection_lock = threading.Lock()
    server.tablet_capture = None
    other = 'front' if args.camera == 'rear' else 'rear'
    controls = (f'<button id="switch-camera" data-next="{other}">Switch to {other}</button>'
                if args.tablet_host else '')
    page = PAGE
    for old, new in (
        ('STREAM_URL', server.stream_path),
        ('CONTROL_URL', server.control_path),
        ('CAMERA_CONTROLS', controls),
        ('PREVIEW_ASPECT', '3/4' if args.tablet_host else '4/3'),
        ('CAMERA_TITLE', escape(title)),
        ('CAMERA_DETAILS', escape(f'{details} · streams until stopped')),
    ):
        page = page.replace(old, new)
    server.page = page.encode()
    server.camera_lock = threading.Lock()
    server.busy = threading.Event()
    server.active_stop = None
    print(f'{title} preview listening on http://127.0.0.1:{server.server_port}/', flush=True)
    if rtsp:
        print(f'Each preview also publishes to {rtsp}', flush=True)
    return server


def application_module():
    path = HERE / 'camera-stream-app.py'
    if not path.exists():
        path = HERE.parent / 'share/camera-preview/application.py'
    spec = importlib.util.spec_from_file_location('camera_stream_app', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def white_balance(value):
    try:
        red, blue = (float(part) for part in value.split(','))
    except ValueError:
        raise argparse.ArgumentTypeError('use RED,BLUE gains, such as 0.758,0.742') from None
    if not (0.25 <= red <= 4 and 0.25 <= blue <= 4):
        raise argparse.ArgumentTypeError('gains must be between 0.25 and 4')
    return red, 1, blue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--ssh', metavar='HOST', help='read a V4L2 webcam on a Linux SSH host')
    sources.add_argument('--tablet-host', metavar='HOST', help='read an SM-T280 camera over SSH')
    parser.add_argument('--camera', choices=('rear', 'front'), default='rear', help='SM-T280 camera')
    parser.add_argument('--device', default='/dev/video0', help='V4L2 webcam device')
    parser.add_argument('--size', metavar='WIDTHxHEIGHT', help='webcam capture size')
    parser.add_argument('--input-format', help='webcam input format, such as mjpeg')
    parser.add_argument('--fps', type=int, default=15, help='requested webcam frame rate')
    parser.add_argument('--rtsp', metavar='URL', help='also publish each preview to an RTSP server')
    parser.add_argument('--front-wb', metavar='RED,BLUE', type=white_balance,
                        help='front preview white-balance gains relative to green (default 1,1)')
    parser.add_argument('--app', action='store_true', help='open the Camera Stream desktop application')
    args = parser.parse_args()
    if args.app and sys.platform != 'linux':
        parser.error('Desktop application requires Linux.')
    if not 1 <= args.fps <= 60:
        parser.error('--fps must be between 1 and 60')
    if args.rtsp and not args.rtsp.startswith(('rtsp://', 'rtsps://')):
        parser.error('--rtsp must be an rtsp:// or rtsps:// URL')
    if args.front_wb and not args.tablet_host:
        parser.error('--front-wb requires --tablet-host')
    tablet_app = args.app and (Path.home() / '.local/share/sm-t280').is_dir()
    if (args.ssh or args.tablet_host) and not shutil.which('ssh'):
        parser.error('ssh is missing from PATH')
    if not tablet_app and (args.rtsp or not args.ssh) and not shutil.which('ffmpeg'):
        parser.error('ffmpeg is missing from PATH')
    if args.app:
        application_module().run(sys.modules[__name__], args)
        return
    with create_server(args) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
