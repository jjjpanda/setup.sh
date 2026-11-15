#!/bin/sh
# POSIX entry point. Bootstraps python3 on --apply if missing, then runs setup.py.
set -eu
setup_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

apply_mode=0
for arg in "$@"; do
    if [ "$arg" = "--apply" ]; then
        apply_mode=1
        break
    fi
done

if ! command -v python3 >/dev/null 2>&1; then
    if [ "$apply_mode" -eq 0 ]; then
        echo 'Python 3 is required. Run with --apply to install it interactively, or install python3 first.' >&2
        exit 2
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo 'Installing Python 3 requires root. Re-run with sudo.' >&2
        exit 2
    fi
    if [ ! -t 0 ]; then
        echo 'Python 3 is missing and stdin is not a terminal; install python3 first.' >&2
        exit 2
    fi
    printf 'Python 3 is not installed. Install python3 now? [y/N] '
    IFS= read -r answer || answer=''
    case "$answer" in
        y|Y|yes|YES)
            if command -v apt-get >/dev/null 2>&1; then
                DEBIAN_FRONTEND=noninteractive apt-get update
                DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3
            elif command -v apk >/dev/null 2>&1; then
                apk update
                apk add python3
            else
                echo 'No supported package manager (apt-get or apk) found to install python3.' >&2
                exit 2
            fi
            ;;
        *)
            echo 'Aborted: Python 3 is required.' >&2
            exit 2
            ;;
    esac
fi

exec python3 "$setup_dir/setup.py" "$@"
