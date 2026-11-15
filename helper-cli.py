#!/usr/bin/env python3
"""Validated argv builders for opt-in shell helpers; no credential options."""
import argparse
from functools import partial
import http.server
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def bounded(value, low, high, label):
    if not re.fullmatch(r"[0-9]+", str(value)) or not low <= int(value) <= high:
        raise ValueError(f"{label} must be {low}..{high}")
    return int(value)


def timestamp(value):
    if not re.fullmatch(r"(?:\d+:)?[0-5]?\d:[0-5]\d(?:\.\d+)?|\d+(?:\.\d+)?", value):
        raise ValueError("Time must be seconds or [HH:]MM:SS[.fraction]")
    parts = value.split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    if not 0 <= total <= 7 * 24 * 3600:
        raise ValueError("Time outside 0..7 days")
    return total


def absolute(value):
    return str(Path(value).expanduser().resolve())


def existing_file(value):
    result = absolute(value)
    if not Path(result).is_file():
        raise ValueError("Input must be an existing regular file")
    return result


def output_path(value):
    result = Path(value).expanduser().absolute()
    if result.exists() or result.is_symlink():
        raise ValueError("Output already exists; choose a new name")
    if not result.parent.is_dir():
        raise ValueError("Output directory does not exist")
    return str(result)


def build(args):
    if args.command == "vcls":
        return ["veracrypt", "--text", "--list"]
    if args.command == "vcmount":
        container = existing_file(args.container)
        mount = absolute(args.mountpoint)
        if not Path(mount).is_dir():
            raise ValueError("Mountpoint must be an existing directory")
        if os.path.ismount(mount) or any(Path(mount).iterdir()):
            raise ValueError("Mountpoint must be empty and unmounted")
        return ["veracrypt", "--text", "--mount", container, mount]
    if args.command == "vcumount":
        mount = absolute(args.mountpoint)
        if not Path(mount).is_dir():
            raise ValueError("Mountpoint must be an existing directory")
        return ["veracrypt", "--text", "--dismount", mount]
    if args.command == "video-h264":
        crf = bounded(args.crf if args.crf is not None else args.default_crf, 0, 51, "CRF")
        return ["ffmpeg", "-nostdin", "-n", "-i", existing_file(args.input), "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264", "-crf", str(crf), "-c:a", "aac", "-b:a", "192k", output_path(args.output)]
    if args.command == "video-clip":
        start, end = timestamp(args.start), timestamp(args.end)
        if end <= start:
            raise ValueError("END must be later than START")
        # Exact mode decodes/re-encodes with AAC; copy mode cuts near keyframes.
        codecs = ["-c", "copy"] if args.copy else ["-c:v", "libx264", "-crf", "25", "-c:a", "aac", "-b:a", "192k"]
        return ["ffmpeg", "-nostdin", "-n", "-i", existing_file(args.input), "-ss", str(start), "-t", str(end-start),
                "-map", "0:v:0", "-map", "0:a?", *codecs, output_path(args.output)]
    raise ValueError("Not a subprocess helper")


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    """Loopback browser files; no symlink escape from the selected directory."""
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
                      ".m4a": "audio/mp4", ".wasm": "application/wasm", ".js": "text/javascript"}

    def translate_path(self, path):
        translated = Path(super().translate_path(path))
        if not translated.resolve().is_relative_to(Path(self.directory).resolve()):
            # Map escaping symlinks to a nonexistent path inside the selected root.
            return str(Path(self.directory) / ".linux-setup-refused-path" / "missing")
        return str(translated)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve(args):
    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("Serve requires an existing explicit directory")
    port = bounded(args.port if args.port is not None else args.default_port, 1024, 65535, "Port")
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(LocalHandler, directory=str(directory))) as server:
        print(f"Serving {directory} at http://127.0.0.1:{port}/; Ctrl-C stops it. No HTTP Range support.", flush=True)
        server.serve_forever()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("vcls")
    mount = sub.add_parser("vcmount")
    mount.add_argument("container")
    mount.add_argument("mountpoint")
    sub.add_parser("vcumount").add_argument("mountpoint")
    video = sub.add_parser("video-h264")
    video.add_argument("input")
    video.add_argument("output")
    video.add_argument("crf", nargs="?")
    video.add_argument("--default-crf", default="25")
    clip = sub.add_parser("video-clip")
    for name in ("input", "start", "end", "output"):
        clip.add_argument(name)
    clip.add_argument("--copy", action="store_true", help="fast, approximate keyframe cut; audio copied")
    local = sub.add_parser("serve-local")
    local.add_argument("directory")
    local.add_argument("port", nargs="?")
    local.add_argument("--default-port", default="8000")
    workspace = sub.add_parser("workspace")
    workspace.add_argument("action", choices=("open", "close"))
    workspace.add_argument("directory")
    workspace.add_argument("--mount-point", help="merged view destination; use the same value when closing")
    workspace.add_argument("--app", nargs=argparse.REMAINDER, default=[])
    return p


def main():
    args = parser().parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Run helpers as your desktop user, without sudo")
    if args.command == "serve-local":
        serve(args)
        return 0
    if args.command == "workspace":
        import workspace
        return workspace.run(args.action, args.directory, args.app, args.mount_point)
    argv = build(args)
    if not shutil.which(argv[0]):
        print("Missing dependency:", argv[0], file=sys.stderr)
        return 127
    return subprocess.call(argv)  # credentials remain software prompts on the terminal


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
