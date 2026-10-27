"""Tests for libexec/fast-share-mount-helper.

The helper is sourced into bash so its functions can be called one by one.
The mount command itself runs against a stub mount.cifs and a temporary
mount tree, so the full flow (arguments, password pipe, error mapping,
mount point choice) is exercised without root.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

from support import FIXTURES, HELPER, load_user_side

user_side = load_user_side()

# logger is replaced so the sourced-function tests do not write to the system
# journal. CommandLineTests runs the helper as a program, which does log.
PRELUDE = 'source "$HELPER"; logger() { :; }; '


def bash(script: str, *args: str, stdin: str = "", env: dict | None = None):
    full_env = dict(os.environ, HELPER=HELPER)
    if env:
        full_env.update(env)
    return subprocess.run(["bash", "-c", PRELUDE + script, "test", *args],
                          input=stdin, capture_output=True, text=True, env=full_env,
                          timeout=60, check=False)


def valid(function: str, value: str) -> bool:
    return bash(f'{function} "$1"', value).returncode == 0


HOSTS = ["nas", "photos.example.net", "192.168.1.5", "a-b.example", "NAS", "x" * 63,
         ".nas", "nas.", "a..b", "-nas", "nas-", "na_s", "na s", "", "x" * 64,
         ".".join(["a" * 63] * 4), "host;rm", "host/x", "fe80::1", "a.-b"]
SHARES = ["Shared", "My Share", "C$", "Fotos Österreich", "x" * 255, "x" * 256, "",
          "a/b", "a\\b", "-x", ".", "..", "...", "tab\there", "new\nline", "del\x7f",
          "comma,share", "x,sec=krb5,cruid=0", "a=b", "dot.share", "Ünïcödé" * 40,
          "Ö" * 127, "Ö" * 128, "Ö" * 200]
USERS = ["kris", "jordan", "first.last", "user@example.com", "machine$", "John Smith",
         " lead", "trail ", "", "a,b", "a=b", "a%b", "dom\\user", "a/b", "x" * 64, "x" * 65,
         "-dash", "Ünï"]
DOMAINS = ["WORKGROUP", "corp.example", "", "-x", "a" * 64, "a" * 65, "a b", "a;b"]


class ValidationTests(unittest.TestCase):
    """The user side pre-checks with the same rules; both must agree."""

    def check_agreement(self, function, python_check, values):
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(valid(function, value), python_check(value))

    def test_hosts_agree(self):
        self.check_agreement("is_valid_host", user_side.is_valid_host, HOSTS)

    def test_shares_agree(self):
        self.check_agreement("is_valid_share", user_side.is_valid_share, SHARES)

    def test_users_agree(self):
        self.check_agreement("is_valid_user", user_side.is_valid_user, USERS)

    def test_domains_agree(self):
        self.check_agreement("is_valid_domain", user_side.is_valid_domain, DOMAINS)

    def test_expected_verdicts(self):
        self.assertTrue(valid("is_valid_host", "photos.example.net"))
        self.assertFalse(valid("is_valid_host", "fe80::1"))
        self.assertTrue(valid("is_valid_share", "My Share"))
        self.assertFalse(valid("is_valid_share", ".."))
        self.assertFalse(valid("is_valid_share", "x,sec=krb5,cruid=0"))
        self.assertTrue(valid("is_valid_share", "Ö" * 127))
        self.assertFalse(valid("is_valid_share", "Ö" * 128), "the limit is 255 bytes")
        self.assertTrue(valid("is_valid_user", "John Smith"))
        self.assertFalse(valid("is_valid_user", "a,b"))
        self.assertFalse(valid("is_valid_user", "a%b"))

    def test_uid_port_version(self):
        for value, expected in (("1000", True), ("0", False), ("01000", False), ("-1", False),
                                ("1" * 11, False), ("abc", False), ("", False)):
            with self.subTest(uid=value):
                self.assertEqual(valid("is_valid_uid", value), expected)
        for value, expected in (("445", True), ("65535", True), ("65536", False), ("0", False),
                                ("44a", False), ("", False)):
            with self.subTest(port=value):
                self.assertEqual(valid("is_valid_port", value), expected)
        for value, expected in (("3.1.1", True), ("1.0", True), ("default", True),
                                ("4.0", False), ("3.1.1,uid=0", False)):
            with self.subTest(vers=value):
                self.assertEqual(valid("is_valid_vers", value), expected)


class NameTests(unittest.TestCase):
    def name(self, value):
        return bash('safe_name "$1"', value).stdout

    def test_safe_name(self):
        self.assertEqual(self.name("Shared"), "Shared")
        self.assertEqual(self.name("My Share"), "My_Share")
        self.assertEqual(self.name("C$"), "C_")
        self.assertEqual(self.name(".hidden"), "_hidden")
        self.assertEqual(self.name("photos.example.net"), "photos.example.net")
        self.assertEqual(self.name("Ö"), "__")
        self.assertEqual(len(self.name("x" * 200)), 100)

    def test_mountpoint_paths(self):
        cases = {
            "/run/dolphin-fast-share-mount/1000/nas/Shared": True,
            "/run/dolphin-fast-share-mount/1000/nas/Shared~2": True,
            "/run/dolphin-fast-share-mount/1000/nas": False,
            "/run/dolphin-fast-share-mount/1000/nas/Shared/sub": False,
            "/run/dolphin-fast-share-mount/1000/../1001/x": False,
            "/run/dolphin-fast-share-mount/1000/nas/..": False,
            "/run/dolphin-fast-share-mount/1001/nas/Shared": False,
            "/run/dolphin-fast-share-mount/1000/n as/Shared": False,
            "/etc/passwd": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                result = bash('is_tool_mountpoint_path "$1" 1000', path)
                self.assertEqual(result.returncode == 0, expected)


class MountTableTests(unittest.TestCase):
    fixture = os.path.join(FIXTURES, "mountinfo.txt")

    def entry(self, mountpoint):
        return bash('mount_entry_at "$1" "$2" && printf "%s|%s|%s" "$ENTRY_SOURCE" '
                    '"$ENTRY_FSTYPE" "$ENTRY_OPTIONS"', self.fixture, mountpoint)

    def test_entry_found(self):
        result = self.entry("/run/dolphin-fast-share-mount/1000/photos.example.net/Shared")
        self.assertEqual(result.returncode, 0)
        source, fstype, options = result.stdout.split("|")
        self.assertEqual((source, fstype), ("//photos.example.net/Shared", "cifs"))
        self.assertIn("vers=3.1.1", options)

    def test_entry_with_escaped_space_and_no_optional_fields(self):
        result = self.entry("/run/dolphin-fast-share-mount/1000/nas/My_Share")
        self.assertEqual(result.stdout.split("|")[0], "//nas/My\\040Share")
        compare = bash('same_source "$(mountinfo_escape "$1")" "$2"', "//nas/My Share",
                       "//NAS/my\\040share")
        self.assertEqual(compare.returncode, 0)

    def test_entry_missing(self):
        self.assertNotEqual(self.entry("/run/dolphin-fast-share-mount/1000/nas").returncode, 0)

    def test_cifs_mounts_under(self):
        result = bash('cifs_mounts_under "$1" "$2"', self.fixture, "/run/dolphin-fast-share-mount/1000")
        self.assertEqual(result.stdout.splitlines(), [
            "/run/dolphin-fast-share-mount/1000/photos.example.net/Shared",
            "/run/dolphin-fast-share-mount/1000/nas/My_Share",
        ])
        every = bash('cifs_mounts_under "$1" "$2"', self.fixture, "/run/dolphin-fast-share-mount")
        self.assertEqual(len(every.stdout.splitlines()), 4)

    def test_version_from_options(self):
        self.assertEqual(bash('smb_version_from_options "$1"', "rw,vers=3.0,uid=1").stdout, "3.0")
        self.assertEqual(bash('smb_version_from_options "$1"', "rw,uid=1").stdout, "unknown")


class ErrorTests(unittest.TestCase):
    def classify(self, status, err, kernel=""):
        result = bash('classify_mount_error "$1" "$2" "$3"; printf "%s" "$CLASS_CODE"',
                      str(status), err, kernel)
        return result.stdout

    def test_classification(self):
        self.assertEqual(self.classify(32, "mount error(13): Permission denied"), "EAUTH")
        self.assertEqual(self.classify(32, "mount error(2): No such file or directory"), "ENOSHARE")
        self.assertEqual(self.classify(32, "mount error(113): No route to host"), "EHOST")
        self.assertEqual(self.classify(32, "mount error(111): Connection refused"), "EHOST")
        self.assertEqual(self.classify(32, "mount error(112): Host is down"), "EHOST")
        self.assertEqual(self.classify(32, "mount error(112): Host is down",
                                       "CIFS: VFS: \\\\old Dialect not supported by server"),
                         "EDIALECT")
        self.assertEqual(self.classify(32, "mount error(95): Operation not supported"), "EDIALECT")
        self.assertEqual(self.classify(124, ""), "ETIMEOUT")
        self.assertEqual(self.classify(1, "mount error: could not resolve address for nas: "
                                          "Unknown error"), "EHOST")
        self.assertEqual(self.classify(32, "mount error(22): Invalid argument"), "EINTERNAL")

    def test_fail_line_and_status(self):
        result = bash('fail EAUTH "Login failed" "$1"', "detail with\ttab\nand newline")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(result.stderr, "EAUTH\tLogin failed\tdetail with tab | and newline\n")

    def test_password_length_limit(self):
        ok = bash('read_password; printf "%s" "${#PASSWORD}"', stdin="p" * 512 + "\n")
        self.assertEqual(ok.stdout, "512")
        too_long = bash("read_password", stdin="p" * 513 + "\n")
        self.assertEqual(too_long.returncode, 3)
        self.assertTrue(too_long.stderr.startswith("EINVALID\t"))

    def test_password_kept_exactly(self):
        secret = " lead, comma=equals %percent\\back 'quote\" trail "
        result = bash('read_password; printf "%s" "$PASSWORD"', stdin=secret + "\n")
        self.assertEqual(result.stdout, secret)


class CommandLineTests(unittest.TestCase):
    def run_helper(self, *args):
        return subprocess.run([HELPER, *args], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, check=False)

    def test_version(self):
        result = self.run_helper("--version")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"^\d{8}(\.\d+)?\n$")

    def test_usage_and_unknown_arguments(self):
        self.assertEqual(self.run_helper().returncode, 2)
        self.assertEqual(self.run_helper("mount", "--bogus").returncode, 2)
        self.assertEqual(self.run_helper("mount", "--uid").returncode, 2)

    @unittest.skipIf(os.geteuid() == 0, "runs as root")
    def test_refuses_without_root(self):
        result = self.run_helper("mount", "--uid", str(os.getuid()), "--host", "nas",
                                 "--share", "x", "--user", "kris")
        self.assertEqual(result.returncode, 17)
        self.assertTrue(result.stderr.startswith("EPERMS\t"))


STUB_MOUNT_CIFS = textwrap.dedent("""\
    #!/bin/bash
    printf '%s\\n' "$@" > "$STUB_DIR/argv"
    env > "$STUB_DIR/env"
    if [[ -n ${PASSWD_FD:-} ]]; then
        IFS= read -r -u "$PASSWD_FD" password || true
        printf '%s' "$password" > "$STUB_DIR/password"
    fi
    case ${STUB_MODE:-ok} in
        ok)
            printf '200 30 0:99 / %s rw,nosuid,nodev - cifs %s rw,vers=3.1.1,username=x\\n' \\
                "$2" "${1// /\\\\040}" >> "$STUB_MOUNTINFO"
            ;;
        auth) echo "mount error(13): Permission denied" >&2; exit 32 ;;
        noshare) echo "mount error(2): No such file or directory" >&2; exit 32 ;;
        down) echo "mount error(112): Host is down" >&2; exit 32 ;;
        silent) ;;
    esac
    """)


class MountFlowTests(unittest.TestCase):
    """cmd_mount with root-only steps replaced and mount.cifs stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.base = os.path.join(self.dir, "base")
        self.stub_dir = os.path.join(self.dir, "stub")
        os.makedirs(self.stub_dir)
        stub = os.path.join(self.stub_dir, "mount.cifs")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB_MOUNT_CIFS)
        os.chmod(stub, 0o755)
        self.mountinfo = os.path.join(self.dir, "mountinfo")
        with open(self.mountinfo, "w", encoding="utf-8") as handle:
            handle.write("22 1 0:21 / / rw - btrfs /dev/sda2 rw\n")

    def tearDown(self):
        self.tmp.cleanup()

    def mount(self, *args, stdin="secret\n", mode="ok", kernel="", extra=""):
        script = (
            'MOUNT_BASE="$BASE"; MOUNTINFO="$STUB_MOUNTINFO"; PATH="$STUB_DIR:$PATH"; '
            'require_root() { :; }; '
            'check_target_uid() { TARGET_GID=100; }; '
            'ensure_root_dir() { mkdir -p -- "$1"; }; '
            'ensure_user_root() { mkdir -p -- "$MOUNT_BASE/$1"; : > "$STUB_DIR/acl-set"; }; '
            'kernel_cifs_lines() { printf "%s" "$KERNEL"; }; '
            + extra +
            'cmd_mount "$@"'
        )
        env = {"BASE": self.base, "STUB_DIR": self.stub_dir, "STUB_MOUNTINFO": self.mountinfo,
               "STUB_MODE": mode, "KERNEL": kernel,
               # Set by a careless caller; mount.cifs prefers them over PASSWD_FD.
               "PASSWD": "leaked-from-env", "PASSWD_FILE": "/etc/shadow"}
        return bash(script, *args, stdin=stdin, env=env)

    def read(self, name):
        with open(os.path.join(self.stub_dir, name), encoding="utf-8") as handle:
            return handle.read()

    def test_success_passes_password_only_through_the_pipe(self):
        secret = " p@ss, word=1 %x\\y "
        result = self.mount("--uid", "1000", "--host", "NAS", "--share", "My Share",
                            "--user", "kris", stdin=secret + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        mountpoint = os.path.join(self.base, "1000", "nas", "My_Share")
        self.assertEqual(result.stdout, f"OK\t{mountpoint}\t3.1.1\n")
        self.assertEqual(self.read("password"), secret)
        argv = self.read("argv").splitlines()
        self.assertEqual(argv[:2], ["//nas/My Share", mountpoint])
        self.assertEqual(argv[2], "-o")
        options = argv[3].split(",")
        for expected in ("uid=1000", "gid=100", "forceuid", "forcegid", "noperm", "nosuid",
                         "nodev", "username=kris"):
            self.assertIn(expected, options)
        self.assertNotIn(secret, self.read("argv"))
        self.assertFalse(any(o.startswith(("password", "pass=")) for o in options))
        env = self.read("env").splitlines()
        self.assertFalse(any(secret in line for line in env))
        self.assertFalse(any(line.startswith(("PASSWD=", "PASSWD_FILE=")) for line in env))
        self.assertTrue(os.path.exists(os.path.join(self.stub_dir, "acl-set")),
                        "cmd_mount did not set up the user's folder")

    def test_guest_reads_no_password(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "Public", "--guest",
                            stdin="should-not-be-read\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.read("argv").splitlines()[3].split(",")
        self.assertIn("guest", options)
        self.assertFalse(any(o.startswith("username=") for o in options))
        self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "password")))

    def test_optional_settings(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            "--domain", "WORKGROUP", "--port", "4445", "--vers", "3.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.read("argv").splitlines()[3].split(",")
        for expected in ("domain=WORKGROUP", "port=4445", "vers=3.0"):
            self.assertIn(expected, options)

    def test_wrong_password_is_reported_and_cleaned_up(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            mode="auth")
        self.assertEqual(result.returncode, 10)
        self.assertTrue(result.stderr.startswith("EAUTH\t"))
        self.assertFalse(os.path.exists(os.path.join(self.base, "1000", "nas", "x")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "1000", "nas")))

    def test_missing_share_and_smb1_server(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            mode="noshare")
        self.assertEqual(result.returncode, 11)
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            mode="down", kernel="CIFS: VFS: Dialect not supported by server")
        self.assertEqual(result.returncode, 13)
        self.assertTrue(result.stderr.startswith("EDIALECT\t"))

    def test_already_mounted_same_share(self):
        first = self.mount("--uid", "1000", "--host", "nas", "--share", "Media", "--user", "kris")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.mount("--uid", "1000", "--host", "NAS", "--share", "media", "--user", "kris")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith("ALREADY\t"))

    def test_name_collision_gets_a_suffix(self):
        first = self.mount("--uid", "1000", "--host", "nas", "--share", "My Share", "--user", "kris")
        second = self.mount("--uid", "1000", "--host", "nas", "--share", "My_Share", "--user", "kris")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.split("\t")[1].endswith("/nas/My_Share~2"))

    def test_non_ascii_share_in_other_case_is_already_mounted(self):
        first = self.mount("--uid", "1000", "--host", "nas", "--share", "Österreich", "--user", "kris")
        second = self.mount("--uid", "1000", "--host", "nas", "--share", "österreich", "--user", "kris")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(second.stdout.startswith("ALREADY\t"), second.stdout + second.stderr)

    def test_unreadable_mount_is_detached_and_reported(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            extra="probe_readable() { return 1; }; umount() { :; }; ")
        self.assertEqual(result.returncode, 12)
        self.assertIn("cannot be read", result.stderr)

    def test_missing_mount_after_success_is_reported(self):
        result = self.mount("--uid", "1000", "--host", "nas", "--share", "x", "--user", "kris",
                            mode="silent")
        self.assertEqual(result.returncode, 20)
        self.assertIn("no CIFS mount is present", result.stderr)

    def test_invalid_input_rejected_before_mounting(self):
        for args in (("--host", "na s", "--share", "x"), ("--host", "nas", "--share", ".."),
                     ("--host", "nas", "--share", "x,sec=krb5,cruid=0"),
                     ("--host", "nas", "--share", "x", "--port", "99999"),
                     ("--host", "nas", "--share", "x", "--vers", "3.1.1,uid=0")):
            with self.subTest(args=args):
                result = self.mount("--uid", "1000", "--user", "kris", *args)
                self.assertEqual(result.returncode, 3)
                self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "argv")))


class DirectoryTests(unittest.TestCase):
    def test_refuses_symlink_and_foreign_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            link = os.path.join(folder, "link")
            os.symlink("/etc", link)
            result = bash('ensure_root_dir "$1" 0755', link)
            self.assertEqual(result.returncode, 20)
            self.assertIn("symbolic link", result.stderr)
            if os.geteuid() != 0:
                owned = os.path.join(folder, "mine")
                os.mkdir(owned)
                result = bash('ensure_root_dir "$1" 0755', owned)
                self.assertEqual(result.returncode, 20)
                self.assertIn("not owned by root", result.stderr)

    def test_helper_file_is_executable(self):
        self.assertTrue(os.stat(HELPER).st_mode & stat.S_IXUSR)


class CallerTests(unittest.TestCase):
    """Rules that only the integration test exercised before."""

    def test_pkexec_caller_must_be_the_target_user(self):
        uid = str(os.getuid())
        other = str(os.getuid() + 1)
        refused = bash('check_target_uid "$1"', uid, env={"PKEXEC_UID": other})
        self.assertEqual(refused.returncode, 17)
        self.assertIn("own mounts", refused.stderr)
        allowed = bash('check_target_uid "$1" && printf "%s" "$TARGET_GID"', uid,
                       env={"PKEXEC_UID": uid})
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, str(os.getgid()))

    def test_every_user_refused_under_pkexec(self):
        result = bash('require_root() { :; }; cmd_unmount_all --every-user',
                      env={"PKEXEC_UID": str(os.getuid())})
        self.assertEqual(result.returncode, 17)

    def test_missing_setfacl_leaves_the_folder_alone(self):
        result = bash('PATH=/nonexistent; hash -r; '
                      'ensure_root_dir() { echo "touched $1" >&2; }; ensure_user_root 1000')
        self.assertEqual(result.returncode, 16)
        self.assertNotIn("touched", result.stderr)

    def test_access_list_is_set_in_one_step_without_chmod(self):
        result = bash('ensure_root_dir() { echo "dir $*"; }; '
                      'exec 9>&1; setfacl() { echo "setfacl $*" >&9; }; ensure_user_root 1000')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "dir /run/dolphin-fast-share-mount 0755",
            "dir /run/dolphin-fast-share-mount/1000 0700 keep",
            "setfacl --set u::rwx,g::---,o::---,u:1000:r-x,m::r-x -- "
            "/run/dolphin-fast-share-mount/1000",
        ])

    def test_keep_skips_chmod(self):
        if os.geteuid() == 0:
            self.skipTest("runs as root")
        result = bash('stat() { echo 0; }; chown() { :; }; chmod() { echo "chmod $*"; }; '
                      'ensure_root_dir "$1" 0700 keep; ensure_root_dir "$1" 0755', tempfile.gettempdir())
        self.assertEqual(result.stdout.splitlines(), [f"chmod 0755 -- {tempfile.gettempdir()}"])


class UnmountTests(unittest.TestCase):
    def test_busy_and_timeout(self):
        busy = bash('umount() { echo "umount: $2: target is busy." >&2; return 32; }; '
                    'timeout() { shift; "$@"; }; '
                    'unmount_one /x "" || printf "%s" "$UNMOUNT_CODE"')
        self.assertEqual(busy.stdout, "EBUSY")
        slow = bash('timeout() { return 124; }; unmount_one /x "" || printf "%s" "$UNMOUNT_CODE"')
        self.assertEqual(slow.stdout, "ETIMEOUT")

    def test_lazy_passes_l(self):
        result = bash('exec 9>&1; timeout() { shift; "$@"; }; umount() { echo "umount $*" >&9; }; '
                      'remove_if_empty() { :; }; unmount_one /x 1')
        self.assertIn("umount -l -- /x", result.stdout)

    def test_path_outside_the_tree_is_refused(self):
        for path in ("/etc", "/run/dolphin-fast-share-mount/1000/nas",
                     "/run/dolphin-fast-share-mount/1000/../1001/nas/x"):
            with self.subTest(path=path):
                result = bash('require_root() { :; }; check_target_uid() { :; }; '
                              'umount() { echo "UMOUNT CALLED" >&2; }; '
                              'cmd_unmount --uid 1000 --path "$1"', path)
                self.assertEqual(result.returncode, 3)
                self.assertNotIn("UMOUNT CALLED", result.stderr)

    def test_not_mounted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/1000/nas/x"
            os.makedirs(path)
            result = bash('MOUNT_BASE="$1"; MOUNTINFO="$2"; require_root() { :; }; '
                          'check_target_uid() { :; }; cmd_unmount --uid 1000 --path "$3"',
                          folder, os.path.join(FIXTURES, "mountinfo.txt"), path)
            self.assertEqual(result.returncode, 15)
            self.assertFalse(os.path.exists(path), "empty leftover folder not removed")

    def test_unmount_all_removes_only_empty_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(f"{folder}/1000/nas/Empty")
            os.makedirs(f"{folder}/1000/nas/Kept")
            with open(f"{folder}/1000/nas/Kept/file", "w", encoding="utf-8") as handle:
                handle.write("x")
            os.makedirs(f"{folder}/1000/other/Gone")
            result = bash('MOUNT_BASE="$1"; MOUNTINFO=/dev/null; require_root() { :; }; '
                          'check_target_uid() { :; }; cmd_unmount_all --uid 1000', folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(f"{folder}/1000/nas/Empty"))
            self.assertFalse(os.path.exists(f"{folder}/1000/other"))
            self.assertTrue(os.path.exists(f"{folder}/1000/nas/Kept/file"))


class KernelLineTests(unittest.TestCase):
    def test_other_servers_are_left_out(self):
        lines = ("CIFS: VFS: \\\\otherbox Send error in SessSetup = -13\n"
                 "CIFS: VFS: \\\\nas-hr.example Send error in SessSetup = -13\n"
                 "CIFS: Attempting to mount //hr-secrets.example/Payroll\n"
                 "CIFS: VFS: \\\\NAS Dialect not supported\n"
                 "CIFS: VFS: cifs_mount failed w/return code = -112\n"
                 "usb 1-1: new device\n")
        result = bash('timeout() { shift; "$@"; }; journalctl() { printf "%s" "$LINES"; }; '
                      'kernel_cifs_lines 0 nas', env={"LINES": lines})
        self.assertEqual(result.stdout.splitlines(), [
            "CIFS: VFS: \\\\NAS Dialect not supported",
            "CIFS: VFS: cifs_mount failed w/return code = -112",
        ])


class KernelLineExitTests(unittest.TestCase):
    def test_trailing_line_about_another_server_does_not_stop_the_helper(self):
        lines = "CIFS: VFS: \\\\nas Dialect not supported\nCIFS: VFS: \\\\otherbox Send error\n"
        result = bash('timeout() { shift; "$@"; }; journalctl() { printf "%s" "$LINES"; }; '
                      'kernel=$(kernel_cifs_lines 0 nas); printf "reached|%s" "$kernel"',
                      env={"LINES": lines})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "reached|CIFS: VFS: \\\\nas Dialect not supported")


class LockTests(unittest.TestCase):
    def test_second_run_waits_and_gives_up(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(f"{folder}/1000")
            handle = os.open(f"{folder}/1000", os.O_RDONLY)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                result = bash('MOUNT_BASE="$1"; LOCK_WAIT=1; lock_user_root 1000', folder)
            finally:
                os.close(handle)
            self.assertEqual(result.returncode, 14)
            self.assertIn("still running", result.stderr)
            free = bash('MOUNT_BASE="$1"; LOCK_WAIT=1; lock_user_root 1000', folder)
            self.assertEqual(free.returncode, 0, free.stderr)


if __name__ == "__main__":
    unittest.main()

# End of File #
