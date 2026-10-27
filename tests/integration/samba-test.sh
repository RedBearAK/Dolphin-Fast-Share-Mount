#!/bin/bash
#
# Integration test: real kernel CIFS mounts against a Samba server in a
# container. Needs podman or docker, cifs-utils, and sudo (mounting needs
# root). It runs the helper from this checkout directly with sudo, so the
# installed copy is not involved, and it cleans up after itself.
#
#   tests/integration/samba-test.sh
#
# The container listens on 127.0.0.1:4445 (an unprivileged port, which also
# tests the --port option). Image: docker.io/dperson/samba, used only for its
# Samba binaries: the test writes its own smb.conf and accounts. smbd is
# copied to another path before it starts, because AppArmor attaches profiles
# by path and a host profile for /usr/sbin/smbd (openSUSE ships one) would
# confine the container's smbd to the host's shares: logins then succeed but
# opening the share fails, and the kernel reports mount error 13.
#
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HELPER="$ROOT/libexec/fast-share-mount-helper"
IMAGE=docker.io/dperson/samba:latest
NAME=fast-share-mount-test-samba
PORT=4445
TEST_USER=fsmtester
TEST_PASSWORD='correct horse, battery=staple'
UID_SELF=$(id -u)
BASE=/run/dolphin-fast-share-mount/$UID_SELF
PASSED=0
FAILED=0

engine=$(command -v podman || command -v docker || true)
[[ -n $engine ]] || { echo "podman or docker is needed"; exit 2; }
[[ -x /usr/sbin/mount.cifs || -x /sbin/mount.cifs ]] || { echo "cifs-utils is needed"; exit 2; }

cleanup() {
    sudo "$HELPER" unmount-all --uid "$UID_SELF" --lazy >/dev/null 2>&1 || true
    "$engine" rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

check() {
    local name=$1 expected=$2 actual=$3
    if [[ $actual == "$expected" ]]; then
        PASSED=$((PASSED + 1))
        echo "ok    $name"
    else
        FAILED=$((FAILED + 1))
        echo "FAIL  $name: expected '$expected', got '$actual'"
        [[ ! -s $TMP/last ]] || sed 's/^/      helper: /' "$TMP/last"
    fi
    : > "$TMP/last"
}

# stop here when a mount the remaining checks depend on did not happen
require_mount() {
    if ! mountpoint -q -- "$1"; then
        echo "FAIL  $1 is not mounted; stopping. Last Samba log lines:"
        "$engine" logs --tail 30 "$NAME" 2>&1 | sed 's/^/      samba: /'
        exit 1
    fi
}

# helper ARGS... with the password on stdin; prints "status|first stdout field|stderr code"
helper() {
    local password=$1 out err status=0
    shift
    out=$(printf '%s\n' "$password" | sudo "$HELPER" "$@" 2>"$TMP/err") || status=$?
    err=$(cut -f1 < "$TMP/err" | tail -n 1)
    cp -- "$TMP/err" "$TMP/last"
    printf '%s|%s|%s' "$status" "$(cut -f1 <<< "$out" | head -n 1)" "$err"
}

TMP=$(mktemp -d)
: > "$TMP/last"
trap 'cleanup; rm -rf -- "$TMP"' EXIT

# runs inside the container as sh -c SCRIPT sh USER PASSWORD
# shellcheck disable=SC2016
SAMBA_SETUP='set -eu
mkdir -p /srv/shared /srv/public /srv/locked
cat > /etc/samba/smb.conf <<CONF
[global]
   server role = standalone server
   map to guest = bad user
   guest account = nobody
   server min protocol = SMB2_10
   log level = 2
   load printers = no
   disable spoolss = yes
[Shared]
   path = /srv/shared
   read only = no
   valid users = $1
[Public]
   path = /srv/public
   read only = no
   guest ok = yes
[Locked]
   path = /srv/locked
   valid users = root
CONF
adduser -D -H "$1"
chown "$1" /srv/shared
chmod 0777 /srv/public
printf "%s\n%s\n" "$2" "$2" | smbpasswd -s -a "$1"
cp /usr/sbin/smbd /usr/local/bin/test-smbd
exec /usr/local/bin/test-smbd -FS --no-process-group </dev/null'

echo "Starting Samba ($IMAGE) on 127.0.0.1:$PORT"
"$engine" rm -f "$NAME" >/dev/null 2>&1 || true
"$engine" run -d --rm --name "$NAME" -p "127.0.0.1:$PORT:445" --entrypoint /bin/sh \
    "$IMAGE" -c "$SAMBA_SETUP" sh "$TEST_USER" "$TEST_PASSWORD" >/dev/null
# Rootless podman's port forwarder accepts connections before smbd runs, so
# an open port proves nothing: wait for smbd to say it is ready.
ready=""
for _ in $(seq 60); do
    if "$engine" logs "$NAME" 2>&1 | grep -q "daemon 'smbd' finished starting up"; then
        ready=1
        break
    fi
    sleep 1
done
if [[ -z $ready ]]; then
    echo "Samba did not start within 60 seconds. Its log:"
    "$engine" logs --tail 30 "$NAME" 2>&1 | sed 's/^/      samba: /'
    exit 2
fi

sudo -v

common=(--uid "$UID_SELF" --host 127.0.0.1 --port "$PORT")

check "wrong password" "10||EAUTH" "$(helper wrong mount "${common[@]}" --share Shared --user "$TEST_USER")"
check "no mount point left after failure" "absent" "$([[ -e $BASE/127.0.0.1/Shared ]] && echo present || echo absent)"
check "missing share" "11||ENOSHARE" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share Nope --user "$TEST_USER")"
check "share refused after a good login" "19||EDENIED" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share Locked --user "$TEST_USER")"
check "mount" "0|OK|" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share Shared --user "$TEST_USER")"
require_mount "$BASE/127.0.0.1/Shared"
check "mount again is ALREADY" "0|ALREADY|" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share shared --user "$TEST_USER")"
check "guest mount with a display name" "0|OK|" \
    "$(helper "" mount "${common[@]}" --share Public --guest --label "Test Box")"
require_mount "$BASE/127.0.0.1/Test_Box-Public"
check "sidebar name and icon recorded by mount(8)" "x-gvfs-name=Test%20Box-Public x-gvfs-icon=folder-remote" \
    "$(grep -F "TARGET=$BASE/127.0.0.1/Test_Box-Public " /run/mount/utab 2>/dev/null \
        | grep -o 'x-gvfs-[a-z]*=[^, ]*' | tr '\n' ' ' | sed 's/ $//')"

mountpoint="$BASE/127.0.0.1/Shared"
check "user owns the files" "$UID_SELF" "$(stat -c %u "$mountpoint")"
echo "written by the test" > "$mountpoint/test-file.txt"
check "write and read back" "written by the test" "$(cat "$mountpoint/test-file.txt")"
check "user folder owned by root, group bits show the ACL mask" "750 0" "$(sudo stat -c '%a %u' "$BASE")"
check "user ACL" "user::rwx user:$UID_SELF:r-x group::--- mask::r-x other::---" \
    "$(sudo getfacl -pn "$BASE" 2>/dev/null | grep -v '^#' | grep . | tr '\n' ' ' | sed 's/ $//')"
listing() { sudo runuser -u nobody -- ls -- "$1" >/dev/null 2>&1 && echo allowed || echo denied; }
check "other users can list the tree, not the user's folder" "allowed denied" \
    "$(listing "${BASE%/*}") $(listing "$BASE")"
vers=$(sed -n "s|.* $mountpoint .* - cifs .*vers=\([0-9.]*\).*|\1|p" /proc/self/mountinfo)
check "SMB 2.1 or later negotiated" "yes" "$([[ $vers =~ ^(2\.1|3) ]] && echo yes || echo "no ($vers)")"

exec 4<"$mountpoint/test-file.txt"
(cd "$mountpoint" && sleep 30) &
holder=$!
sleep 1
check "busy share refuses plain unmount" "14||EBUSY" "$(helper "" unmount --uid "$UID_SELF" --path "$mountpoint")"
kill "$holder" 2>/dev/null || true
wait "$holder" 2>/dev/null || true
exec 4<&-
check "unmount" "0|UNMOUNTED|" "$(helper "" unmount --uid "$UID_SELF" --path "$mountpoint")"
check "mount point removed" "absent" "$([[ -e $mountpoint ]] && echo present || echo absent)"
check "unmount when not mounted" "15||ENOTMOUNTED" "$(helper "" unmount --uid "$UID_SELF" --path "$mountpoint")"
check "path outside the tree refused" "3||EINVALID" "$(helper "" unmount --uid "$UID_SELF" --path /etc)"
status=0
sudo PKEXEC_UID=$((UID_SELF + 1)) "$HELPER" unmount --uid "$UID_SELF" --path "$mountpoint" \
    </dev/null >/dev/null 2>"$TMP/err" || status=$?
check "other uid refused under pkexec rules" "17|EPERMS" "$status|$(cut -f1 < "$TMP/err")"

check "mount for stale test" "0|OK|" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share Shared --user "$TEST_USER")"
"$engine" stop -t 1 "$NAME" >/dev/null
check "stale server: forced unmount" "0|UNMOUNTED|" "$(helper "" unmount --uid "$UID_SELF" --path "$mountpoint" --lazy)"
check "server gone: reported" "12||EHOST" "$(helper "$TEST_PASSWORD" mount "${common[@]}" --share Shared --user "$TEST_USER")"

check "unmount-all leaves an empty tree" "" "$(sudo "$HELPER" unmount-all --uid "$UID_SELF" --lazy >/dev/null 2>&1; sudo find "$BASE" -mindepth 1 2>/dev/null)"

echo "passed $PASSED, failed $FAILED"
(( FAILED == 0 ))

# End of File #
