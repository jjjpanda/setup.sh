#!/usr/bin/env python3
"""Prepare an XFCE desktop; plan by default, apply with sudo."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

SOURCE = Path(__file__).resolve().parent
VENDOR_CONFIG = SOURCE / "vendor.json"


def load_vendor():
    """Load upstream .deb URLs and pinned SHA-256 digests from vendor.json."""
    if not VENDOR_CONFIG.is_file():
        raise ValueError(f"Missing vendor manifest: {VENDOR_CONFIG}")
    data = json.loads(VENDOR_CONFIG.read_text())
    if not isinstance(data, dict):
        raise ValueError("vendor.json must map package names to url and sha256")
    vendor = {}
    for package, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid vendor.json entry for {package}")
        url = entry.get("url", "")
        digest = str(entry.get("sha256", "")).lower()
        if not isinstance(url, str) or not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid url or sha256 in vendor.json for {package}")
        vendor[package] = (url, digest)
    return vendor


class DesktopUnavailable(ValueError):
    pass


def load_profile(tablet):
    name, rel = ("sm_t280_profile", "sm-t280/profile.py") if tablet else ("laptop_profile", "laptop_profile.py")
    spec = importlib.util.spec_from_file_location(name, SOURCE / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=3600, **kwargs)


class Host:
    def __init__(self, user=None):
        if sys.platform != "linux":
            raise ValueError("Linux required")
        target = user or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        try:
            entry = pwd.getpwnam(target)
        except (KeyError, TypeError):
            raise ValueError("Run as or with SUDO_USER set to the desktop account") from None
        self.user, self.uid, self.gid = entry.pw_name, entry.pw_uid, entry.pw_gid
        self.home = Path(entry.pw_dir)
        if self.uid == 0 or not self.home.is_absolute() or self.home == Path("/"):
            raise ValueError("Select an existing non-root desktop account with an absolute home")
        if any(c in str(self.home) for c in '\n\r\0"\\%`$'):
            raise ValueError("Target home contains unsupported desktop-launcher characters")
        if not self.home.is_dir() or self.home.is_symlink() or self.home.stat().st_uid != self.uid:
            raise ValueError("Target home missing, redirected or owned by a different account")
        self.root = Path("/")
        self.manager = "apt" if shutil.which("apt-get") else "apk" if shutil.which("apk") else None
        self.arch = platform.machine()

    def path(self, name):
        return self.root / name.lstrip("/")

    def command(self, args, live=False):
        if live:
            print("Running:", " ".join(args), flush=True)
        return subprocess.run(args, text=True, capture_output=not live, timeout=3600,
                              env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))

    def which(self, name):
        return shutil.which(name)

    def file_owner(self, system):
        return (0, 0) if system else (self.uid, self.gid)

    def session_command(self, request):
        env = dict(
            os.environ, HOME=str(self.home), USER=self.user, LOGNAME=self.user,
            XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_DATA_HOME=str(self.home / ".local/share"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_RUNTIME_DIR=f"/run/user/{self.uid}",
        )
        for name in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
            env.pop(name, None)
        procs = sorted((p for p in Path("/proc").iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
        for process in procs:
            try:
                if process.stat().st_uid == self.uid and (process / "comm").read_text().strip() == "xfce4-session":
                    for item in (process / "environ").read_bytes().split(b"\0"):
                        key, _, value = item.partition(b"=")
                        decoded = key.decode(errors="ignore")
                        if decoded in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR"):
                            env[decoded] = value.decode()
                    break
            except (OSError, UnicodeError):
                continue
        if not env.get("DISPLAY") or not env.get("DBUS_SESSION_BUS_ADDRESS"):
            raise DesktopUnavailable("Log into the XFCE desktop session and rerun")
        argv = [sys.executable, str(SOURCE / "desktop-session.py")]

        def demote():
            os.initgroups(self.user, self.gid)
            os.setgid(self.gid)
            os.setuid(self.uid)

        result = run(argv, input=json.dumps(request), env=env, preexec_fn=demote if os.geteuid() == 0 else None)
        if result.returncode:
            raise ValueError("Desktop transport: " + result.stderr.strip())
        return json.loads(result.stdout)

    def vendor_install(self, package, vendor):
        """Official upstream .deb for apps absent from Ubuntu/Mint repositories, pinned by SHA-256."""
        url, digest = vendor[package]
        filename = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or f"{package}.deb"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / filename
            hasher = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as out:
                while chunk := response.read(65536):
                    hasher.update(chunk)
                    out.write(chunk)
            if hasher.hexdigest() != digest:
                raise OSError("Checksum mismatch: " + url)
            os.chmod(directory, 0o755)
            return self.command(["apt-get", "install", "-y", "--no-install-recommends", str(path)], live=True)

    def installed_set(self, packages):
        if not packages:
            return set()
        if self.manager == "apt":
            result = self.command(["dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n", *packages])
            return {
                parts[0].split(":", 1)[0] for line in result.stdout.splitlines()
                if len(parts := line.split("\t")) == 2 and parts[1] == "installed"
            }
        if self.manager == "apk":
            result = self.command(["apk", "info", "-e", *packages])
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return set()

    def installed(self, package):
        return package in self.installed_set([package])


class Session:
    def __init__(self, host):
        self.host = host

    def get(self, key):
        return self.host.session_command(dict(action="get", key=key))

    def normalize(self, key, value):
        return self.host.session_command(dict(action="normalize", key=key, value=value))

    def list(self, channel, base):
        return self.host.session_command(dict(action="list", key=["xf", channel, base]))

    def set(self, key, value):
        self.host.session_command(dict(action="set", key=key, value=value))
        readback = self.get(key)
        if readback is None or readback.get("type") != value.get("type") or readback.get("text") != value.get("text"):
            raise ValueError("Desktop readback differs: " + str(key))

    def restart_panel(self):
        self.host.session_command(dict(action="restart-panel"))


def is_tablet(host):
    for name in ("/etc/deviceinfo", "/proc/device-tree/model", "/proc/device-tree/compatible",
                 "/sys/devices/virtual/dmi/id/product_name"):
        path = host.path(name)
        if path.exists() and re.search(r"sm[-_ ]?t280|gtexswifi", path.read_text(errors="replace").lower()):
            return True
    return any(host.path("/etc/init.d").glob("sm-t280-*"))


def variant(kind, value):
    if kind == "s":
        value = "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    elif kind == "b":
        value = "true" if value else "false"
    elif isinstance(value, list):
        value = "[" + ", ".join(variant(kind[1:], item)["text"] for item in value) + "]"
    else:
        value = str(value)
    return dict(type=kind, text=value)


def settings_plan(host, profile, session, launchers):
    result = []

    def xf(channel, prop, kind, value):
        result.append((["xf", channel, prop], variant(kind, value)))

    def gs(schema, key, kind, value):
        result.append((["gs", schema, key], variant(kind, value)))

    profile.settings(host, session, launchers, xf, gs)
    return result


def files_plan(host, profile, launchers, errors):
    files = []

    def add(name, content, mode=0o644, system=False):
        path = host.path(name) if system else host.home / name
        files.append((path, content.encode() if isinstance(content, str) else content, mode, system))

    for name, target, mode in (
        ("camera-preview.py", ".local/bin/camera-preview", 0o755),
        ("camera-stream-app.py", ".local/share/camera-preview/application.py", 0o644),
    ):
        add(target, (SOURCE / name).read_bytes(), mode)
    profile.files(host, add, launchers, errors, SOURCE)
    return files


def install_packages(host, missing, remove, upgrade=True, vendor=None):
    if not host.manager:
        raise ValueError("No supported package manager")
    if not upgrade and not missing and not remove:
        return []
    vendor = load_vendor() if vendor is None else vendor
    if remove:
        cmd = ["apt-get", "remove", "-y", *remove] if host.manager == "apt" else ["apk", "del", *remove]
        result = host.command(cmd, live=True)
        if result.returncode:
            raise ValueError("Package removal exited " + str(result.returncode))
    if upgrade:
        commands = [["apt-get", "update"], ["apt-get", "-y", "full-upgrade"]] if host.manager == "apt" else [["apk", "update"], ["apk", "upgrade"]]
    else:
        commands = [["apt-get", "update"]] if host.manager == "apt" else [["apk", "update"]]
    for command in commands:
        result = host.command(command, live=True)
        if result.returncode:
            raise ValueError(" ".join(command) + " exited " + str(result.returncode))
    errors = []
    for package in missing:
        try:
            argv = ["apt-get", "install", "-y", "--no-install-recommends", package] if host.manager == "apt" else ["apk", "add", package]
            result = host.command(argv, live=True)
            if (result.returncode or not host.installed(package)) and package in vendor and host.manager == "apt" and host.arch == "x86_64":
                result = host.vendor_install(package, vendor)
            if result.returncode or not host.installed(package):
                errors.append("package " + package + ": install exited " + str(result.returncode))
        except (OSError, subprocess.SubprocessError) as error:
            errors.append("package " + package + ": " + str(error))
    return errors


def changed(host, item):
    path, content, mode, system = item
    try:
        if path.is_symlink() or not path.exists():
            return True
        stat = path.stat()
        return (stat.st_mode & 0o777 != mode or
                (stat.st_uid, stat.st_gid) != host.file_owner(system) or
                path.read_bytes() != content)
    except PermissionError:
        return True


def old_files(host, profile):
    """Files written by earlier versions of this script."""
    paths = []
    for name in profile.OLD_FILES:
        path = Path(name) if Path(name).is_absolute() else host.home / name
        if path.is_file() or path.is_symlink():
            paths.append(path)
    return paths


def ufw_needed(host, tablet, missing=()):
    if tablet or host.manager != "apt":
        return False
    if "ufw" in missing:
        return True
    if not host.which("ufw"):
        return False
    if os.geteuid() == 0:
        res = host.command(["ufw", "status"])
        return res.returncode == 0 and "Status: active" not in res.stdout
    conf = host.path("/etc/ufw/ufw.conf")
    return conf.is_file() and "ENABLED=yes" not in conf.read_text(errors="replace")


def enable_ufw(host):
    for cmd in (["ufw", "default", "deny", "incoming"],
                ["ufw", "default", "allow", "outgoing"],
                ["ufw", "--force", "enable"]):
        res = host.command(cmd, live=True)
        if res.returncode:
            raise ValueError(" ".join(cmd) + " exited " + str(res.returncode))


def services_needed(host, profile):
    needed = []
    for name, level in profile.SERVICES:
        link = host.path(f"/etc/runlevels/{level}/{name}")
        if not link.is_symlink() and not link.exists():
            needed.append((name, level))
    return needed


def plan_session(host, profile, session):
    settings, waiting, launchers, errors = [], [], [], []
    try:
        for key, value in settings_plan(host, profile, session, launchers):
            try:
                value = session.normalize(key, value)
                current = session.get(key)
                if current is None or current.get("type") != value.get("type") or current.get("text") != value.get("text"):
                    settings.append((key, value))
            except DesktopUnavailable as error:
                waiting.append(str(error))
            except ValueError as error:
                errors.append(str(error))
    except DesktopUnavailable as error:
        waiting.append(str(error))
    return settings, waiting, launchers, errors


def prepare(host, tablet, profile):
    vendor = {} if tablet else load_vendor()
    desired_pkgs = profile.packages(host)
    installed = host.installed_set(desired_pkgs)
    missing = [p for p in desired_pkgs if p not in installed]
    package_removals = []
    if not tablet and host.manager == "apt" and profile.REMOVE_PACKAGES:
        removed_installed = host.command(["dpkg-query", "-W", "-f=${binary:Package} ${db:Status-Status}\n", *profile.REMOVE_PACKAGES])
        candidates = sorted({line.split()[0] for line in removed_installed.stdout.splitlines()
                             if len(line.split()) == 2 and line.split()[1] == "installed"})
        if candidates:
            simulation = host.command(["apt-get", "-s", "remove", *candidates])
            if simulation.returncode:
                raise ValueError("Package removal plan: " + simulation.stderr.strip())
            package_removals = [line.split()[1] for line in simulation.stdout.splitlines() if line.startswith("Remv ")]
    session = Session(host)
    deferred_settings = "python3-gi" in missing or "py3-gobject3" in missing
    if deferred_settings:
        settings, waiting, launchers, errors = [], [], [], []
    else:
        settings, waiting, launchers, errors = plan_session(host, profile, session)
    files = [item for item in files_plan(host, profile, launchers, errors) if changed(host, item)]
    return dict(
        missing=missing, package_removals=package_removals, settings=settings, files=files,
        remove=old_files(host, profile), enable_ufw=ufw_needed(host, tablet, missing),
        services=services_needed(host, profile), waiting=waiting, errors=errors,
        session=session, vendor=vendor, deferred_settings=deferred_settings,
    )


def format_path(host, path):
    try:
        return "~/" + str(path.relative_to(host.home))
    except ValueError:
        return str(path)


def report(host, tablet, plan):
    release = host.path("/etc/os-release")
    match = re.search(r'^PRETTY_NAME=(.*)$', release.read_text(), re.M) if release.exists() else None
    distro = match[1].strip('"') if match else "Linux"
    print(f"{'tablet' if tablet else 'laptop'} \u00b7 user {host.user} \u00b7 {distro}")
    print(f"packages  {len(plan['missing'])} to install, {len(plan['package_removals'])} to remove" + (", upgrade optional" if not tablet else ""))
    if plan["deferred_settings"]:
        print("settings  deferred until GI bindings are installed")
    else:
        print(f"settings  {len(plan['settings'])} to change")
    print(f"files     {len(plan['files'])} to write, {len(plan['remove'])} to remove")
    print("---")
    for package in plan["missing"]:
        print("+ package", package)
    for package in plan["package_removals"]:
        print("- package", package)
    for key, value in plan["settings"]:
        print("~ setting", key[1], key[2])
    for path, *_ in plan["files"]:
        print("+ file", format_path(host, path))
    for path in plan["remove"]:
        print("- file", format_path(host, path))
    if plan["enable_ufw"]:
        print("~ firewall enable ufw (default deny incoming)")
    for name, level in plan["services"]:
        print("+ service", name, level)


def write_file(host, item):
    path, content, mode, system = item
    if path.is_symlink():
        raise OSError(f"Refusing to overwrite symlink: {path}")
    anchor = host.root if system else host.home
    rel_parts = path.parent.relative_to(anchor).parts
    cursor = anchor
    for part in rel_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise OSError(f"Refusing to traverse symlinked directory: {cursor}")
    parents = []
    parent = path.parent
    while not parent.exists():
        parents.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if not system:
        for created in parents:
            os.chown(created, host.uid, host.gid)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fchown(out.fileno(), *host.file_owner(system))
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def confirm_upgrade():
    """Ask interactively whether to run apt-get full-upgrade before applying."""
    if not sys.stdin.isatty():
        raise ValueError("Pass --upgrade or --no-upgrade when running --apply without a terminal")
    while True:
        try:
            answer = input("Upgrade packages first (apt-get full-upgrade)? [y/N] ").strip().lower()
        except EOFError:
            return False
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True


def try_step(errors, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        errors.append(str(error))
        return None


def apply(host, tablet, profile, plan, upgrade=False):
    errors = list(plan["errors"])
    waiting = list(plan["waiting"])
    settings = list(plan["settings"])
    files = list(plan["files"])
    policy = host.path("/etc/apt/preferences.d/90-linux-setup")
    for item in files:
        if item[0] == policy:
            try_step(errors, write_file, host, item)
    if upgrade or plan["missing"] or plan["package_removals"]:
        installed_errors = try_step(
            errors, install_packages, host, plan["missing"], plan["package_removals"],
            upgrade=upgrade, vendor=plan["vendor"],
        )
        if installed_errors:
            errors += installed_errors
    if plan["deferred_settings"]:
        new_settings, new_waiting, new_launchers, new_errors = plan_session(host, profile, plan["session"])
        settings = new_settings
        waiting.extend(new_waiting)
        file_errors = []
        files = [item for item in files_plan(host, profile, new_launchers, file_errors) if changed(host, item)]
        for err in new_errors + file_errors:
            if err not in errors:
                errors.append(err)
    for item in files:
        if item[0] != policy:
            try_step(errors, write_file, host, item)
    for path in plan["remove"]:
        try_step(errors, path.unlink)
    for key, value in settings:
        try_step(errors, plan["session"].set, key, value)
    if any(key[1] == "xfce4-panel" for key, _ in settings):
        try_step(errors, plan["session"].restart_panel)
    if plan["enable_ufw"]:
        try_step(errors, enable_ufw, host)
    for name, level in plan["services"]:
        result = host.command(["rc-update", "add", name, level], live=True)
        if result.returncode:
            errors.append(f"rc-update add {name} {level} exited {result.returncode}")
    for error in errors + waiting:
        print("error:", error)
    if tablet and (plan["services"] or any(system for *_, system in files)):
        print("Reboot to load the changed system configuration.")
    if not errors and not waiting:
        print("done")
    return 2 if errors else 3 if waiting else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--apply", action="store_true")
    upgrades = parser.add_mutually_exclusive_group()
    upgrades.add_argument("--upgrade", action="store_true", help="run full-upgrade during --apply")
    upgrades.add_argument("--no-upgrade", action="store_true", help="skip full-upgrade during --apply")
    args = parser.parse_args()
    if (args.upgrade or args.no_upgrade) and not args.apply:
        raise ValueError("Use --upgrade or --no-upgrade only with --apply")
    host = Host()
    tablet = is_tablet(host)
    if tablet and args.upgrade:
        raise ValueError("Tablet packages cannot be upgraded")
    if args.apply and os.geteuid() != 0:
        raise ValueError("Apply requires sudo")
    profile = load_profile(tablet)
    plan = prepare(host, tablet, profile)
    report(host, tablet, plan)
    if args.apply:
        if tablet:
            upgrade = False
        elif args.upgrade:
            upgrade = True
        elif args.no_upgrade or plan["errors"]:
            upgrade = False
        else:
            upgrade = confirm_upgrade()
        return apply(host, tablet, profile, plan, upgrade=upgrade)
    for message in plan["errors"] + plan["waiting"]:
        print("error:", message)
    return 2 if plan["errors"] else 3 if plan["waiting"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("error:", error, file=sys.stderr)
        sys.exit(2)
