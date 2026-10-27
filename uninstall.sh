#!/bin/bash
#
# uninstall.sh - remove Dolphin-Fast-Share-Mount completely.
#
# Run it as your desktop user. It unmounts the tool's mounts first (asking
# for root rights once), then removes the system part (helper, polkit
# action), the user part (command, Dolphin menus), your settings, the log and
# the passwords the tool saved. A copy is installed with the tool, so this
# works without the project folder:
#   ~/.local/share/dolphin-fast-share-mount/uninstall.sh
#
#   ./uninstall.sh                   everything
#   ./uninstall.sh --keep-config     keep ~/.config/dolphin-fast-share-mount.conf
#   ./uninstall.sh --keep-passwords  keep saved passwords
#   ./uninstall.sh --force           detach mounts that are in use (lazy unmount)
#   ./uninstall.sh --user-only       only your part (unmounts through the helper)
#   sudo ./uninstall.sh --system-only
#   ./uninstall.sh --prefix /usr     system part installed under /usr
#
# Running it twice is harmless. Mount folders under /run are only ever
# removed when empty, so a share that is still mounted is never deleted
# through.
#
# SPDX-License-Identifier: GPL-3.0-or-later

VERSION=20261026

set -euo pipefail
umask 022

APP_ID=dolphin-fast-share-mount
SCRIPT=$(readlink -f -- "${BASH_SOURCE[0]}")
PREFIX=/usr/local
DESTDIR=${DESTDIR:-}
MOUNT_BASE=/run/$APP_ID
POLICY_NAME=io.github.redbearak.dolphin-fast-share-mount.policy
KEEP_CONFIG=""
KEEP_PASSWORDS=""
FORCE=""
FAILED=0

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; FAILED=1; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,22p' "$SCRIPT" | sed 's/^# \{0,1\}//'
}

remove_file() {
    local path=$1
    if [[ -L $path || -f $path ]]; then
        if rm -f -- "$path"; then say "  removed $path"; else warn "could not remove $path"; fi
    elif [[ -e $path ]]; then
        warn "$path is not a file; left in place"
    fi
}

# Remove a folder the tool owns entirely (no mounts can be inside it).
remove_tree() {
    local path=$1
    if [[ -L $path ]]; then
        if rm -f -- "$path"; then say "  removed $path"; else warn "could not remove $path"; fi
    elif [[ -d $path ]]; then
        if rm -rf -- "$path"; then say "  removed $path"; else warn "could not remove $path"; fi
    fi
}

remove_listed() {
    local manifest=$1 path
    shift
    [[ -f $manifest && ! -L $manifest ]] || return 0
    while IFS= read -r path; do
        [[ -n $path ]] || continue
        local root allowed=""
        for root in "$@"; do
            [[ $path == "$root"/* && $path != *"/../"* ]] && allowed=1 && break
        done
        if [[ -n $allowed ]]; then
            remove_file "$path"
        else
            say "  left $path alone (outside the tool's folders)"
        fi
    done < "$manifest"
}

# ---- system part -----------------------------------------------------------------

# Every CIFS mount below /run/dolphin-fast-share-mount, deepest first.
tool_mounts() {
    local line i
    local -a fields
    while IFS= read -r line; do
        read -ra fields <<< "$line"
        [[ ${fields[4]:-} == "$MOUNT_BASE"/* ]] || continue
        for (( i = 6; i < ${#fields[@]}; i++ )); do
            if [[ ${fields[i]} == "-" ]]; then
                case ${fields[i+1]:-} in
                    cifs|smb3) printf '%s\n' "${fields[4]}" ;;
                esac
                break
            fi
        done
    done < /proc/self/mountinfo | sort -r -u
}

unmount_everything() {
    local helper=$1 mountpoint remaining
    local -a lazy=()
    [[ -z $FORCE ]] || lazy=(--lazy)
    if [[ -x $helper ]]; then
        "$helper" unmount-all --every-user "${lazy[@]}" >/dev/null || true
    fi
    # Without a working helper (a damaged installation), unmount directly.
    while IFS= read -r mountpoint; do
        [[ -n $mountpoint ]] || continue
        if [[ -n $FORCE ]]; then
            timeout 30 umount -l -- "$mountpoint" || true
        else
            timeout 30 umount -- "$mountpoint" || true
        fi
    done < <(tool_mounts)
    remaining=$(tool_mounts)
    if [[ -n $remaining ]]; then
        printf 'Error: these shares are still mounted (in use?):\n%s\n' "$remaining" >&2
        die "Close the programs using them and run the uninstaller again, or add --force"
    fi
    if [[ -d $MOUNT_BASE && ! -L $MOUNT_BASE ]]; then
        find "$MOUNT_BASE" -xdev -depth -type d -empty -delete 2>/dev/null || true
    fi
    if [[ -e $MOUNT_BASE ]]; then
        warn "$MOUNT_BASE is not empty; left in place (it disappears at reboot)"
    else
        say "  no mounts left; removed $MOUNT_BASE"
    fi
}

uninstall_system() {
    local root=$DESTDIR
    if [[ -z $root ]]; then
        (( EUID == 0 )) || die "The system part needs root: sudo $0 --system-only"
    fi
    local helper="$PREFIX/libexec/fast-share-mount-helper"
    local policy="/usr/share/polkit-1/actions/$POLICY_NAME"
    local share_dir="$PREFIX/share/$APP_ID"

    say "Removing the system part under ${root:-}$PREFIX"
    if [[ -z $root ]]; then
        unmount_everything "$helper"
    fi
    remove_listed "$root$share_dir/installed-files.txt" \
        "$root$PREFIX/libexec" "$root/usr/share/polkit-1/actions" "$root$share_dir"
    remove_file "$root$helper"
    remove_file "$root$policy"
    remove_tree "$root$share_dir"
    say "System part removed."
}

# ---- user part -------------------------------------------------------------------

# Remove the KWallet folder with gdbus, for when the command itself is gone.
forget_kwallet_folder() {
    command -v gdbus >/dev/null || return 0
    local version name path wallet="kdewallet" reply handle tool
    for tool in kreadconfig6 kreadconfig5; do
        if command -v "$tool" >/dev/null; then
            reply=$("$tool" --file kwalletrc --group Wallet --key "Default Wallet" 2>/dev/null || true)
            [[ -z $reply ]] || wallet=$reply
            break
        fi
    done
    for version in 6 5; do
        name=org.kde.kwalletd$version
        path=/modules/kwalletd$version
        reply=$(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
            --method org.freedesktop.DBus.NameHasOwner "$name" 2>/dev/null || true)
        [[ $reply == *true* ]] || continue
        reply=$(gdbus call --session --dest "$name" --object-path "$path" \
            --method org.kde.KWallet.open "$wallet" 0 fast-share-mount 2>/dev/null || true)
        handle=$(sed -n 's/^(\(-\{0,1\}[0-9]*\),*)$/\1/p' <<< "$reply")
        [[ -n $handle && $handle != -* ]] || continue
        gdbus call --session --dest "$name" --object-path "$path" \
            --method org.kde.KWallet.removeFolder "$handle" "$APP_ID" fast-share-mount >/dev/null 2>&1 || true
    done
}

uninstall_user() {
    local unmount_first=$1
    (( EUID != 0 )) || die "Run the uninstaller as your desktop user, not with sudo"
    local data=${XDG_DATA_HOME:-$HOME/.local/share}
    local config=${XDG_CONFIG_HOME:-$HOME/.config}
    local state=${XDG_STATE_HOME:-$HOME/.local/state}
    local bin_dir=$HOME/.local/bin
    local command="$bin_dir/fast-share-mount"
    local own_dir="$data/$APP_ID"

    say "Removing the user part for $(id -un)"
    if [[ -n $unmount_first && -x $command ]]; then
        local -a force=()
        [[ -z $FORCE ]] || force=(--force)
        "$command" unmount --all "${force[@]}" || warn "some of your shares could not be unmounted"
    fi
    if [[ -z $KEEP_PASSWORDS ]]; then
        if [[ -x $command ]]; then
            local result
            result=$("$command" forget --all 2>/dev/null || true)
            say "  ${result:-could not run fast-share-mount forget --all}"
        else
            forget_kwallet_folder
            if command -v secret-tool >/dev/null; then
                secret-tool clear application "$APP_ID" 2>/dev/null || true
            fi
            say "  asked KWallet and the keyring to remove saved passwords"
        fi
    fi

    remove_listed "$own_dir/installed-files.txt" "$bin_dir" "$data/kio/servicemenus" "$own_dir"
    remove_file "$command"
    remove_file "$data/kio/servicemenus/dolphin-fast-share-mount.desktop"
    remove_file "$data/kio/servicemenus/dolphin-fast-share-mount-unmount.desktop"
    remove_tree "$own_dir"
    remove_tree "$state/$APP_ID"
    if [[ -n ${XDG_RUNTIME_DIR:-} ]]; then
        remove_file "$XDG_RUNTIME_DIR/$APP_ID.lock"
    fi
    if [[ -z $KEEP_CONFIG ]]; then
        remove_file "$config/$APP_ID.conf"
    else
        say "  kept $config/$APP_ID.conf"
    fi
    say "User part removed."
}

# ---- main ---------------------------------------------------------------------------

main() {
    local mode=both
    while (( $# )); do
        case $1 in
            --user-only) mode=user; shift ;;
            --system-only) mode=system; shift ;;
            --keep-config) KEEP_CONFIG=1; shift ;;
            --keep-passwords) KEEP_PASSWORDS=1; shift ;;
            --force) FORCE=1; shift ;;
            --prefix)
                (( $# >= 2 )) || die "--prefix needs a folder"
                PREFIX=${2%/}
                shift 2
                ;;
            --version) say "$VERSION"; exit 0 ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
    done
    case $PREFIX in
        /usr|/usr/local) ;;
        *) die "--prefix must be /usr or /usr/local" ;;
    esac

    case $mode in
        system)
            uninstall_system
            ;;
        user)
            uninstall_user unmount
            ;;
        both)
            (( EUID != 0 )) || die "Run the uninstaller as your desktop user, not with sudo (it asks for root itself)"
            local -a pass=(--system-only --prefix "$PREFIX")
            [[ -z $FORCE ]] || pass+=(--force)
            if [[ -n $DESTDIR ]]; then
                uninstall_system
            elif command -v sudo >/dev/null; then
                say "Root rights are needed to unmount and remove the system part."
                sudo -- /bin/bash "$SCRIPT" "${pass[@]}" || die "The system part was not removed and your files were left in place (idle shares may already be unmounted)"
            elif command -v pkexec >/dev/null; then
                pkexec /bin/bash "$SCRIPT" "${pass[@]}" || die "The system part was not removed and your files were left in place (idle shares may already be unmounted)"
            else
                die "Neither sudo nor pkexec is available for the system part"
            fi
            uninstall_user ""
            ;;
    esac
    if (( FAILED )); then
        say "Finished with warnings (see above)."
        exit 1
    fi
    say "Dolphin-Fast-Share-Mount is uninstalled."
}

main "$@"

# End of File #
