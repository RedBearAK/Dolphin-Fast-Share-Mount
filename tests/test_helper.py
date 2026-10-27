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
import time
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

    # Kernel lines as journalctl printed them on openSUSE (kernel 7.2) for a
    # wrong password and for a share refused after a good login.
    SOURCE = "//127.0.0.1/Shared"
    WRONG_PASSWORD = ("CIFS: Attempting to mount //127.0.0.1/Shared\n"
                      "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n"
                      "CIFS: VFS: \\\\127.0.0.1 failed to create a new SMB session with Unknown: -13\n"
                      "CIFS: VFS: cifs_mount failed w/return code = -13")
    SHARE_REFUSED = ("CIFS: Attempting to mount //127.0.0.1/Shared\n"
                     "CIFS: VFS: cifs_mount failed w/return code = -13")
    OTHER = "CIFS: Attempting to mount (another share)"
    OPEN = "Login failed: wrong user name or password, or no access to this share"
    ERRORS = {13: "mount error(13): Permission denied", 5: "mount error(5): Input/output error",
              127: "mount error(127): Key has expired", 128: "mount error(128): Key has been revoked"}

    def sort_access(self, kernel, number=13, source=SOURCE):
        result = bash('classify_mount_error 32 "$1" "$2" "$3"; '
                      'printf "%s|%s" "$CLASS_CODE" "$CLASS_MESSAGE"',
                      self.ERRORS[number], kernel, source)
        return tuple(result.stdout.split("|", 1))

    @classmethod
    def with_status(cls, status):
        return cls.WRONG_PASSWORD.replace("0xc000006d STATUS_LOGON_FAILURE", status)

    def test_wrong_password_is_a_failed_login(self):
        self.assertEqual(self.sort_access(self.WRONG_PASSWORD),
                         ("EAUTH", "Login failed: wrong user name or password"))

    def test_failed_session_without_a_status_line_is_a_failed_login(self):
        kernel = self.WRONG_PASSWORD.replace(
            "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n", "")
        self.assertEqual(self.sort_access(kernel)[0], "EAUTH")

    def test_refused_share_after_a_good_login(self):
        self.assertEqual(self.sort_access(self.SHARE_REFUSED),
                         ("EDENIED", "The login worked, but the server refused access to the share"))

    def test_only_the_last_attempt_of_this_share_counts(self):
        # an earlier attempt of ours (another address of the server) failed
        # differently; without the slice its account status would win
        earlier = self.with_status("0xc0000234 STATUS_ACCOUNT_LOCKED_OUT")
        self.assertEqual(self.sort_access(earlier + "\n" + self.WRONG_PASSWORD),
                         ("EAUTH", "Login failed: wrong user name or password"))
        self.assertEqual(self.sort_access(self.WRONG_PASSWORD + "\n" + self.SHARE_REFUSED)[0],
                         "EDENIED")

    def test_anchor_is_the_whole_source_not_a_prefix(self):
        kernel = self.SHARE_REFUSED.replace("//127.0.0.1/Shared", "//127.0.0.1/SharedMore")
        self.assertEqual(self.sort_access(kernel), ("EAUTH", self.OPEN))

    def test_another_mount_in_the_window_keeps_the_message_open(self):
        locked_elsewhere = (self.OTHER + "\nCIFS: Status code returned 0xc0000234 "
                            "STATUS_ACCOUNT_LOCKED_OUT\n")
        # another server's account error after our attempt started
        mixed = self.WRONG_PASSWORD.replace("CIFS: VFS: cifs_mount", locked_elsewhere + "CIFS: VFS: cifs_mount")
        self.assertEqual(self.sort_access(mixed), ("EAUTH", self.OPEN))
        # another attempt that started just before ours, its status after our anchor
        mixed = self.OTHER + "\n" + self.SHARE_REFUSED.replace(
            "CIFS: VFS: cifs_mount", "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n"
                                     "CIFS: VFS: cifs_mount")
        self.assertEqual(self.sort_access(mixed), ("EAUTH", self.OPEN))
        # a refused share while another server's login fails
        self.assertEqual(self.sort_access(self.SHARE_REFUSED + "\n" + self.OTHER +
                                          "\nCIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE"),
                         ("EAUTH", self.OPEN))

    def test_attempts_before_ours_do_not_count(self):
        # without journalctl the lines come from all of dmesg
        earlier = self.OTHER + "\nCIFS: VFS: cifs_mount failed w/return code = -2\n"
        self.assertEqual(self.sort_access(earlier + self.WRONG_PASSWORD),
                         ("EAUTH", "Login failed: wrong user name or password"))
        self.assertEqual(self.sort_access(earlier + self.SHARE_REFUSED)[0], "EDENIED")

    def test_status_line_without_our_session_line_proves_nothing(self):
        # status lines name no server: this one may be another mount's reconnect
        for status in ("0xc000006d STATUS_LOGON_FAILURE", "0xc0000071 STATUS_PASSWORD_EXPIRED",
                       "0xc0000234 STATUS_ACCOUNT_LOCKED_OUT"):
            stray = self.SHARE_REFUSED.replace(
                "CIFS: VFS: cifs_mount", f"CIFS: Status code returned {status}\nCIFS: VFS: cifs_mount")
            self.assertEqual(self.sort_access(stray), ("EAUTH", self.OPEN), status)

    def test_refused_account_names_its_status(self):
        for status in ("STATUS_ACCOUNT_LOCKED_OUT", "STATUS_ACCOUNT_RESTRICTION",
                       "STATUS_INVALID_LOGON_HOURS", "STATUS_INVALID_WORKSTATION"):
            self.assertEqual(self.sort_access(self.with_status(f"0xc0000000 {status}")),
                             ("EDENIED", f"The server refused this account ({status})"))

    def test_expired_and_disabled_accounts_arrive_with_their_own_numbers(self):
        for number, status in ((127, "STATUS_PASSWORD_EXPIRED"), (127, "STATUS_ACCOUNT_EXPIRED"),
                               (128, "STATUS_ACCOUNT_DISABLED"), (5, "STATUS_PASSWORD_MUST_CHANGE")):
            kernel = self.with_status(f"0xc0000000 {status}").replace("-13", f"-{number}")
            self.assertEqual(self.sort_access(kernel, number),
                             ("EDENIED", f"The server refused this account ({status})"))
        # without usable kernel lines the number alone still says enough
        self.assertEqual(self.sort_access("", 127),
                         ("EDENIED", "The server refused this account "
                                     "(its password or the account has expired)"))
        self.assertEqual(self.sort_access("", 128),
                         ("EDENIED", "The server refused this account (the account is disabled)"))

    def test_input_output_error_is_an_account_error_only_when_the_status_says_so(self):
        kernel = self.with_status("0xc0000064 STATUS_NO_SUCH_USER").replace("-13", "-5")
        self.assertEqual(self.sort_access(kernel, 5),
                         ("EAUTH", "Login failed: wrong user name or password"))
        session_only = self.WRONG_PASSWORD.replace(
            "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n", "").replace("-13", "-5")
        self.assertEqual(self.sort_access(session_only, 5), ("EINTERNAL", "mount.cifs failed"))
        self.assertEqual(self.sort_access("", 5), ("EINTERNAL", "mount.cifs failed"))

    def test_incomplete_or_missing_kernel_lines_stay_open_to_both_causes(self):
        # the attempt's last line has not reached the journal yet
        self.assertEqual(self.sort_access("CIFS: Attempting to mount //127.0.0.1/Shared"),
                         ("EAUTH", self.OPEN))
        # no kernel lines at all (journal unreadable)
        self.assertEqual(self.sort_access(""), ("EAUTH", self.OPEN))
        # lines without the attempt's start (cut off) do not prove a refused share
        self.assertEqual(self.sort_access("CIFS: VFS: cifs_mount failed w/return code = -13"),
                         ("EAUTH", self.OPEN))
        # no source given: nothing can be anchored
        self.assertEqual(self.sort_access(self.SHARE_REFUSED, source=""), ("EAUTH", self.OPEN))

    def test_refused_share_exit_status(self):
        result = bash('fail EDENIED "x"')
        self.assertEqual(result.returncode, 19)

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

    def test_kernel_lines_are_read_from_below_the_second(self):
        before = time.time()
        result = self.mount("--uid", "1000", "--host", "NAS", "--share", "Media", "--user", "kris",
                            mode="auth",
                            extra='kernel_cifs_lines() { printf "%s" "$1" > "$STUB_DIR/since"; }; ')
        self.assertEqual(result.returncode, 10, result.stderr)
        since = self.read("since")
        self.assertRegex(since, r"^[0-9]+\.[0-9]{9}$")
        self.assertLessEqual(abs(float(since) - before), 5)

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
            "CIFS: Attempting to mount (another share)",
            "CIFS: VFS: \\\\NAS Dialect not supported",
            "CIFS: VFS: cifs_mount failed w/return code = -112",
        ])


class KernelLineAttemptTests(unittest.TestCase):
    def read(self, lines):
        return bash('timeout() { shift; "$@"; }; journalctl() { printf "%s" "$LINES"; }; '
                    'kernel_cifs_lines 0 nas', env={"LINES": lines}).stdout.splitlines()

    def test_another_servers_attempt_is_kept_without_its_name(self):
        lines = ("CIFS: Attempting to mount //nas/Media\n"
                 "CIFS: Attempting to mount //hr-secrets.example/Payroll\n"
                 "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n")
        self.assertEqual(self.read(lines), [
            "CIFS: Attempting to mount //nas/Media",
            "CIFS: Attempting to mount (another share)",
            "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE",
        ])

    def test_a_full_attempt_survives_noise_before_it(self):
        noise = "CIFS: VFS: cifs_mount failed w/return code = -2\n" * 20
        attempt = ("CIFS: No dialect specified on mount.\n"
                   "CIFS: Attempting to mount //nas/Media\n"
                   "CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE\n"
                   "CIFS: VFS: \\\\nas failed to create a new SMB session with Unknown: -13\n"
                   "CIFS: VFS: \\\\nas Send error in SessSetup = -13\n"
                   "CIFS: VFS: cifs_mount failed w/return code = -13\n")
        kept = self.read(noise + attempt)
        self.assertEqual(len(kept), 12)
        self.assertEqual(kept[-6:], attempt.splitlines())


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
