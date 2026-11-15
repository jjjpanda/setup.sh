#!/usr/bin/env python3
"""Session helper: touch and Home buttons, X11 tooltip/maximize guards and headphone-jack audio routing."""
import ctypes
import ctypes.util
import fcntl
import importlib.util
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import threading
import time


WORK = Path(__file__).resolve().parent
EVENT = struct.Struct('@llHHi')
EVIOCGRAB = 0x40044590
JACK = Path('/sys/class/switch/h2w/state')
SPEAKER = [('Speaker Function', 'on'), ('SPKL Mixer DACLSPKL', 'on'), ('HeadPhone Function', 'off'),
           ('HPL Mixer DACLHPL', 'off'), ('HPR Mixer DACRHPR', 'off'), ('DACL', '5'), ('DACR', '5')]
HEADPHONES = [('Speaker Function', 'off'), ('SPKL Mixer DACLSPKL', 'off'), ('HeadPhone Function', 'on'),
              ('HPL Mixer DACLHPL', 'on'), ('HPR Mixer DACRHPR', 'on'), ('DACL', '7'), ('DACR', '7')]
MAIN_MIC = [('HP Mic Function', 'off'), ('ADCL Mixer HPMICADCL', 'off'), ('ADCR Mixer HPMICADCR', 'off'),
            ('Mic Function', 'on'), ('ADCL Mixer MainMICADCL', 'on'), ('ADCR Mixer MainMICADCR', 'on')]
HEADSET_MIC = [('Mic Function', 'off'), ('ADCL Mixer MainMICADCL', 'off'), ('ADCR Mixer MainMICADCR', 'off'),
               ('HP Mic Function', 'on'), ('ADCL Mixer HPMICADCL', 'on'), ('ADCR Mixer HPMICADCR', 'on')]
ROUTES = {'0': SPEAKER + MAIN_MIC, '1': HEADPHONES + HEADSET_MIC, '2': HEADPHONES + MAIN_MIC}
IGNORE_X_ERRORS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda *_: 0)


class MapEvent(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('event', ctypes.c_ulong), ('window', ctypes.c_ulong),
                ('override_redirect', ctypes.c_int)]


class ClientMessage(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                ('format', ctypes.c_int), ('data', ctypes.c_long * 5)]


class Event(ctypes.Union):
    _fields_ = [('type', ctypes.c_int), ('xmap', MapEvent), ('xclient', ClientMessage),
                ('pad', ctypes.c_long * 24)]


def jack_router():
    """Follow h2w: 0 speaker + main mic, 1 headset, 2 headphones + main mic."""
    applied = None
    while True:
        try:
            state = JACK.read_text().strip()
        except OSError:
            state = '0'
        if state != applied and state in ROUTES:
            for control, value in ROUTES[state]:
                subprocess.run(['amixer', '-q', '-c', 'sprdphone', 'sset', control, value],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('Audio route: ' + {'0': 'speaker', '1': 'headset', '2': 'headphones'}[state], flush=True)
            applied = state
        time.sleep(3)


def xlib():
    os.environ.setdefault('DISPLAY', ':0')
    os.environ.setdefault('XAUTHORITY', str(Path.home() / '.Xauthority'))
    x = ctypes.CDLL(ctypes.util.find_library('X11'))
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
    x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(Event)]
    x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                     ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                     ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.POINTER(ctypes.c_void_p)]
    x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_uint)]
    x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    x.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XWarpPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                             ctypes.POINTER(Event)]
    x.XSetErrorHandler(IGNORE_X_ERRORS)
    display = x.XOpenDisplay(None)
    if not display:
        raise RuntimeError('No X display')

    def atom(name):
        return x.XInternAtom(display, name, 0)

    def values(window, prop, kind=0, length=8):
        actual, fmt, count, rest, data = (ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(),
                                          ctypes.c_ulong(), ctypes.c_void_p())
        if x.XGetWindowProperty(display, window, prop, 0, length, 0, kind, actual, fmt, count, rest, data) \
                or not data:
            return None
        found = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:count.value]
        x.XFree(data)
        return found

    return x, display, x.XDefaultRootWindow(display), atom, values


def maximize_guard():
    """Maximize new top-level NORMAL windows; skip dialogs, transients, docks and skip-taskbar tools."""
    x, display, root, atom, values = xlib()
    clients, kind, state = atom(b'_NET_CLIENT_LIST'), atom(b'_NET_WM_WINDOW_TYPE'), atom(b'_NET_WM_STATE')
    normal, skip = atom(b'_NET_WM_WINDOW_TYPE_NORMAL'), atom(b'_NET_WM_STATE_SKIP_TASKBAR')
    vert, horz = atom(b'_NET_WM_STATE_MAXIMIZED_VERT'), atom(b'_NET_WM_STATE_MAXIMIZED_HORZ')

    def maximize(window):
        types = values(window, kind, 4) or [normal]
        flags = values(window, state, 4) or []
        if normal not in types or skip in flags or vert in flags or values(window, 68, 33):
            return
        message = Event()
        message.xclient.type, message.xclient.window = 33, window
        message.xclient.message_type, message.xclient.format = state, 32
        message.xclient.data[:] = [1, vert, horz, 1, 0]
        x.XSendEvent(display, root, 0, (1 << 19) | (1 << 20), message)
        x.XFlush(display)

    x.XSelectInput(display, root, 1 << 22)
    known = set(values(root, clients, 33, 1024) or [])
    event = Event()
    while True:
        x.XNextEvent(display, event)
        current = set(values(root, clients, 33, 1024) or [])
        for window in current - known:
            maximize(window)
        known = current


def tooltip_guard():
    """Non-composited Xorg ignores GTK tooltip opacity; unmap tooltip windows as they appear."""
    x, display, root, atom, values = xlib()
    kind, tooltip = atom(b'_NET_WM_WINDOW_TYPE'), atom(b'_NET_WM_WINDOW_TYPE_TOOLTIP')

    def is_tooltip(window):
        return tooltip in (values(window, kind, 4) or [])

    def hide(window):
        x.XMoveWindow(display, window, -4000, -4000)
        x.XUnmapWindow(display, window)
        x.XWarpPointer(display, 0, root, 0, 0, 0, 0, 0, 0)
        x.XFlush(display)

    x.XSelectInput(display, root, 1 << 19)
    parent, children, count = ctypes.c_ulong(), ctypes.c_void_p(), ctypes.c_uint()
    if x.XQueryTree(display, root, ctypes.c_ulong(), parent, children, count) and children:
        for window in ctypes.cast(children, ctypes.POINTER(ctypes.c_ulong))[:count.value]:
            if is_tooltip(window):
                hide(window)
        x.XFree(children)
    event = Event()
    while True:
        x.XNextEvent(display, event)
        if event.type == 19 and is_tooltip(event.xmap.window):
            hide(event.xmap.window)


def background(target):
    def run():
        while True:
            try:
                target()
            except Exception as error:
                print(target.__name__ + ': ' + str(error), flush=True)
            time.sleep(10)
    threading.Thread(target=run, daemon=True).start()


def device(name):
    for candidate in Path('/sys/class/input').glob('event*'):
        try:
            if (candidate / 'device/name').read_text().strip() == name:
                return Path('/dev/input') / candidate.name
        except OSError:
            pass
    return None


def load_control():
    sys.path.insert(0, str(WORK))
    spec = importlib.util.spec_from_file_location('tablet_control', WORK / 'tablet-control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    os.environ.update(control.desktop_environment())
    return control


def act(control, action):
    def run():
        try:
            control.run(action)
        except Exception as error:
            print(f'Button {action} failed: {error}', flush=True)
    print('Button: ' + action, flush=True)
    threading.Thread(target=run, daemon=True).start()


def watch(name, grab, actions, control):
    """Read one input device; grab only devices whose keys X must not also see."""
    while True:
        path = device(name)
        try:
            if path is None:
                raise FileNotFoundError('No ' + name)
            with path.open('rb', buffering=0) as source:
                if grab:
                    fcntl.ioctl(source, EVIOCGRAB, 1)
                print(f'{name} active: ' + ', '.join(actions.values()), flush=True)
                pending = b''
                while True:
                    if not select.select([source], [], [], 5)[0]:
                        continue
                    block = source.read(EVENT.size * 16)
                    if not block:
                        break
                    pending += block
                    while len(pending) >= EVENT.size:
                        _, _, kind, code, value = EVENT.unpack(pending[:EVENT.size])
                        pending = pending[EVENT.size:]
                        if kind == 1 and value == 1 and code in actions:
                            act(control, actions[code])
        except (PermissionError, FileNotFoundError):
            pass
        except OSError as error:
            print(f'{name} unavailable: {error}', flush=True)
        time.sleep(5)


def main():
    with (WORK / 'touchkeys.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        background(tooltip_guard)
        background(maximize_guard)
        background(jack_router)
        control = load_control()
        # sci-keypad also carries power/volume, so it is read without a grab.
        threading.Thread(target=watch, args=('sci-keypad', False, {172: 'home', 102: 'home'}, control),
                         daemon=True).start()
        watch('sec_touchkey', True, {254: 'recents', 158: 'back'}, control)


if __name__ == '__main__':
    main()
