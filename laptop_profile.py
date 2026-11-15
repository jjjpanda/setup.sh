"""Linux Mint / desktop XFCE profile for setup.py."""
from pathlib import Path

THEME = None
FONT = "Inter 11"
MONOSPACE = "JetBrains Mono 11"
DAY_TEMP = 6500
NIGHT_TEMP = 3400
DAWN = "06:30-07:30"
DUSK = "18:00-19:00"
REMOVE_PACKAGES = (
    "hypnotix", "thunderbird*", "transmission*", "libreoffice*",
    "warpinator", "rhythmbox*", "librhythmbox*", "redshift*", "light-locker*",
)
SERVICES = ()
OLD_FILES = (
    ".config/autostart/redshift-night-light.desktop",
    ".config/redshift/redshift.conf",
    ".local/share/linux-setup/night-light.py",
    ".local/share/linux-setup/transaction.py",
    ".local/share/linux-setup/desktop-session.py",
    ".local/share/linux-setup/firefox-private.svg",
    ".local/share/applications/linux-setup-files.desktop",
    ".local/share/applications/linux-setup-terminal.desktop",
    ".local/share/applications/linux-setup-firefox.desktop",
    ".local/share/applications/linux-setup-veracrypt.desktop",
    ".local/share/applications/linux-setup-obsidian.desktop",
    ".local/share/applications/linux-setup-gufw.desktop",
    ".local/share/camera-preview/sm-t280/source-client.py",
)

FONTS = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <match target="font">
    <edit name="antialias" mode="assign"><bool>true</bool></edit>
    <edit name="hinting" mode="assign"><bool>true</bool></edit>
    <edit name="hintstyle" mode="assign"><const>hintslight</const></edit>
    <edit name="rgba" mode="assign"><const>rgb</const></edit>
    <edit name="lcdfilter" mode="assign"><const>lcddefault</const></edit>
  </match>
  <alias><family>sans-serif</family><prefer><family>Inter</family><family>Noto Sans</family><family>DejaVu Sans</family></prefer></alias>
  <alias><family>monospace</family><prefer><family>JetBrains Mono</family><family>DejaVu Sans Mono</family><family>Noto Sans Mono</family></prefer></alias>
</fontconfig>
"""

BEGIN = b"# >>> linux-setup helpers >>>\n"
END = b"# <<< linux-setup helpers <<<\n"
BLOCK = BEGIN + b'''if [ -n "${BASH_VERSION:-}" ] && [ -r "$HOME/.local/share/linux-setup/helpers.bash" ]; then
    . "$HOME/.local/share/linux-setup/helpers.bash"
fi
''' + END


def theme_name(host):
    if THEME:
        return THEME
    return "Mint-Y-Dark" if host.path("/usr/share/themes/Mint-Y-Dark").is_dir() else "Adwaita-dark"


def packages(host):
    pkgs = ["python3", "xfconf", "thunar", "xfce4-terminal", "firefox", "xfce4-power-manager"]
    if host.manager == "apt":
        pkgs += [
            "python3-gi", "xfce4-screensaver", "gir1.2-gtk-3.0", "ffmpeg",
            "veracrypt", "obsidian", "ufw", "gufw", "gammastep", "unionfs-fuse",
            "xfce4-power-manager-plugins", "x11-xserver-utils", "libglib2.0-bin",
            "adwaita-icon-theme", "gnome-themes-extra",
            "fonts-noto-core", "fonts-dejavu-core", "fonts-inter", "fonts-jetbrains-mono",
        ]
    else:
        pkgs += [
            "py3-gobject3", "gtk+3.0", "ffmpeg", "gammastep", "unionfs-fuse",
            "xrandr", "adwaita-icon-theme", "gnome-themes-extra",
            "font-noto", "font-dejavu", "font-inter", "font-jetbrains-mono",
        ]
    return list(dict.fromkeys(pkgs))


def settings(host, session, launchers, xf, gs):
    theme = theme_name(host)
    gs("org.gnome.desktop.interface", "color-scheme", "s", "prefer-dark")
    gs("org.gnome.desktop.interface", "gtk-theme", "s", theme)
    gs("org.gnome.desktop.interface", "font-name", "s", FONT)
    xf("xsettings", "/Net/ThemeName", "s", theme)
    xf("xsettings", "/Gtk/FontName", "s", FONT)
    xf("xsettings", "/Gtk/MonospaceFontName", "s", MONOSPACE)
    xf("xsettings", "/Xft/DPI", "i", 96)
    for prop, kind, value in (("Antialias", "i", 1), ("Hinting", "i", 1), ("HintStyle", "s", "hintslight"), ("RGBA", "s", "rgb")):
        xf("xsettings", "/Xft/" + prop, kind, value)
    xf("xsettings", "/Gtk/EnableAnimations", "b", False)
    xf("xfwm4", "/general/use_compositing", "b", False)
    xf("xfwm4", "/general/workspace_count", "i", 1)
    xf("thunar", "/misc-thumbnail-mode", "s", "THUNAR_THUMBNAIL_MODE_NEVER")
    backdrop = session.list("xfce4-desktop", "/backdrop")
    bases = {path.rsplit("/", 1)[0] for path in backdrop if "/workspace" in path}
    for base in sorted(bases or {"/backdrop/screen0/monitor0/workspace0"}):
        xf("xfce4-desktop", base + "/image-style", "i", 0)
        xf("xfce4-desktop", base + "/color-style", "i", 0)
        xf("xfce4-desktop", base + "/rgba1", "ad", [18 / 255, 22 / 255, 31 / 255, 1.0])
    height = 48
    xf("xfce4-panel", "/panels", "ai", [1])
    for key, kind, value in (
        ("position", "s", "p=10;x=0;y=0"), ("size", "u", height),
        ("nrows", "u", 1), ("icon-size", "u", 32), ("length", "u", 100),
        ("autohide-behavior", "u", 0), ("enable-struts", "b", True), ("position-locked", "b", True),
    ):
        xf("xfce4-panel", "/panels/panel-1/" + key, kind, value)
    ids = session.get(["xf", "xfce4-panel", "/plugins/plugin-ids-linux-setup"])
    numbers = [int(n) for n in ids["value"]] if ids and isinstance(ids.get("value"), list) else []
    names = [
        "files", "terminal", "firefox", "private",
        *(["veracrypt", "obsidian"] if host.manager == "apt" else []),
        "camera-stream",
        *(["gufw"] if host.manager == "apt" else []),
    ]
    whisker = any(any(host.path("/usr/lib").glob(pattern)) for pattern in
                  ("xfce4/panel/plugins/libwhiskermenu.so", "*/xfce4/panel/plugins/libwhiskermenu.so"))
    plugins = ["whiskermenu" if whisker else "applicationsmenu"] + names + ["tasklist", "separator", "systray", "power-manager-plugin", "clock"]
    count = len(plugins)
    if len(numbers) != count:
        import re
        occupied = {int(match.group(1)) for prop in session.list("xfce4-panel", "/plugins")
                    if (match := re.match(r"^/plugins/plugin-(\d+)(?:/|$)", prop))}
        for first in range(100, 10000, count):
            candidate = list(range(first, first + count))
            if not occupied.intersection(candidate):
                numbers = candidate
                break
        else:
            raise ValueError("No free panel plugin IDs")
    xf("xfce4-panel", "/plugins/plugin-ids-linux-setup", "ai", numbers)
    for number, name in zip(numbers, plugins):
        xf("xfce4-panel", "/plugins/plugin-" + str(number), "s", "launcher" if name in names else name)
        if name in names:
            launchers.append((number, name))
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/items", "as", ["linux-setup-panel-" + name + ".desktop"])
        elif name == "separator":
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/expand", "b", True)
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/style", "u", 0)
        elif name == "systray":
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/icon-size", "i", 0)
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/single-row", "b", True)
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/square-icons", "b", True)
        elif name in ("whiskermenu", "applicationsmenu"):
            base = "/plugins/plugin-" + str(number) + "/"
            xf("xfce4-panel", base + "button-icon", "s", "view-app-grid")
            xf("xfce4-panel", base + "show-button-title", "b", False)
            if name == "whiskermenu":
                xf("xfce4-panel", base + "show-button-icon", "b", True)
            xf("xfce4-panel", base + "button-title", "s", "")
        elif name == "tasklist":
            for prop, value in (("show-labels", False), ("flat-buttons", True), ("show-handle", False)):
                xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/" + prop, "b", value)
        elif name == "clock":
            xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/mode", "u", 2)
            for prop, kind, value in (
                ("digital-layout", "u", 1), ("digital-time-format", "s", "%H:%M"),
                ("digital-date-format", "s", "%Y-%m-%d"), ("digital-time-font", "s", "Inter 16"),
                ("digital-date-font", "s", "Inter 10"),
            ):
                xf("xfce4-panel", "/plugins/plugin-" + str(number) + "/" + prop, kind, value)
    xf("xfce4-panel", "/panels/panel-1/plugin-ids", "ai", numbers)
    xf("xfwm4", "/general/margin_bottom", "i", height)


def shell_content(existing):
    if existing.count(BEGIN) != existing.count(END) or existing.count(BEGIN) > 1:
        raise ValueError("Malformed or duplicate managed Bash source block")
    if BEGIN in existing:
        start, finish = existing.index(BEGIN), existing.index(END) + len(END)
        if finish <= start:
            raise ValueError("Malformed managed Bash source block")
        return existing[:start] + BLOCK + existing[finish:]
    return existing + (b"\n" if existing and not existing.endswith(b"\n") else b"") + BLOCK


def update_ini_key(text, section, key, value):
    """Update or insert one INI key while preserving existing comments, blank lines, and casing."""
    import re
    lines = text.splitlines(keepends=True)
    current = None
    in_target = False
    replaced = False
    last_section_line = None
    key_pattern = re.compile(rf"^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)(.*?)(\r?\n?)$", re.IGNORECASE)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            in_target = current == section
            if in_target:
                last_section_line = idx
            continue
        if in_target:
            last_section_line = idx
            match = key_pattern.match(line)
            if match:
                lines[idx] = f"{match.group(1)}{value}{match.group(3)}"
                replaced = True
    if replaced:
        return "".join(lines)
    if last_section_line is not None:
        if lines[last_section_line] and not lines[last_section_line].endswith("\n"):
            lines[last_section_line] += "\n"
        lines.insert(last_section_line + 1, f"{key}={value}\n")
        return "".join(lines)
    prefix = "\n" if text and not text.endswith("\n") else ""
    return f"{text}{prefix}[{section}]\n{key}={value}\n"


def files(host, add, launchers, errors, source):
    """Panel launchers live in the panel's own directory under a name absent from XDG; otherwise xfce4-panel copies and renames them."""
    theme = theme_name(host)
    gtk = (
        "[Settings]\ngtk-application-prefer-dark-theme=1\ngtk-theme-name=" + theme +
        "\ngtk-font-name=" + FONT + "\ngtk-enable-animations=0\n"
    )
    for version in (3, 4):
        add(f".config/gtk-{version}.0/settings.ini", gtk)
    add(".config/fontconfig/fonts.conf", FONTS)
    add("/etc/sysctl.d/90-desktop-responsiveness.conf", "vm.swappiness = 10\nvm.vfs_cache_pressure = 50\n", system=True)
    if host.manager == "apt":
        policy = ("Package: " + " ".join(REMOVE_PACKAGES) + "\nPin: version *\nPin-Priority: -1\n") if REMOVE_PACKAGES else ""
        add("/etc/apt/preferences.d/90-linux-setup", policy, system=True)
    for name, section in (("slick-greeter.conf", "Greeter"), ("lightdm-gtk-greeter.conf", "greeter")):
        path = host.path("/etc/lightdm/" + name)
        if path.is_file():
            updated = update_ini_key(path.read_text(errors="replace"), section, "background", "#12161f")
            add("/etc/lightdm/" + name, updated, path.stat().st_mode & 0o777, system=True)
    add(".config/gammastep/config.ini", f"[general]\ntemp-day={DAY_TEMP}\ntemp-night={NIGHT_TEMP}\ndawn-time={DAWN}\ndusk-time={DUSK}\nadjustment-method=randr\n")
    add(".config/autostart/gammastep.desktop", "[Desktop Entry]\nType=Application\nName=Gammastep\nExec=gammastep\nTerminal=false\n")
    private_icon = "firefox-nightly" if host.path("/usr/share/icons/Mint-Y/apps/32/firefox-nightly.png").is_file() else "firefox"
    commands = dict(
        files=("Files", "thunar %F", "system-file-manager"),
        terminal=("Terminal", "xfce4-terminal", "utilities-terminal"),
        firefox=("Firefox", "firefox --new-window %u", "firefox"),
        private=("Firefox (Private)", "firefox --private-window %u", private_icon),
        veracrypt=("VeraCrypt", "veracrypt", "veracrypt"),
        obsidian=("Obsidian", "obsidian", "obsidian"),
        gufw=("Firewall", "gufw", "gufw"),
        **{"camera-stream": ("Camera Stream", '"' + str(host.home / ".local/bin/camera-preview") + '" --app', "camera-video")},
    )
    panel_desktop = {}
    for name, (title, command, icon) in commands.items():
        desktop = f"[Desktop Entry]\nType=Application\nName={title}\nExec={command}\nIcon={icon}\nTerminal=false\n"
        panel_desktop[name] = desktop.encode()
        if name in ("private", "camera-stream"):
            add(".local/share/applications/linux-setup-" + name + ".desktop", panel_desktop[name])
    add(".local/share/camera-preview/source-client.py", (source / "camera-source-client.py").read_bytes())
    for number, name in launchers:
        add(f".config/xfce4/panel/launcher-{number}/linux-setup-panel-{name}.desktop", panel_desktop[name])
    if not host.which("bash"):
        errors.append("Shell helpers require Bash")
    else:
        add(".local/share/linux-setup/helpers.bash", (source / "helpers.bash").read_bytes())
        add(".local/share/linux-setup/helper-cli.py", (source / "helper-cli.py").read_bytes())
        add(".local/share/linux-setup/workspace.py", (source / "workspace.py").read_bytes())
        rc = host.home / ".bashrc"
        try:
            add(".bashrc", shell_content(rc.read_bytes() if rc.exists() else b""),
                (rc.stat().st_mode & 0o777) if rc.exists() else 0o644)
        except ValueError as error:
            errors.append(str(error))
        local = host.home / ".config/linux-setup/local.bash"
        if not local.exists():
            add(".config/linux-setup/local.bash", b"# Private paths/preferences only. Never put credentials here.\n# PROJECTS_DIR=\"$HOME/projects\"\n# MEDIA_DIR=\"$HOME/media\"\nVIDEO_CRF=25\nLOCAL_HTTP_PORT=8000\n", 0o600)
        elif (local.stat().st_mode & 0o777) != 0o600 or local.stat().st_uid != host.uid:
            errors.append("Private helper config must be user owned with mode 0600")
