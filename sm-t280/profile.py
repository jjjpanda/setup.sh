"""SM-T280 (samsung-gtexswifi) tablet mode for setup.py: desktop settings, files and boot services."""
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
TOOLSET_DIR = ".local/share/sm-t280"
PACKAGES = ["firefox", "ffmpeg", "ffplay", "py3-gobject3", "iptables-legacy"]
REMOVE_PACKAGES = ()
SERVICES = (("sm-t280-firewall", "boot"),)
RUNTIME = {"tablet-hub.py": 0o755, "tablet-control.py": 0o755, "tablet-touchkeys.py": 0o755, "tablet-camera.py": 0o755,
           "camera-stream-worker.py": 0o755, "tablet_session.py": 0o644,
           "tablet_camera_capture.py": 0o644, "tablet_camera_front.py": 0o644, "tablet_camera_raw.py": 0o644,
           "tablet_camera_ae.py": 0o644}
OLD_FILES = ()
FONT = "Adwaita Sans"
KEYBOARD = ('sh -c "dbus-send --session --type=method_call --dest=org.onboard.Onboard '
            '/org/onboard/Onboard/Keyboard org.onboard.Onboard.Keyboard.ToggleVisible || onboard &"')


def packages(host):
    return list(PACKAGES)


def settings(host, session, launchers, xf, gs):
    home = host.home
    for prop, kind, value in (("/Net/ThemeName", "s", "TabletNight"), ("/Net/IconThemeName", "s", "Adwaita"),
                              ("/Xft/DPI", "i", 168), ("/Gtk/FontName", "s", FONT + " 10"),
                              ("/Gtk/MonospaceFontName", "s", "Adwaita Mono 10"), ("/Gtk/CursorThemeSize", "i", 32),
                              ("/Gtk/EnableAnimations", "b", False)):
        xf("xsettings", prop, kind, value)
    for prop, kind, value in (("theme", "s", "TabletNight"), ("title_font", "s", FONT + " Bold 11"),
                              ("workspace_count", "i", 1), ("use_compositing", "b", False), ("margin_bottom", "i", 96),
                              ("box_move", "b", True), ("box_resize", "b", True), ("button_layout", "s", "|HMC"),
                              ("cycle_preview", "b", False), ("show_dock_shadow", "b", False),
                              ("wrap_windows", "b", False)):
        xf("xfwm4", "/general/" + prop, kind, value)
    xf("xfce4-panel", "/panels", "ai", [1])
    for prop, kind, value in (("position", "s", "p=10;x=640;y=799"), ("size", "u", 96), ("icon-size", "u", 56),
                              ("plugin-ids", "ai", [18, 2, 3, 19, 5, 6, 7, 8]), ("autohide-behavior", "u", 0),
                              ("enter-opacity", "u", 100), ("leave-opacity", "u", 100), ("background-style", "u", 1),
                              ("background-rgba", "ad", [0.08, 0.10, 0.14, 1.0]), ("popdown-speed", "u", 0),
                              ("enable-struts", "b", True)):
        xf("xfce4-panel", "/panels/panel-1/" + prop, kind, value)
    for number, item in ((18, "tablet-hub.desktop"), (19, "tablet-keyboard.desktop")):
        xf("xfce4-panel", f"/plugins/plugin-{number}", "s", "launcher")
        xf("xfce4-panel", f"/plugins/plugin-{number}/items", "as", [item])
        xf("xfce4-panel", f"/plugins/plugin-{number}/disable-tooltips", "b", True)
    for prop, kind, value in (("plugin-2/show-labels", "b", False), ("plugin-2/show-tooltips", "b", False),
                              ("plugin-2/show-handle", "b", False), ("plugin-5/icon-size", "i", 48),
                              ("plugin-7/show-notifications", "u", 0), ("plugin-8/digital-time-format", "s", "%H:%M"),
                              ("plugin-8/digital-time-font", "s", FONT + " 14"), ("plugin-8/tooltip-format", "s", "")):
        xf("xfce4-panel", "/plugins/" + prop, kind, value)
    for monitor in ("monitordefault", "monitor0"):
        base = f"/backdrop/screen0/{monitor}/workspace0/"
        xf("xfce4-desktop", base + "image-style", "i", 0)
        xf("xfce4-desktop", base + "color-style", "i", 0)
        xf("xfce4-desktop", base + "rgba1", "ad", [0.055, 0.07, 0.10, 1.0])
    xf("xfce4-desktop", "/desktop-icons/style", "i", 0)
    xf("xfce4-power-manager", "/xfce4-power-manager/show-panel-label", "u", 1)
    xf("xfce4-power-manager", "/xfce4-power-manager/power-button-action", "u", 3)
    for prop, kind, value in (("/saver/idle-activation/delay", "i", 5), ("/lock/saver-activation/enabled", "b", True),
                              ("/lock/saver-activation/delay", "i", 0), ("/lock/embedded-keyboard/displayed", "b", True)):
        xf("xfce4-screensaver", prop, kind, value)
    xf("xfce4-keyboard-shortcuts", "/commands/custom/XF86Back", "s",
       f"python3 {home}/{TOOLSET_DIR}/tablet-control.py back")
    xf("xfce4-keyboard-shortcuts", "/xfwm4/custom/HomePage", "s", "")
    xf("thunar", "/misc-thumbnail-mode", "s", "THUNAR_THUMBNAIL_MODE_NEVER")
    for key, kind, value in (("key-label-font", "s", FONT), ("layout", "s", "Compact"), ("show-status-icon", "b", False),
                             ("start-minimized", "b", True), ("system-theme-tracking-enabled", "b", False),
                             ("use-system-defaults", "b", False)):
        gs("org.onboard", key, kind, value)
    for key, kind, value in (("color-scheme", "s", "/usr/share/onboard/themes/Charcoal.colors"),
                             ("key-style", "s", "gradient"), ("key-fill-gradient", "d", 8.0),
                             ("key-stroke-gradient", "d", 8.0), ("key-gradient-direction", "d", -3.0),
                             ("key-stroke-width", "d", 0.0), ("key-size", "d", 94.0), ("key-label-font", "s", ""),
                             ("roundrect-radius", "d", 20.0)):
        gs("org.onboard.theme-settings", key, kind, value)
    gs("org.onboard.typing-assistance.word-suggestions", "enabled", "b", False)
    for key, kind, value in (("background-transparency", "d", 0.0), ("docking-enabled", "b", True),
                             ("docking-shrink-workarea", "b", True)):
        gs("org.onboard.window", key, kind, value)
    for orientation, height in (("landscape", 280), ("portrait", 320)):
        gs("org.onboard.window." + orientation, "dock-height", "i", height)
        gs("org.onboard.window." + orientation, "height", "i", height)


def desktop(name, command, icon=None, extra="", comment=None):
    return (f"[Desktop Entry]\nType=Application\nName={name}\n" + (f"Comment={comment}\n" if comment else "") +
            f"Exec={command}\n" + (f"Icon={icon}\n" if icon else "") + extra + "Terminal=false\n")


def theme(root):
    """TabletNight: Greybird GTK with touch sizes, and recolored XFCE Default-xhdpi window frames."""
    css = '''/* Touch sizes for the installed Greybird dark GTK3 assets. */
@import url("/usr/share/themes/Greybird/gtk-3.0/gtk-contained-dark.css");
window, .background { background-color: #111722; color: #e8edf6; }
button, entry, searchentry, spinbutton { min-height: 44px; padding: 6px 10px; border-radius: 9px; }
button { background-image: none; background-color: #252e40; color: #e8edf6; }
button:hover { background-color: #35445e; }
button:checked { background-color: #355d91; }
menu { padding: 5px; }
menuitem { min-height: 40px; padding: 5px 12px; }
switch { min-width: 76px; min-height: 44px; }
switch slider { min-width: 38px; min-height: 38px; }
scale slider { min-width: 40px; min-height: 40px; }
scale trough { min-height: 8px; min-width: 8px; }
scrollbar.vertical slider { min-width: 26px; min-height: 56px; }
scrollbar.horizontal slider { min-height: 26px; min-width: 56px; }
notebook tab { padding: 10px 14px; }
.xfce4-panel { background-color: #151c29; color: #e8edf6;
  -XfcePanelWindow-autohide-size: 12; -XfcePanelWindow-popup-delay: 150;
  -XfcePanelWindow-popdown-delay: 450; border-top: 1px solid #40597b; }
.xfce4-panel button { padding: 0 8px; min-height: 80px; min-width: 64px; border-radius: 8px; }
.app-tile { padding: 14px; border: 1px solid #354159; border-radius: 16px; }
'''
    files = {
        "gtk-3.0/gtk.css": css,
        "gtk-3.0/gtk-dark.css": css,
        "gtk-2.0/gtkrc": '''include "/usr/share/themes/Greybird/gtk-2.0/gtkrc"
gtk-color-scheme="bg_color:#151c29\\nbase_color:#111722\\nfg_color:#e8edf6\\ntext_color:#e8edf6\\nselected_bg_color:#355d91\\nselected_fg_color:#ffffff\\npanel_bg:#151c29\\nfm_color:#111722"
gtk-font-name="Adwaita Sans 10"
''',
        "index.theme": "[Desktop Entry]\nName=TabletNight\nType=X-GNOME-Metatheme\n[X-GNOME-Metatheme]\n"
                       "GtkTheme=TabletNight\nMetacityTheme=TabletNight\nIconTheme=Adwaita\n",
        "SOURCE.txt": "Derived from installed Greybird GTK theme and XFCE Default-xhdpi XPM assets; "
                      "original themes remain in /usr/share/themes.\n",
    }
    source = root / "usr/share/themes/Default-xhdpi/xfwm4"

    def palette(match):
        name = match.group(1)
        if name.startswith("inactive"):
            return "c " + ("#9da9ba" if "text" in name else "#171e2a")
        return "c " + ("#e8edf6" if "text" in name else "#527bb0" if "hilight" in name else "#202a3b")
    for path in sorted(source.glob("*.xpm")):
        files["xfwm4/" + path.name] = re.sub(r"c #[0-9A-Fa-f]+ s (\w+)", palette, path.read_text())
    if (source / "themerc").is_file():
        files["xfwm4/themerc"] = ((source / "themerc").read_text()
                                  .replace("active_text_color=active_text_color_2", "active_text_color=#e8edf6")
                                  .replace("inactive_text_color=inactive_text_color_2", "inactive_text_color=#9da9ba"))
    return files


def firewall_rules(version):
    """iptables-restore input policy: drop unsolicited traffic except DHCP, ICMP, and dynamic Wi-Fi SSH/mDNS."""
    rules = ["-A INPUT -i lo -j ACCEPT", "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"]
    if version == 4:
        rules += ["-A INPUT -i wlan0 -p udp -m udp --sport 67 --dport 68 -j ACCEPT",
                  "-A INPUT -m conntrack --ctstate INVALID -j DROP", "-A INPUT -p icmp -j ACCEPT"]
    else:
        rules += ["-A INPUT -i wlan0 -p udp -m udp --sport 547 --dport 546 -j ACCEPT",
                  "-A INPUT -m conntrack --ctstate INVALID -j DROP", "-A INPUT -p ipv6-icmp -j ACCEPT"]
    rules.append("-A INPUT -i wlan0 -j SM_T280_WIFI")
    return ("*filter\n:INPUT DROP [0:0]\n:FORWARD ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\n:SM_T280_WIFI - [0:0]\n" +
            "".join(line + "\n" for line in rules) + "COMMIT\n")


FIREWALL_DISPATCHER = '''#!/usr/bin/env python3
"""Update SM_T280_WIFI firewall chain for the active wlan0 on-link subnets."""
import ipaddress
import re
import subprocess
import sys

INTERFACE = "wlan0"
CHAIN = "SM_T280_WIFI"
ACTIONS = {"up", "dhcp4-change", "dhcp6-change"}


def networks(family):
    result = subprocess.run(
        ["ip", family, "-o", "addr", "show", "dev", INTERFACE, "scope", "global"],
        text=True, capture_output=True, check=False,
    )
    seen = []
    for address in re.findall(r"inet6? (\\S+)", result.stdout):
        network = str(ipaddress.ip_interface(address).network)
        if network not in seen:
            seen.append(network)
    return seen


def sync(tool, sources):
    if subprocess.run([tool, "-n", "-L", CHAIN], capture_output=True, check=False).returncode != 0:
        return
    rules = [f"-F {CHAIN}"] + [
        f"-A {CHAIN} -s {source} -p {proto} -m {proto} --dport {port} -j ACCEPT"
        for source in sources
        for proto, port in (("tcp", "22"), ("udp", "5353"))
    ]
    payload = f"*filter\\n:{CHAIN} - [0:0]\\n" + "\\n".join(rules) + "\\nCOMMIT\\n"
    subprocess.run([tool + "-restore", "--noflush"], input=payload, text=True, check=True)


def main():
    if len(sys.argv) < 3 or sys.argv[1] != INTERFACE:
        return 0
    action = sys.argv[2]
    if action == "down":
        sync("iptables-legacy", [])
        sync("ip6tables-legacy", [])
    elif action in ACTIONS:
        sync("iptables-legacy", networks("-4"))
        sync("ip6tables-legacy", ["fe80::/10"] + networks("-6"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


FIREWALL_INIT = '''#!/sbin/openrc-run
description="SM-T280 input firewall"

depend() {
	need localmount
	after bootmisc
	before net networkmanager dropbear sm-t280-wcn
}

start() {
	ebegin "Loading SM-T280 input firewall"
	iptables-legacy-restore < /etc/sm-t280-firewall/rules.v4 && ip6tables-legacy-restore < /etc/sm-t280-firewall/rules.v6
	eend $?
}

stop() {
	ebegin "Keeping SM-T280 input firewall loaded"
	eend 0
}
'''
GREETER = f'''[Greeter]
background=#111722
background-color=#111722
draw-grid=false
draw-user-backgrounds=false
show-hostname=false
show-a11y=true
show-keyboard=false
theme-name=TabletNight
icon-theme-name=Adwaita
cursor-theme-size=32
font-name={FONT} 13
xft-dpi=168
xft-antialias=true
xft-hintstyle=hintslight
onscreen-keyboard=true
clock-format=%H:%M  %a %d %b
'''


def files(host, add, launchers, errors, source):
    home = host.home
    work = home / TOOLSET_DIR
    for name, mode in RUNTIME.items():
        add(f"{TOOLSET_DIR}/{name}", (HERE / name).read_bytes(), mode)
    add(".config/gtk-3.0/settings.ini", "[Settings]\ngtk-application-prefer-dark-theme=true\n"
                                        "gtk-enable-tooltips=false\ngtk-touchscreen-mode=true\n")
    add(".config/gtk-3.0/gtk.css", "/* TabletNight contains the touch styling. Previous overrides are backed up. */\n"
                                   "tooltip { opacity: 0; }\n")
    hub = desktop("Tablet Controls", f"python3 {work}/tablet-hub.py --dashboard", "view-app-grid-symbolic",
                  "Categories=Utility;\n", "Device controls and camera") + "StartupNotify=true\n"
    keyboard = desktop("Keyboard", KEYBOARD, "input-keyboard-symbolic", "Categories=Utility;\n") + "StartupNotify=true\n"
    for path, content in ((".local/share/applications/tablet-hub.desktop", hub),
                          (".config/xfce4/panel/launcher-18/tablet-hub.desktop", hub),
                          (".local/share/applications/tablet-keyboard.desktop", keyboard),
                          (".config/xfce4/panel/launcher-19/tablet-keyboard.desktop", keyboard)):
        add(path, content)
    add(".local/share/applications/camera-stream.desktop", desktop(
        "Camera Stream", f'"{home}/.local/bin/camera-preview" --app', "camera-video",
        "Categories=AudioVideo;Video;\n", "Start and stop the camera streaming application") +
        "StartupNotify=true\n")
    add(".config/autostart/tablet-touchkeys.desktop", desktop(
        "Tablet touch buttons", f'sh -c "exec python3 {work}/tablet-touchkeys.py >> {work}/touchkeys.log 2>&1"',
        extra="OnlyShowIn=XFCE;\n"))
    for name in ("blueman", "mpris-proxy", "pipewire"):
        add(f".config/autostart/{name}.desktop", "[Desktop Entry]\nType=Application\nHidden=true\n")

    def system(path, content, mode=0o644):
        add(path, content, mode, True)
    for name, content in theme(host.root).items():
        system("/usr/share/themes/TabletNight/" + name, content)
    system("/etc/lightdm/slick-greeter.conf", GREETER)
    system("/etc/sysctl.d/90-sm-t280-security.conf", "kernel.randomize_va_space = 2\nkernel.kptr_restrict = 2\n"
           "kernel.dmesg_restrict = 1\nfs.protected_hardlinks = 1\nfs.protected_symlinks = 1\n")
    system("/etc/conf.d/dropbear", "# /etc/conf.d/dropbear: config file for /etc/init.d/dropbear\n\n"
           "# see `dropbear -h` for more information\n# -w disables root logins\n"
           "# -p # changes the port number to listen on\nDROPBEAR_OPTS=\"-w -s\"\n")
    system("/etc/init.d/sm-t280-firewall", FIREWALL_INIT, 0o755)
    system("/etc/NetworkManager/dispatcher.d/90-sm-t280-firewall", FIREWALL_DISPATCHER, 0o755)
    for version in (4, 6):
        system(f"/etc/sm-t280-firewall/rules.v{version}", firewall_rules(version), 0o600)
