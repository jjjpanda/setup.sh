#!/usr/bin/env python3
"""Touch launcher and read-only device telemetry for the tablet."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from tablet_session import desktop_environment
os.environ.update(desktop_environment())
os.environ['NO_AT_BRIDGE'] = '1'

# Panel launchers invoke this file directly. Forward warm opens before the
# expensive GTK import, preserving the ordinary first-launch path below.
if __name__ == '__main__' and sys.argv[1:] in ([], ['--dashboard']):
    action = 'show-device' if sys.argv[1:] else 'show-apps'
    try:
        if subprocess.run(['gapplication', 'action', 'local.tablet.Hub', action],
                          capture_output=True, timeout=2).returncode == 0:
            raise SystemExit(0)
    except (OSError, subprocess.TimeoutExpired):
        pass

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gio, GLib, Gdk, GdkPixbuf, Pango

def read(path, default='Unavailable'):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


MDNIE = Path('/sys/class/mdnie/mdnie')
EBOOK = '9'
ACCEL = '/sys/class/sensors/accelerometer_sensor/raw_data'


def button(label, callback, icon=None):
    widget = Gtk.Button()
    box = Gtk.Box(spacing=10)
    if icon:
        box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
    box.pack_start(Gtk.Label(label=label), True, True, 0)
    widget.add(box)
    widget.connect('clicked', callback)
    return widget


class Hub(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='local.tablet.Hub', flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        for option in ('dashboard',):
            self.add_main_option(option, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, 'Open ' + option, None)
        self.window = None
        self.timer = None
        self.apps = []
        self.testing = False
        self.warm_on = False
        self.camera_dialog = None
        self.camera_process = None
        self.camera_generation = 0
        self.refresh_pending = False
        self.closing = False
        self.first_draw = True
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.safe_quit)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        for action_name, page in (('show-apps', 'apps'), ('show-device', 'device')):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect('activate', lambda _action, _parameter, selected=page: self.show_page(selected))
            self.add_action(action)

    def show_page(self, page):
        self.activate()
        self.stack.set_visible_child_name(page)
        self.window.show_all()
        self.window.present()

    def safe_quit(self):
        self.stop_camera()
        self.quit()
        return False

    def do_shutdown(self):
        self.closing = True
        self.stop_camera()
        self.sensors('0')
        Gtk.Application.do_shutdown(self)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        page = 'device' if options.contains('dashboard') else 'apps'
        self.show_page(page)
        return 0

    def do_activate(self):
        if self.window is not None:
            self.window.show_all()
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title='Tablet')
        self.window.set_icon_name('view-app-grid-symbolic')
        self.window.set_skip_taskbar_hint(True)
        # Fit both the 800px portrait panel and landscape. Maximization uses
        # the work area; a landscape-sized default can force portrait overflow.
        self.window.set_default_size(720, 720)
        self.window.set_decorated(False)
        self.window.maximize()
        self.window.connect('delete-event', self.hide)
        self.window.connect('key-press-event', self.keypress)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_border_width(20)
        self.window.add(root)
        toolbar = Gtk.Box(spacing=12)
        title = Gtk.Label()
        title.set_markup('<b>Tablet</b>')
        toolbar.pack_start(title, False, False, 8)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        toolbar.pack_start(switcher, True, False, 0)
        toolbar.pack_end(button('Close', self.hide, 'window-close-symbolic'), False, False, 0)
        toolbar.pack_end(button('Keyboard', lambda _: self.keyboard(), 'input-keyboard-symbolic'), False, False, 0)
        root.pack_start(toolbar, False, False, 0)
        root.pack_start(self.stack, True, True, 0)
        self.apps_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_titled(self.apps_container, 'apps', 'Apps')
        self.stack.add_titled(self.device_page(), 'device', 'Device')
        self.stack.connect('notify::visible-child-name', lambda *_: self.refresh())
        self.window.connect_after('draw', self.after_first_draw)
        self.window.show_all()
        self.timer = GLib.timeout_add_seconds(5, self.refresh)

    def after_first_draw(self, *_):
        if self.first_draw:
            self.first_draw = False
            GLib.idle_add(self.populate_apps)
            self.refresh()
        return False

    def hide(self, *_):
        self.quit()
        return True

    def keypress(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            return self.hide()
        return False

    def keyboard(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync('org.onboard.Onboard', '/org/onboard/Onboard/Keyboard',
                          'org.onboard.Onboard.Keyboard', 'ToggleVisible', None, None, 0, 2000, None)
        except GLib.Error:
            subprocess.Popen(['onboard'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def apps_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        search = Gtk.SearchEntry(placeholder_text='Search apps')
        search.connect('search-changed', lambda entry: self.filter_apps(entry.get_text()))
        box.pack_start(search, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_column_spacing(14)
        self.flow.set_row_spacing(14)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(4)
        self.flow.set_homogeneous(True)
        self.flow.set_valign(Gtk.Align.START)
        scroller.add(self.flow)
        box.pack_start(scroller, True, True, 0)
        return box

    def populate_apps(self):
        # Scan once per open; keep the list while this window is alive. Hidden
        # app tiles must not delay the Device page's first frame.
        if self.closing:
            return False
        self.apps_container.pack_start(self.apps_page(), True, True, 0)
        self.apps_container.show_all()
        entries = [app for app in Gio.AppInfo.get_all() if app.should_show() and app.get_id() != 'tablet-hub.desktop']
        def order(app):
            name = app.get_display_name().lower()
            rank = 0 if name == 'file manager' else 1 if name == 'terminal emulator' else 2 if name == 'settings manager' else 3
            return rank, name
        self.app_entries = iter(sorted(entries, key=order))
        GLib.idle_add(self.add_app_batch)
        return False

    def add_app_batch(self):
        if self.closing:
            return False
        for _ in range(2):
            app = next(self.app_entries, None)
            if app is None:
                self.flow.show_all()
                return False
            name = app.get_display_name()
            tile = Gtk.Button()
            tile.get_style_context().add_class('app-tile')
            tile.set_size_request(170, 148)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            icon = app.get_icon() or Gio.ThemedIcon.new('application-x-executable')
            app_id = (app.get_id() or '').lower()
            for fragment, symbol in [('thunar', 'folder-symbolic'), ('terminal', 'utilities-terminal-symbolic'),
                                      ('onboard', 'input-keyboard-symbolic'), ('keyboard', 'input-keyboard-symbolic'),
                                      ('blueman', 'bluetooth-symbolic'), ('appfinder', 'system-search-symbolic'),
                                      ('pavucontrol', 'audio-volume-high-symbolic')]:
                if fragment in app_id:
                    icon = Gio.ThemedIcon.new(symbol)
                    break
            picture = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.DIALOG)
            picture.set_pixel_size(44)
            # Gtk.Image otherwise resolves every themed icon together on the
            # first Apps draw. Decode here, spread across the deferred batches.
            # If Apps is already visible, let GTK resolve its icons in the one
            # final draw; predecoding them too would delay the complete grid.
            info = (Gtk.IconTheme.get_default().lookup_by_gicon(icon, 44, Gtk.IconLookupFlags.FORCE_SIZE)
                    if self.stack.get_visible_child_name() != 'apps' else None)
            if info is not None:
                try:
                    pixbuf, _ = info.load_symbolic_for_context(self.window.get_style_context())
                    picture.set_from_pixbuf(pixbuf)
                except GLib.Error:
                    pass
            label = Gtk.Label(label=name)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(23)
            label.set_line_wrap(True)
            label.set_lines(2)
            label.set_justify(Gtk.Justification.CENTER)
            content.pack_start(picture, True, True, 0)
            content.pack_start(label, False, False, 0)
            tile.add(content)
            tile.connect('clicked', lambda _, selected=app: self.launch(selected))
            child = Gtk.FlowBoxChild()
            child.search_text = (name + ' ' + (app.get_description() or '')).casefold()
            child.add(tile)
            self.flow.add(child)
            self.apps.append(child)
        return True

    def filter_apps(self, text):
        self.flow.set_filter_func(lambda child: text.casefold() in child.search_text)

    def launch(self, app):
        try:
            app.launch([], Gdk.Display.get_default().get_app_launch_context())
            self.hide()
        except GLib.Error as error:
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                       text='Could not open this app')
            dialog.format_secondary_text(str(error))
            dialog.run()
            dialog.destroy()

    def device_page(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(8)
        scroll.add(box)
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                              min_children_per_line=1, max_children_per_line=4,
                              column_spacing=12, row_spacing=12)
        self.night = button('Night light', lambda _: self.toggle_night(), 'night-light-symbolic')
        self.test = button('Test speaker + mic', lambda _: self.audio_test(), 'audio-input-microphone-symbolic')
        widgets = [self.night, self.test,
                   button('Camera test / preview', lambda _: self.camera_preview(),
                          'camera-photo-symbolic'),
                   button('Display & brightness', lambda _: subprocess.Popen(['xfce4-power-manager', '-c']),
                          'display-brightness-symbolic')]
        if shutil.which('pavucontrol'):
            widgets.append(button('Sound settings', lambda _: subprocess.Popen(['pavucontrol']),
                                  'audio-volume-high-symbolic'))
        for widget in widgets:
            widget.set_size_request(-1, 72)
            actions.add(widget)
        box.pack_start(actions, False, False, 0)
        self.values = {}
        grid = Gtk.Grid(column_spacing=36, row_spacing=14)
        fields = ['Battery', 'Voltage', 'Temperature', 'Audio route', 'Microphone', 'Accelerometer',
                  'Grip sensor', 'Cameras', 'Night light', 'Wi-Fi', 'Bluetooth', 'USB cable',
                  'Clock', 'Free storage']
        for i, name in enumerate(fields):
            grid.attach(Gtk.Label(label=name, xalign=0), 0, i, 1, 1)
            value = Gtk.Label(label='—', xalign=0)
            value.set_selectable(True)
            value.set_line_wrap(True)
            value.set_max_width_chars(28)
            grid.attach(value, 1, i, 1, 1)
            self.values[name] = value
        box.pack_start(grid, False, False, 0)
        self.warm = Gtk.CssProvider()
        self.warm.load_from_data(b'window, .background { background-color: #1d1408; color: #f3d9b0; }'
                                 b' button { background-color: #3a2a14; color: #f3d9b0; }')
        return scroll

    def stop_camera(self):
        self.camera_generation += 1
        child = self.camera_process
        if child is not None and child.poll() is None:
            child.terminate()
        # The background waiter requests cleanup and reaps it without SIGKILL.
        # Keep its reference until reaped so Stop cannot permit a second owner.

    def camera_preview(self):
        import tempfile
        import threading
        if self.camera_dialog is not None:
            self.camera_dialog.present()
            return
        dialog = Gtk.Dialog(title='Camera test / preview', transient_for=self.window)
        self.camera_dialog = dialog
        dialog.set_default_size(560, 620)
        dialog.add_button('Close', Gtk.ResponseType.CLOSE)
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_border_width(14)
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                               min_children_per_line=1, max_children_per_line=4,
                               column_spacing=12, row_spacing=12)
        choice = Gtk.ComboBoxText()
        choice.append('rear', 'Rear · S5K4ECGA')
        choice.append('front', 'Front · SR259')
        choice.set_active_id('rear')
        controls.add(choice)
        status = Gtk.Label(label='Still preview is available for both cameras.',
                           xalign=0)
        status.set_line_wrap(True)
        status.set_selectable(True)
        picture = Gtk.Image()

        def run_camera(probe=False):
            if self.camera_process is not None:
                status.set_text('Camera operation still stopping; wait for it to exit.')
                return
            self.camera_generation += 1
            generation = self.camera_generation
            status.set_text('Reading chip IDs…' if probe else 'Capturing optical preview…')
            picture.clear()
            selected = choice.get_active_id()
            script = Path(__file__).with_name('tablet-camera.py')
            workspace = tempfile.TemporaryDirectory(prefix='tablet-camera-preview-')
            output = Path(workspace.name) / 'frame.jpg'
            command = [sys.executable, str(script), '--probe'] if probe else [
                sys.executable, str(script), '--capture', '--' + selected, '-o', str(output)]
            try:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
            except OSError as error:
                workspace.cleanup()
                status.set_text(str(error))
                return
            self.camera_process = child

            def wait_camera():
                frame = None
                try:
                    log, _ = child.communicate(timeout=120 if selected == 'front' else 45)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        log, _ = child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        # DMA teardown may be uncertain. Never kill its owner;
                        # retain the reference and reap in this daemon thread.
                        log, _ = child.communicate()
                    log += b'\nTimed out; kernel cleanup cannot be guaranteed.'
                if child.returncode == 0 and not probe and output.is_file():
                    if output.stat().st_size <= 10 * 1024 * 1024:
                        frame = output.read_bytes()
                message = log[-8192:].decode('utf-8', errors='replace').strip()
                workspace.cleanup()

                def finished():
                    if self.camera_process is child:
                        self.camera_process = None
                    if self.camera_dialog is not dialog or generation != self.camera_generation:
                        return False
                    if frame:
                        try:
                            loader = GdkPixbuf.PixbufLoader.new_with_type('jpeg')
                            loader.set_size(480, 360)
                            loader.write(frame)
                            loader.close()
                            pixbuf = loader.get_pixbuf()
                            if pixbuf is not None:
                                rotation = (GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE if selected == 'front'
                                            else GdkPixbuf.PixbufRotation.CLOCKWISE)
                                pixbuf = pixbuf.rotate_simple(rotation)
                            picture.set_from_pixbuf(pixbuf)
                            status.set_text('Captured optical frame · ' + selected)
                        except GLib.Error as error:
                            status.set_text('Captured file could not be decoded: ' + str(error))
                    else:
                        status.set_text(message or 'Camera returned no image.')
                    return False
                GLib.idle_add(finished)

            threading.Thread(target=wait_camera, daemon=True).start()

        controls.add(button('Test chip IDs', lambda _: run_camera(True)))
        controls.add(button('Preview frame', lambda _: run_camera()))
        controls.add(button('Stop', lambda _: self.stop_camera()))
        content.pack_start(controls, False, False, 0)
        content.pack_start(status, False, False, 0)
        content.pack_start(picture, True, True, 0)

        def close(*_):
            self.stop_camera()
            self.camera_dialog = None
            dialog.destroy()
        dialog.connect('response', close)
        dialog.show_all()

    def label(self, widget, text):
        widget.get_child().get_children()[-1].set_text(text)

    def night_on(self):
        return self.warm_on or read(MDNIE / 'scenario').split(':')[-1].strip() == EBOOK

    def toggle_night(self):
        on = not self.night_on()
        try:
            (MDNIE / 'scenario').write_text(EBOOK if on else '0')
        except OSError:
            pass
        screen = Gdk.Screen.get_default()
        if on:
            Gtk.StyleContext.add_provider_for_screen(screen, self.warm, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        else:
            Gtk.StyleContext.remove_provider_for_screen(screen, self.warm)
        self.warm_on = on
        self.refresh()

    def audio_test(self):
        if self.testing:
            return
        self.testing = True
        self.label(self.test, 'Recording 3 s… speak now')
        clip = f'/run/user/{os.getuid()}/tablet-mic-test.raw'
        child = subprocess.Popen(['sh', '-c', f'timeout 3 parec -d tablet_mic --raw > {clip}; '
                                  f'pacat -d tablet_speaker --raw {clip}; rm -f {clip}'])
        def done(*_):
            self.testing = False
            self.label(self.test, 'Test speaker + mic')
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, child.pid, done)

    def sensors(self, on):
        for node in Path('/sys/class/input').glob('input*'):
            if read(node / 'name') in ('accelerometer_sensor', 'grip_sensor_wifi'):
                try:
                    if read(node / 'enable') != on:
                        (node / 'enable').write_text(on)
                except OSError:
                    pass

    def refresh(self):
        if (self.first_draw or self.closing or self.refresh_pending or self.window is None
                or not self.window.get_visible() or self.stack.get_visible_child_name() != 'device'):
            return True
        self.sensors('1')
        self.refresh_pending = True
        import threading

        def collect():
            try:
                data, night = self.device_data()
            except Exception as error:
                print('Device refresh: ' + str(error), file=sys.stderr)
                data, night = {}, None

            def apply():
                self.refresh_pending = False
                if self.closing or self.stack.get_visible_child_name() != 'device':
                    return False
                for key, value in data.items():
                    self.values[key].set_text(value)
                if night is not None:
                    self.label(self.night, 'Night light: ' + night.split()[0])
                return False
            GLib.idle_add(apply)

        threading.Thread(target=collect, daemon=True).start()
        return True

    def device_data(self):
        battery = Path('/sys/class/power_supply/battery')
        def scaled(path, divisor, suffix):
            try:
                return f'{float(read(path)) / divisor:.1f}{suffix}'
            except ValueError:
                return 'Unavailable'
        try:
            axes = dict(zip('XYZ', (int(v) for v in read(ACCEL).split(','))))
            name = max(axes, key=lambda k: abs(axes[k]))
            pose = ('Flat' if name == 'Z' else 'Upright') + f" ({name}{'+' if axes[name] > 0 else '−'})"
            motion = ', '.join(map(str, axes.values())) + ' — ' + (pose if any(axes.values()) else 'no data (sensor off)')
        except ValueError:
            motion = 'Unavailable'
        grip = read('/sys/class/sensors/grip_sensor_wifi/raw_data')
        try:
            sources = subprocess.run(['pactl', 'list', 'short', 'sources'], capture_output=True,
                                     text=True, timeout=1).stdout
        except (OSError, subprocess.TimeoutExpired):
            sources = ''
        sensor = '/dev/sprd_sensor'
        cams = (f"{sensor} {'read/write access' if os.access(sensor, os.R_OK | os.W_OK) else 'unavailable or permission denied'}; "
                'rear and front still capture available')
        night = 'On' if self.night_on() else 'Off'
        night += (' (mDNIe eBook + warm theme)' if os.access(MDNIE / 'scenario', os.W_OK)
                  else ' (warm theme only; mDNIe needs root setup)')
        nets = [p.name for p in Path('/sys/class/net').glob('wl*')]
        fs = os.statvfs('/')
        data = {
            'Battery': read(battery / 'capacity') + '% · ' + read(battery / 'status'),
            'Voltage': scaled(battery / 'voltage_now', 1000000, ' V'),
            'Temperature': scaled(battery / 'temp', 10, ' °C'),
            'Audio route': {'0': 'Speaker + built-in mic', '1': 'Headset (headphones + headset mic)',
                            '2': 'Headphones + built-in mic'}.get(read('/sys/class/switch/h2w/state'), 'Unavailable'),
            'Microphone': 'tablet_mic ready' if '\ttablet_mic\t' in sources else 'tablet_mic missing',
            'Accelerometer': motion,
            'Grip sensor': grip + (' (raw)' if grip != 'Unavailable' else ''),
            'Cameras': cams,
            'Night light': night,
            'Wi-Fi': ', '.join(nets) if nets else 'Unavailable',
            'Bluetooth': 'Controller available' if list(Path('/sys/class/bluetooth').glob('hci*')) else 'Unavailable',
            'USB cable': {'0': 'Disconnected', '1': 'Connected'}.get(read('/sys/class/power_supply/usb/online'), 'Unavailable'),
            'Clock': GLib.DateTime.new_now_local().format('%Y-%m-%d %H:%M %Z'),
            'Free storage': f'{fs.f_bavail * fs.f_frsize / 1024**2:.0f} MiB',
        }
        return data, night

if __name__ == '__main__':
    if '--check' in sys.argv:
        apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
        print(json.dumps({'visible_apps': len(apps), 'gtk': Gtk.get_major_version(),
                          'session': bool(os.environ.get('DBUS_SESSION_BUS_ADDRESS'))}))
    else:
        raise SystemExit(Hub().run(sys.argv))
