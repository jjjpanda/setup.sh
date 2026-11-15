#!/usr/bin/env python3
"""Small local desktop actions used by panel launchers and the touch-button helper."""
import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
import ctypes
import ctypes.util
from pathlib import Path
from tablet_session import desktop_environment

NAVIGABLE = ('thunar', 'firefox', 'chromium', 'epiphany', 'falkon', 'midori')
TERMINAL_LOCK = threading.Lock()
# A client can close between reading the window list and querying its class.
# Keep the callback alive; Xlib's default error handler exits the process.
IGNORE_X_ERRORS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda *_: 0)


class ClientMessage(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                ('format', ctypes.c_int), ('data', ctypes.c_long * 5), ('pad', ctypes.c_long * 12)]


def open_apps():
    """Open the XFCE app list; the first call leaves Appfinder resident for fast reopening."""
    if appfinder := shutil.which('xfce4-appfinder'):
        subprocess.Popen([appfinder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        result = subprocess.run(['gapplication', 'action', 'local.tablet.Hub', 'show-apps'],
                                capture_output=True, timeout=2)
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    # First launch, an older resident helper, or a missing gapplication utility.
    subprocess.Popen([sys.executable, str(Path(__file__).with_name('tablet-hub.py'))],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextlib.contextmanager
def x11():
    x = ctypes.CDLL(ctypes.util.find_library('X11'))
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                     ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                     ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.POINTER(ctypes.c_void_p)]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                             ctypes.POINTER(ClientMessage)]
    x.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x.XStringToKeysym.restype = ctypes.c_ulong
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x.XSetErrorHandler(IGNORE_X_ERRORS)
    display = x.XOpenDisplay(None)
    if not display:
        raise RuntimeError('No X display')
    try:
        yield x, display
    finally:
        x.XCloseDisplay(display)


def property_bytes(x, display, window, name, length=64):
    actual, fmt, count, rest, data = (ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(),
                                      ctypes.c_ulong(), ctypes.c_void_p())
    if x.XGetWindowProperty(display, window, x.XInternAtom(display, name, 0), 0, length, 0, 0,
                            actual, fmt, count, rest, data) or not data:
        return fmt.value, b''
    size = {8: 1, 16: ctypes.sizeof(ctypes.c_short), 32: ctypes.sizeof(ctypes.c_long)}.get(fmt.value, 1)
    found = ctypes.string_at(data, count.value * size)
    x.XFree(data)
    return fmt.value, found


def active_class(x, display):
    fmt, data = property_bytes(x, display, x.XDefaultRootWindow(display), b'_NET_ACTIVE_WINDOW', 1)
    window = ctypes.c_ulong.from_buffer_copy(data).value if fmt == 32 and data else 0
    return property_bytes(x, display, window, b'WM_CLASS')[1].decode(errors='replace').lower() if window else ''


def send_keys(x, display, names):
    xtest = ctypes.CDLL(ctypes.util.find_library('Xtst'))
    xtest.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    codes = [x.XKeysymToKeycode(display, x.XStringToKeysym(n.encode())) for n in names]
    if not all(codes):
        raise RuntimeError('Required key is not in the current keymap')
    for code in codes:
        xtest.XTestFakeKeyEvent(display, code, 1, 0)
    for code in reversed(codes):
        xtest.XTestFakeKeyEvent(display, code, 0, 0)
    x.XFlush(display)


def show_desktop(x, display):
    root = x.XDefaultRootWindow(display)
    event = ClientMessage(type=33, send_event=1, window=root, format=32,
                          message_type=x.XInternAtom(display, b'_NET_SHOWING_DESKTOP', 0))
    event.data[0] = 1
    x.XSendEvent(display, root, 0, (1 << 19) | (1 << 20), ctypes.byref(event))
    x.XFlush(display)


def terminal_window(x, display):
    root = x.XDefaultRootWindow(display)
    fmt, data = property_bytes(x, display, root, b'_NET_CLIENT_LIST_STACKING', 4096)
    if fmt != 32:
        return None
    windows = (ctypes.c_ulong * (len(data) // ctypes.sizeof(ctypes.c_ulong))).from_buffer_copy(data)
    for window in reversed(windows):
        classes = property_bytes(x, display, window, b'WM_CLASS')[1].lower().split(b'\0')
        if b'xfce4-terminal' in classes:
            return window
    return None


def open_terminal():
    """Raise the most recently stacked terminal, including minimized windows."""
    # Button actions run in threads. Serialize launches until a window exists.
    with TERMINAL_LOCK, x11() as (x, display):
        window = terminal_window(x, display)
        if window is None:
            subprocess.Popen(['xfce4-terminal'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while window is None and time.monotonic() < deadline:
                time.sleep(0.03)
                window = terminal_window(x, display)
        if window is not None:
            event = ClientMessage(type=33, send_event=1, window=window, format=32,
                                  message_type=x.XInternAtom(display, b'_NET_ACTIVE_WINDOW', 0))
            event.data[0] = 2  # Pager request: switch workspace and unminimize if needed.
            x.XSendEvent(display, x.XDefaultRootWindow(display), 0,
                         (1 << 19) | (1 << 20), ctypes.byref(event))
            x.XFlush(display)


def run(action):
    if action == 'keyboard':
        try:
            from gi.repository import Gio
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync('org.onboard.Onboard', '/org/onboard/Onboard/Keyboard',
                          'org.onboard.Onboard.Keyboard', 'ToggleVisible', None, None, 0, 2000, None)
        except Exception:
            subprocess.Popen(['onboard'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif action == 'recents':
        open_apps()
    elif action == 'home':
        open_terminal()
    elif action == 'back':
        with x11() as (x, display):
            name = active_class(x, display)
            send_keys(x, display, ['Alt_L', 'Left'] if any(app in name for app in NAVIGABLE) else ['Escape'])
    else:
        raise ValueError(action)


def main():
    if sys.argv[1:] not in (['keyboard'], ['recents'], ['back'], ['home']):
        raise SystemExit('Usage: tablet-control.py keyboard|recents|back|home')
    os.environ.update(desktop_environment())
    run(sys.argv[1])


if __name__ == '__main__':
    main()
