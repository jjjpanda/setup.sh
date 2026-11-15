"""Desktop application and launcher integration for Camera Stream."""

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time

WORKER = Path.home() / '.local/share/sm-t280/camera-stream-worker.py'


class SourceHandler(socketserver.StreamRequestHandler):
    def reply(self, data, flush=False):
        self.wfile.write(json.dumps(data).encode() + b'\n')
        if flush:
            self.wfile.flush()

    def handle(self):
        line = self.rfile.readline(1024)
        if not line:
            return
        request = json.loads(line)
        if 'stop' in request:
            matches = request['stop'] == self.server.last_session
            if matches and self.server.active_stop is not None:
                self.server.active_stop()
            self.reply({'ok': matches})
            return
        if 'status' in request:
            self.reply({'ok': request['status'] == self.server.last_session,
                        'result': self.server.last_result})
            return
        camera = request.get('camera')
        if camera not in ('rear', 'front'):
            self.reply({'ok': False, 'error': 'Choose rear or front'})
            return
        if not self.server.camera_lock.acquire(blocking=False):
            self.reply({'ok': False, 'error': 'Tablet camera already in use'})
            return
        self.server.busy.set()
        self.server.last_session = secrets.token_hex(16)
        self.server.last_result = 1
        self.server.report('Starting ' + camera + ' camera…')
        log_path = Path.home() / '.cache/camera-stream/session.log'
        try:
            command = [sys.executable, '-B', str(WORKER), camera]
            with log_path.open('ab', buffering=0) as log, subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=log, start_new_session=True) as worker:
                self.server.active_stop = worker.stdin.close
                try:
                    self.reply({'ok': True, 'session': self.server.last_session}, flush=True)
                    self.request.settimeout(3)
                    self.server.report('Streaming ' + camera + ' camera')
                    try:
                        for chunk in iter(lambda: worker.stdout.read1(32768), b''):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except OSError:
                        pass
                finally:
                    worker.stdin.close()
                    self.server.preview.drain(worker.stdout)
                    worker.stdout.close()
                    self.server.active_stop = None
                result = worker.wait()
                self.server.last_result = result
                self.server.report('Ready for a viewer' if result == 0 else 'Capture ended with an error. See session.log.')
        finally:
            self.server.busy.clear()
            self.server.camera_lock.release()


class SourceServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, preview, report):
        self.path = Path.home() / '.cache/camera-stream/source.sock'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            with socket.socket(socket.AF_UNIX) as existing:
                try:
                    existing.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise RuntimeError('Camera Stream is already running')
        super().__init__(str(self.path), SourceHandler)
        self.path.chmod(0o600)
        self.preview = preview
        self.report = report
        self.camera_lock = threading.Lock()
        self.busy = threading.Event()
        self.active_stop = None
        self.last_session = None
        self.last_result = None

    def server_close(self):
        super().server_close()
        self.path.unlink(missing_ok=True)


def run(preview, args):
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import GLib, Gtk

    application = Gtk.Application(application_id='org.homelab.CameraStream')
    window = None

    def activate(app):
        nonlocal window
        if window is not None:
            window.present()
            return
        window = Gtk.ApplicationWindow(application=app, title='Camera Stream')
        window.set_default_size(420, 200)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_border_width(20)
        window.add(box)
        title = Gtk.Label(label='Camera Stream', xalign=0)
        title.set_markup('<big><b>Camera Stream</b></big>')
        box.pack_start(title, False, False, 0)
        status = Gtk.Label(label='Ready for a viewer', xalign=0)
        status.set_line_wrap(True)
        box.pack_start(status, True, True, 0)
        stop = Gtk.Button(label='Stop stream')
        box.pack_start(stop, False, False, 0)
        ready_text = 'Ready. Open preview to start the webcam.'

        def report(message):
            GLib.idle_add(status.set_text, message)

        try:
            tablet = WORKER.is_file()
            if tablet:
                service = SourceServer(preview, report)
                instruction = Gtk.Label(label='Open the front or rear preview on your computer.', xalign=0)
                instruction.set_line_wrap(True)
                box.pack_start(instruction, False, False, 0)
            else:
                service = preview.create_server(args)
                url = f'http://127.0.0.1:{service.server_port}/'
                open_button = Gtk.Button(label='Open preview')
                player = (['ffplay', '-hide_banner', '-loglevel', 'error', '-autoexit',
                           '-fflags', 'nobuffer', '-flags', 'low_delay', '-framedrop',
                           '-probesize', '32', '-analyzeduration', '0', '-f', 'mpjpeg',
                           '-window_title', 'Camera Stream', url.rstrip('/') + service.stream_path]
                          if shutil.which('ffplay') else ['xdg-open', url])
                open_button.connect('clicked', lambda *_: subprocess.Popen(player))
                box.pack_start(open_button, False, False, 0)
                if shutil.which('ffplay'):
                    browser_button = Gtk.Button(label='Open in browser')
                    browser_button.connect('clicked', lambda *_: subprocess.Popen(['xdg-open', url]))
                    box.pack_start(browser_button, False, False, 0)
                status.set_text(ready_text)
        except Exception as error:
            status.set_text(str(error))
            stop.set_sensitive(False)
            window.show_all()
            return
        threading.Thread(target=service.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
        closing = False

        def stop_stream(*_):
            if service.active_stop is not None:
                service.active_stop()

        stop.connect('clicked', stop_stream)

        def close(*_):
            nonlocal closing
            if closing:
                return True
            closing = True
            stop.set_sensitive(False)
            status.set_text('Closing camera streaming…')

            def finish():
                stop_stream()
                service.shutdown()
                stop_stream()
                while service.busy.is_set():
                    time.sleep(0.1)
                service.server_close()
                GLib.idle_add(app.quit)

            threading.Thread(target=finish, daemon=True).start()
            return True

        window.connect('delete-event', close)

        def refresh():
            if closing:
                return False
            stop.set_sensitive(service.busy.is_set())
            if not tablet:
                status.set_text('Streaming webcam' if service.busy.is_set() else ready_text)
                open_button.set_sensitive(not service.busy.is_set())
            return True

        GLib.timeout_add(300, refresh)
        window.show_all()

    application.connect('activate', activate)
    return application.run([])
