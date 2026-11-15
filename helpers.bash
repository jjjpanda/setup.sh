# Opt-in Bash conveniences. Sourcing defines functions/aliases only.
[[ -n ${BASH_VERSION:-} ]] || return 0
alias ll='ls -alF'
alias space='df -h'
alias disks='lsblk -f'

# Private configuration is read only on explicit invocation, never on source.
_linux_setup_config() {
    if (( EUID == 0 )); then
        printf 'Run helpers as your desktop user, without sudo.\n' >&2
        return 2
    fi
    local config="$HOME/.config/linux-setup/local.bash" metadata
    if [[ -e $config || -L $config ]]; then
        [[ -f $config && ! -L $config ]] || { printf 'Unsafe local config\n' >&2; return 2; }
        metadata=$(stat -c '%u:%a' -- "$config") || return
        [[ $metadata == "$EUID:600" ]] || { printf 'Local config must be user owned, mode 0600\n' >&2; return 2; }
        # This is a user-owned Bash config; values are never eval'd as commands.
        source "$config" || return
    fi
}
cwork() {
    [[ $# == 0 ]] || { printf 'Usage: cwork\n' >&2; return 2; }
    _linux_setup_config || return
    [[ -n ${PROJECTS_DIR:-} && -d $PROJECTS_DIR ]] || { printf 'Set PROJECTS_DIR to an existing directory\n' >&2; return 2; }
    cd -- "$PROJECTS_DIR"
}
cmedia() {
    [[ $# == 0 ]] || { printf 'Usage: cmedia\n' >&2; return 2; }
    _linux_setup_config || return
    [[ -n ${MEDIA_DIR:-} && -d $MEDIA_DIR ]] || { printf 'Set MEDIA_DIR to an existing directory\n' >&2; return 2; }
    cd -- "$MEDIA_DIR"
}
_linux_setup_tool() {
    _linux_setup_config || return
    command -v python3 >/dev/null || { printf 'Missing dependency: python3\n' >&2; return 127; }
    local helper=$1
    shift
    case $helper in
        video-h264) command python3 "$HOME/.local/share/linux-setup/helper-cli.py" "$helper" "--default-crf=${VIDEO_CRF:-25}" "$@" ;;
        serve-local) command python3 "$HOME/.local/share/linux-setup/helper-cli.py" "$helper" "--default-port=${LOCAL_HTTP_PORT:-8000}" "$@" ;;
        *) command python3 "$HOME/.local/share/linux-setup/helper-cli.py" "$helper" "$@" ;;
    esac
}
vcls() { _linux_setup_tool vcls "$@"; }
vcmount() { _linux_setup_tool vcmount "$@"; }
vcumount() { _linux_setup_tool vcumount "$@"; }
video-h264() { _linux_setup_tool video-h264 "$@"; }
video-clip() { _linux_setup_tool video-clip "$@"; }
serve-local() { _linux_setup_tool serve-local "$@"; }
workspace() { _linux_setup_tool workspace "$@"; }
