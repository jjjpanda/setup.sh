import getpass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def paths(directory, mount_point=None):
    containers = Path(directory).expanduser().resolve()
    if containers == Path("/") or any(c in str(containers) for c in "\r\n\0:=\"'\\"):
        raise ValueError("Unsupported container directory")
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", containers.name)[:32] or "workspace"
    name = label + "-" + hashlib.sha256(str(containers).encode()).hexdigest()[:12]
    base = Path.home() / "mnt/workspaces" / name
    if base.is_relative_to(containers) or containers.is_relative_to(base):
        raise ValueError("Container and workspace directories must be separate")
    root = base / "volumes"
    merged = Path(mount_point).expanduser().resolve() if mount_point is not None else base / "merged"
    if merged == Path("/") or any(c in str(merged) for c in "\r\n\0"):
        raise ValueError("Unsupported merged mount point")
    for path in (containers, root):
        if merged.is_relative_to(path) or path.is_relative_to(merged):
            raise ValueError("Merged mount point must be separate from containers and volume mounts")
    return name, containers, root, merged


def mounts():
    result = {}
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields = after.split()
        result[Path(unescape(before.split()[4]))] = (fields[0], unescape(fields[1]))
    return result


def volumes():
    result = subprocess.run(["veracrypt", "--text", "--list"], capture_output=True,
                            text=True, env=dict(os.environ, LC_ALL="C.UTF-8"))
    if result.returncode and "No volumes mounted" not in result.stdout + result.stderr:
        raise ValueError("Cannot list VeraCrypt volumes: " + result.stderr.strip())
    active = {}
    for line in result.stdout.splitlines():
        if not re.match(r"^\d+: ", line):
            continue
        if re.match(r"^\d+:\s+.+\s+-\s+-$", line):
            continue
        dev_matches = list(re.finditer(r"(?<=\s)/dev/\S+(?=\s)", line))
        if len(dev_matches) != 1:
            raise ValueError("Cannot parse VeraCrypt volume list")
        dev = dev_matches[0]
        volume = line[line.index(": ") + 2 : dev.start()].strip()
        target = line[dev.end() :].strip()
        if not volume or not target:
            raise ValueError("Cannot parse VeraCrypt volume list")
        if target != "-":
            active[Path(volume).resolve()] = Path(target).resolve()
    return active


def command(argv, password=None):
    result = subprocess.run(argv, input=password, text=True)
    if result.returncode:
        raise ValueError(argv[0] + " failed; current mounts remain available for review")


def rollback_mounts(mounted, merged=None):
    """Best-effort dismount of merged view (if mounted) and volumes mounted during a failed open."""
    errors = []
    if merged is not None and merged in mounts():
        print("Rollback unmount:", merged, flush=True)
        try:
            result = subprocess.run([shutil.which("fusermount3") or "fusermount", "-u", str(merged)], text=True)
            if result.returncode or merged in mounts():
                errors.append(merged.name)
        except (OSError, ValueError) as error:
            errors.append(f"{merged.name} ({error})")
    for volume, target in reversed(mounted):
        print("Rollback dismount:", target, flush=True)
        try:
            result = subprocess.run(["veracrypt", "--text", "--dismount", str(volume)], text=True)
            if result.returncode or volume in volumes() or target in mounts():
                errors.append(volume.name)
        except (OSError, ValueError) as error:
            errors.append(f"{volume.name} ({error})")
    return errors


def empty_directory(path):
    if path.is_symlink():
        raise ValueError("Mount directory is a symlink: " + str(path))
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError("Mount directory is not empty: " + str(path))


def operate(action, name, containers, root, merged, app):
    active, table = volumes(), mounts()
    identity = "linux-setup-" + str(os.getuid()) + "-" + name
    elsewhere = [path for path, value in table.items() if value == ("fuse.unionfs", identity) and path != merged]
    if elsewhere:
        raise ValueError("Workspace is merged elsewhere; use --mount-point " + str(elsewhere[0]))
    if merged in table and table[merged] != ("fuse.unionfs", identity):
        raise ValueError("Merged directory belongs to a different mount")
    selected = {volume: target for volume, target in active.items() if target.parent == root}
    for volume, target in selected.items():
        if volume.parent != containers or target.name != volume.name:
            raise ValueError("Mount root contains a different volume: " + str(target))
    if action == "close":
        if merged in table:
            command([shutil.which("fusermount3") or "fusermount", "-u", str(merged)])
            if merged in mounts():
                raise ValueError("Merged view is still mounted")
        for volume, target in reversed(sorted(selected.items())):
            print("Dismount:", target, flush=True)
            command(["veracrypt", "--text", "--dismount", str(volume)])
            if volume in volumes() or target in mounts():
                raise ValueError("Volume is still mounted: " + str(target))
        print("Workspace closed:", name)
        return 0
    if not containers.is_dir():
        raise ValueError("Container directory does not exist")
    files = sorted(path for path in containers.iterdir() if path.is_file() and not path.is_symlink() and not path.name.startswith("."))
    if not files:
        raise ValueError("Container directory has no files")
    if any(any(c in path.name for c in "\r\n\0:=\"'\\") for path in files):
        raise ValueError("Container filename contains unsupported characters")
    if set(selected) - set(files):
        raise ValueError("Workspace container selection changed; close it first")
    pending = []
    for volume in files:
        target = root / volume.name
        if volume in active:
            if active[volume] != target or target not in table:
                raise ValueError("Volume is mounted elsewhere: " + str(volume))
        else:
            if target in table:
                raise ValueError("Mount directory is occupied: " + str(target))
            pending.append((volume, target))
    if merged in table:
        if pending:
            raise ValueError("Close the workspace before adding containers")
        if app and not shutil.which(app[0]):
            raise ValueError("Workspace app is unavailable: " + app[0])
        print("Workspace already open:", name)
        return subprocess.call(app, cwd=merged) if app else 0
    if app and not shutil.which(app[0]):
        raise ValueError("Workspace app is unavailable: " + app[0])
    if not shutil.which("unionfs-fuse"):
        raise ValueError("Install unionfs-fuse first")
    print("Merged view:", merged, flush=True)
    empty_directory(merged)
    for volume, target in pending:
        empty_directory(target)
    password = None
    mounted = []
    try:
        if pending:
            if not sys.stdin.isatty():
                raise ValueError("Open the workspace from a terminal")
            if shutil.which("sudo") and subprocess.run(["sudo", "-v"]).returncode:
                raise ValueError("Administrator authorization failed")
            password = getpass.getpass("Workspace password: ") + "\n"
        for volume, target in pending:
            print("Mount:", volume.name, flush=True)
            command(["veracrypt", "--text", "--non-interactive", "--stdin", "--pim=0",
                     "--keyfiles=", "--protect-hidden=no", "--mount", str(volume), str(target)], password)
            if volumes().get(volume) != target or target not in mounts():
                raise ValueError("Volume mount was not confirmed: " + volume.name)
            mounted.append((volume, target))
        branches = ":".join(str(root / volume.name) + "=RW" for volume in files)
        command(["unionfs-fuse", "-o", "fsname=" + identity + ",subtype=unionfs", branches, str(merged)])
        if mounts().get(merged) != ("fuse.unionfs", identity):
            raise ValueError("Merged view mount was not confirmed")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if not mounted:
            if merged in mounts():
                rollback_errors = rollback_mounts([], merged)
                if rollback_errors:
                    print("warning: merged rollback incomplete:", "; ".join(rollback_errors), file=sys.stderr)
            raise
        print("warning: mount failed:", error, file=sys.stderr)
        print("warning: partial mounts remain active:", ", ".join(v.name for v, _ in mounted), file=sys.stderr)
        if sys.stdin.isatty():
            try:
                answer = input(f"Roll back {len(mounted)} mounted volume(s)? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in ("y", "yes"):
                rollback_errors = rollback_mounts(mounted, merged)
                if rollback_errors:
                    raise ValueError("Rollback incomplete: " + "; ".join(rollback_errors)) from None
                raise ValueError("Workspace open failed; rolled back mounted volumes") from None
        raise ValueError("Workspace open failed; partial mounts remain active") from None
    finally:
        password = None
    print("Workspace open:", merged, flush=True)
    return subprocess.call(app, cwd=merged) if app else 0


def run(action, directory, app, mount_point=None):
    if sys.platform != "linux":
        raise ValueError("Workspace helpers require Linux")
    import fcntl
    name, containers, root, merged = paths(directory, mount_point)
    if action == "close" and app:
        raise ValueError("Use --app only with workspace open")
    if app:
        app[0] = os.path.expanduser(app[0])
        if "/" in app[0]:
            app[0] = str(Path(app[0]).resolve())
    if not shutil.which("veracrypt"):
        raise ValueError("VeraCrypt is not installed")
    path = Path.home() / ".cache/linux-setup" / ("workspace-" + name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Workspace command already running: " + name) from None
        return operate(action, name, containers, root, merged, app)
