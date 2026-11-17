# XFCE setup

Opinionated setup for an XFCE desktop on Linux Mint (`apt`) or the postmarketOS
SM-T280 tablet (`apk`, detected automatically). Log into the desktop session
first, then run from the repository:

```sh
sh setup.sh --plan         # read-only, the default
sudo sh setup.sh --apply
```

On `--apply`, `setup.sh` offers to install `python3` if it is missing before
handing off to `setup.py`. The target account comes from `SUDO_USER` (or the
current user for `--plan`). The plan lists only the changes needed; apply
writes them and skips correct values. Exit codes: 0 complete, 2 error, 3 waiting
for a desktop session.

## Laptop

[`laptop_profile.py`](laptop_profile.py) defines the Mint/desktop XFCE profile.
Prompts whether to run `apt-get full-upgrade` (or pass `--upgrade` / `--no-upgrade`
with `--apply`), installs the apps in `packages()`
(VeraCrypt and Obsidian fall back to pinned upstream `.deb` entries in
[`vendor.json`](vendor.json)), removes and blocks `REMOVE_PACKAGES`, enables
UFW (deny incoming), and sets the dark theme, fonts, bottom panel, Gammastep
schedule, natural (inverse) touchpad scrolling and Bash helpers. Private shell
preferences go in `~/.config/linux-setup/local.bash`.

## SM-T280 tablet

[`sm-t280/profile.py`](sm-t280/profile.py) defines the tablet desktop appearance,
Onboard keyboard, toolset (`~/.local/share/sm-t280/`) and firewall service
(expects the device bring-up, audio, and udev/Xorg rules from the `sm-t280`
repository to be installed first). Packages are never upgraded. Changed system
files need a reboot.

The firewall allows SSH and mDNS on `wlan0` only from the active Wi-Fi
network (and link-local IPv6), updated automatically by NetworkManager when
`wlan0` connects or changes networks. Check it with
`rc-service sm-t280-firewall status` and `sudo iptables-legacy -S`.

## Camera Stream

`camera-preview` serves a camera as MJPEG on localhost (with optional RTSP) from
a Linux webcam over SSH or from the tablet. On the tablet, open **Camera
Stream** first; only one tablet camera can run at a time. Tablet camera scripts
expect non-redistributable sensor profiles (`profile.json`, `front-profile.json`)
under `~/.local/share/sm-t280/private/camera/`.

```sh
camera-preview --tablet-host tablet --camera rear --port 8765
camera-preview --tablet-host tablet --camera front --front-wb 0.758,0.742
camera-preview --ssh HOST --rtsp rtsp://127.0.0.1:8554/laptop
```

## Workspace

`workspace open DIRECTORY` mounts each regular file in a directory as a
VeraCrypt volume (one password, PIM 0, no keyfiles) and merges the volumes in
filename order. The directory must contain only volume files.

```sh
workspace open /path/to/containers [--mount-point PATH] [--app COMMAND ...]
workspace close /path/to/containers [--mount-point PATH]
```

Mounts go to `~/mnt/workspaces/NAME-HASH/`. `--app` must be last and runs in the
merged directory; exit it before you close. Use the same `--mount-point` to
close. A failed open warns about partial mounts and prompts to roll them back
(`[y/N]`), and busy mounts stop cleanup.
