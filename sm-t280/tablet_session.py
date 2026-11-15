"""Use the existing tablet desktop session from SSH; no new D-Bus session."""
import os
from pathlib import Path


def desktop_environment():
    env = dict(os.environ, DISPLAY=':0', XAUTHORITY=str(Path.home() / '.Xauthority'),
               XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}', XDG_CURRENT_DESKTOP='XFCE',
               XDG_SESSION_DESKTOP='xfce')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid == os.getuid() and (proc / 'comm').read_text().strip() == 'xfce4-session':
                for field in (proc / 'environ').read_bytes().split(b'\0'):
                    if field.startswith((b'DBUS_SESSION_BUS_ADDRESS=', b'SESSION_MANAGER=')):
                        key, value = field.decode().split('=', 1)
                        env[key] = value
                return env
        except OSError:
            pass
    raise RuntimeError('No existing XFCE session found; log into the desktop first')
