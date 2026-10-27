#!/bin/bash
#
# install.sh - install or repair Dolphin-Fast-Share-Mount.
#
# Run it as your desktop user (not with sudo). It installs the user part
# into your home folder and asks for root rights (sudo) once for the system
# part: the root helper and the polkit action.
#
# Running it again over an existing installation repairs it: every file is
# written again with the right mode and owner, and files that an older
# version installed but this one no longer uses are removed (listed in a
# manifest). A settings file you already have is never overwritten.
#
#   ./install.sh                  user part and system part
#   ./install.sh --user-only      user part only (the helper must already be installed)
#   sudo ./install.sh --system-only
#   ./install.sh --prefix /usr    system part under /usr instead of /usr/local
#
# DESTDIR=/some/dir stages only the system part below that folder (for
# packaging and tests): no sudo, no ownership changes, no polkit check.
#
# SPDX-License-Identifier: GPL-3.0-or-later

VERSION=20261026

set -euo pipefail
umask 022

APP_ID=dolphin-fast-share-mount
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFIX=/usr/local
DESTDIR=${DESTDIR:-}
POLICY_NAME=io.github.redbearak.dolphin-fast-share-mount.policy
POLKIT_ACTION=io.github.redbearak.dolphin-fast-share-mount.mount

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- file helpers ---------------------------------------------------------------

# A path is safe to put into sed replacement text and a .desktop Exec line
# (where "%" would start a field code).
check_plain_path() {
    local path=$1
    [[ $path == /* ]] || die "Not an absolute path: $path"
    [[ $path != *[\"\\\`\$\&\|\;%$'\n'$'\t']* ]] \
        || die "The path contains characters this installer does not handle: $path"
}

ensure_dir() {
    local dir=$1
    if [[ -L $dir && ! -d $dir ]]; then
        die "$dir is a broken symbolic link; remove it and run the installer again"
    fi
    if [[ -e $dir && ! -d $dir ]]; then
        die "$dir exists but is not a folder; move it away and run the installer again"
    fi
    mkdir -p -- "$dir"
}

# install_file SOURCE TARGET MODE [@KEY@ value]...
# Writes TARGET atomically (temporary file in the same folder, then rename),
# replacing whatever was there: a damaged file, a wrong mode, a symbolic link.
install_file() {
    local source=$1 target=$2 mode=$3
    shift 3
    local dir=${target%/*} tmp expression=""
    ensure_dir "$dir"
    if [[ -d $target && ! -L $target ]]; then
        die "$target is a folder; move it away and run the installer again"
    fi
    while (( $# >= 2 )); do
        check_plain_path "$2"
        expression+="s|$1|$2|g;"
        shift 2
    done
    tmp=$(mktemp -- "$dir/.${target##*/}.XXXXXX")
    if [[ -n $expression ]]; then
        sed -e "$expression" -- "$source" > "$tmp" || { rm -f -- "$tmp"; die "Could not write $target"; }
    else
        cat -- "$source" > "$tmp" || { rm -f -- "$tmp"; die "Could not write $target"; }
    fi
    chmod "$mode" -- "$tmp"
    mv -fT -- "$tmp" "$target"
    INSTALLED+=("$target")
}

# prune_old_files MANIFEST STAGING FOLDER...
# Remove files listed in an older manifest that this version did not install.
# Manifest entries are the installed paths without the staging folder
# (DESTDIR). Only plain files below the given folders are touched.
prune_old_files() {
    local manifest=$1 staging=$2 path keep kept root allowed
    shift 2
    [[ -f $manifest && ! -L $manifest ]] || return 0
    while IFS= read -r path; do
        [[ -n $path ]] || continue
        path=$staging$path
        keep=""
        for kept in "${INSTALLED[@]}"; do
            [[ $kept == "$path" ]] && keep=1 && break
        done
        [[ -z $keep ]] || continue
        allowed=""
        for root in "$@"; do
            [[ $path == "$root"/* && $path != *"/../"* ]] && allowed=1 && break
        done
        if [[ -n $allowed && ( -f $path || -L $path ) ]]; then
            rm -f -- "$path" && say "  removed old file $path"
        fi
    done < "$manifest"
}

# write_manifest MANIFEST STAGING: the installed paths, without STAGING.
write_manifest() {
    local manifest=$1 staging=$2 tmp
    ensure_dir "${manifest%/*}"
    tmp=$(mktemp -- "${manifest%/*}/.manifest.XXXXXX")
    printf '%s\n' "${INSTALLED[@]#"$staging"}" > "$tmp"
    chmod 0644 -- "$tmp"
    mv -fT -- "$tmp" "$manifest"
}

# ---- system part -------------------------------------------------------------------

install_system() {
    local root=$DESTDIR
    if [[ -z $root ]]; then
        (( EUID == 0 )) || die "The system part needs root: sudo $0 --system-only"
    fi
    check_plain_path "$PREFIX"
    local helper="$PREFIX/libexec/fast-share-mount-helper"
    local policy="/usr/share/polkit-1/actions/$POLICY_NAME"
    local share_dir="$PREFIX/share/$APP_ID"
    local manifest="$share_dir/installed-files.txt"

    say "Installing the system part under ${root:-}$PREFIX"
    INSTALLED=()
    install_file "$SOURCE_DIR/libexec/fast-share-mount-helper" "$root$helper" 0755
    install_file "$SOURCE_DIR/polkit/$POLICY_NAME.in" "$root$policy" 0644 @HELPER@ "$helper"
    install_file "$SOURCE_DIR/uninstall.sh" "$root$share_dir/uninstall.sh" 0755
    INSTALLED+=("$root$manifest")
    prune_old_files "$root$manifest" "$root" "$root$PREFIX/libexec" "$root/usr/share/polkit-1/actions" "$root$share_dir"
    write_manifest "$root$manifest" "$root"

    if [[ -z $root ]]; then
        local path dir
        for path in "${INSTALLED[@]}"; do
            chown root:root -- "$path"
        done
        # The user side only starts a helper that no one but root can change,
        # in folders no one but root can change.
        dir=$(readlink -f -- "${helper%/*}")
        while :; do
            if [[ $(stat -c %u -- "$dir") != 0 ]] || (( 0$(stat -c %a -- "$dir") & 022 )); then
                warn "$dir can be changed by a user other than root; the helper will refuse to run from there"
            fi
            [[ $dir != / ]] || break
            dir=${dir%/*}
            dir=${dir:-/}
        done
        if command -v pkaction >/dev/null; then
            if pkaction --action-id "$POLKIT_ACTION" >/dev/null 2>&1; then
                say "  polkit action $POLKIT_ACTION is registered"
            else
                warn "polkit does not list $POLKIT_ACTION yet; it usually appears within seconds (check: pkaction --action-id $POLKIT_ACTION)"
            fi
        else
            warn "pkaction not found; polkit may be missing (sudo with ksshaskpass is the fallback)"
        fi
        if [[ $PREFIX != /usr && -e /usr/libexec/fast-share-mount-helper ]]; then
            warn "/usr/libexec/fast-share-mount-helper also exists (a distribution package?) and is used first"
        fi
    fi
    say "System part done."
}

# ---- user part ----------------------------------------------------------------------

install_user() {
    (( EUID != 0 )) || die "Run the installer as your desktop user, not with sudo"
    [[ -n ${HOME:-} && -d $HOME ]] || die "HOME is not set to a folder"
    local data=${XDG_DATA_HOME:-$HOME/.local/share}
    local config=${XDG_CONFIG_HOME:-$HOME/.config}
    local bin_dir=$HOME/.local/bin
    local command="$bin_dir/fast-share-mount"
    local own_dir="$data/$APP_ID"
    local manifest="$own_dir/installed-files.txt"
    check_plain_path "$command"

    if [[ ! -x /usr/bin/python3 ]]; then
        die "/usr/bin/python3 is missing; install your distribution's python3 package"
    fi
    if ! /usr/bin/python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        die "/usr/bin/python3 is older than 3.10"
    fi

    say "Installing the user part for $(id -un)"
    INSTALLED=()
    install_file "$SOURCE_DIR/bin/fast-share-mount" "$command" 0755
    local menu
    for menu in dolphin-fast-share-mount.desktop dolphin-fast-share-mount-unmount.desktop; do
        # Service menus in this folder must be executable (KF 5.85 and later).
        install_file "$SOURCE_DIR/servicemenus/$menu" "$data/kio/servicemenus/$menu" 0755 @BIN@ "$command"
    done
    install_file "$SOURCE_DIR/uninstall.sh" "$own_dir/uninstall.sh" 0755
    INSTALLED+=("$manifest")
    prune_old_files "$manifest" "" "$bin_dir" "$data/kio/servicemenus" "$own_dir"
    write_manifest "$manifest" ""

    local settings="$config/$APP_ID.conf"
    if [[ -L $settings || -e $settings ]]; then
        say "  kept your settings in $settings"
    else
        ensure_dir "$config"
        cp -- "$SOURCE_DIR/config/$APP_ID.conf.example" "$settings"
        chmod 0644 -- "$settings"
        say "  settings template written to $settings"
    fi

    case ":${PATH:-}:" in
        *":$bin_dir:"*) ;;
        *) say "  note: $bin_dir is not in PATH; the Dolphin menu works anyway, the terminal command needs the full path" ;;
    esac
    say "User part done."
}

# ---- main ----------------------------------------------------------------------------

main() {
    local mode=both
    while (( $# )); do
        case $1 in
            --user-only) mode=user; shift ;;
            --system-only) mode=system; shift ;;
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
        *) die "--prefix must be /usr or /usr/local (polkit and the user side look there)" ;;
    esac

    case $mode in
        system)
            install_system
            ;;
        user)
            install_user
            ;;
        both)
            (( EUID != 0 )) || die "Run the installer as your desktop user, not with sudo (it asks for root itself)"
            if [[ -n $DESTDIR ]]; then
                say "DESTDIR is set: staging the system part only."
                install_system
                return
            elif command -v sudo >/dev/null; then
                say "Root rights are needed for the system part (helper and polkit action)."
                sudo -- /bin/bash "$SOURCE_DIR/install.sh" --system-only --prefix "$PREFIX" \
                    || die "The system part was not installed"
            elif command -v pkexec >/dev/null; then
                pkexec /bin/bash "$SOURCE_DIR/install.sh" --system-only --prefix "$PREFIX" \
                    || die "The system part was not installed"
            else
                die "Neither sudo nor pkexec is available for the system part"
            fi
            install_user
            say "Installed. Right-click a folder in an smb:// location in Dolphin: Fast share mount."
            say "Check the setup with: fast-share-mount check"
            ;;
    esac
}

main "$@"

# End of File #
