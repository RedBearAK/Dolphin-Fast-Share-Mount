"""Tests for bin/fast-share-mount (the user side).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from support import FIXTURES, USER_SIDE, load_user_side

fsm = load_user_side()


class UrlTests(unittest.TestCase):
    def test_plain(self):
        ref = fsm.parse_smb_url("smb://photos.example.net/Shared_Files")
        self.assertEqual((ref.host, ref.share, ref.subpath, ref.user, ref.domain, ref.port),
                         ("photos.example.net", "Shared_Files", (), None, None, None))
        self.assertEqual(ref.source, "//photos.example.net/Shared_Files")

    def test_user_domain_port_password_ignored(self):
        ref = fsm.parse_smb_url("smb://CORP;jordan:secret@NAS.local:4445/Media/Films/2024")
        self.assertEqual((ref.host, ref.share, ref.subpath, ref.user, ref.domain, ref.port),
                         ("nas.local", "Media", ("Films", "2024"), "jordan", "CORP", 4445))
        self.assertNotIn("secret", repr(ref))

    def test_backslash_domain_and_percent_encoding(self):
        ref = fsm.parse_smb_url("smb://CORP%5Cjordan@nas/My%20Share/Sub%20Folder/")
        self.assertEqual((ref.domain, ref.user, ref.share, ref.subpath),
                         ("CORP", "jordan", "My Share", ("Sub Folder",)))

    def test_rejections(self):
        cases = {
            "smb://nas/": "server or workgroup",
            "smb://WORKGROUP": "server or workgroup",
            "smb:///Share": "no server",
            "smb://[fe80::1]/Share": "IPv6",
            "file:///home": "Not an smb",
            "smb://nas/Share/../etc": "'..'",
            "smb://nas:99999/Share": "Invalid port",
            "smb://nas:44x/Share": "Invalid port",
            "smb://bad%2Cuser@nas/Share": "User name not supported",
            "smb://na_s/Share": "Server name not supported",
            "smb://nas/%2E%2E": "Share name not supported",
            "smb://nas/x%2Csec%3Dkrb5%2Ccruid%3D0/sub": "Share name not supported",
            "smb://nas:\u00b2/Share": "Invalid port",
            "smb://nas:\u0664\u0664\u0665/Share": "Invalid port",
            "smb://nas/Share/a%2F..%2F..%2Fetc": "encoded '/'",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(fsm.UsageError) as caught:
                    fsm.parse_smb_url(url)
                self.assertIn(expected, str(caught.exception))

    def test_password_not_echoed_in_errors(self):
        with self.assertRaises(fsm.UsageError) as caught:
            fsm.parse_smb_url("ftp://jordan:hunter2@nas/Share")
        self.assertNotIn("hunter2", str(caught.exception))

    def test_key_is_case_insensitive(self):
        self.assertEqual(fsm.parse_smb_url("smb://NAS/Media").key,
                         fsm.parse_smb_url("smb://nas/media").key)

    def test_redact(self):
        self.assertEqual(fsm.redact("smb://jordan:hunter2@nas/x"), "smb://jordan:***@nas/x")
        self.assertEqual(fsm.redact("username=k,password=hunter2,uid=1"),
                         "username=k,password=***,uid=1")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_values(self):
        config, warnings = fsm.parse_config("user=jordan\nopen_mode=window\n# comment\n\n")
        self.assertEqual(config["user"], "jordan")
        self.assertEqual(config["open_mode"], "window")
        self.assertEqual(config["remember_password"], "ask")
        self.assertEqual(warnings, [])

    def test_problems_are_reported(self):
        text = "open_mode=sideways\nbogus=1\nno equals here\nuser=a\nuser=b\nuser=bad,name\n"
        config, warnings = fsm.parse_config(text)
        self.assertEqual(config["open_mode"], "tab")
        self.assertEqual(config["user"], "b")
        joined = "\n".join(warnings)
        for expected in ("line 1: 'sideways' is not valid", "line 2: unknown setting 'bogus'",
                         "line 3: no '='", "line 5: 'user' set again",
                         "line 6: user name 'bad,name' not supported"):
            self.assertIn(expected, joined)

    def test_shell_syntax_is_rejected_as_a_value(self):
        config, warnings = fsm.parse_config("user=$(id)\n")
        self.assertEqual(config["user"], "")
        self.assertIn("not supported", warnings[0])

    def test_example_file_parses_cleanly(self):
        example = os.path.join(os.path.dirname(FIXTURES), "..", "config",
                               "dolphin-fast-share-mount.conf.example")
        with open(example, encoding="utf-8") as handle:
            config, warnings = fsm.parse_config(handle.read())
        self.assertEqual(warnings, [])
        self.assertEqual(config, fsm.DEFAULT_CONFIG)


class MountTableTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURES, "mountinfo.txt"), encoding="utf-8") as handle:
            self.entries = fsm.parse_mountinfo(handle.read())

    def test_parse(self):
        targets = [e.target for e in self.entries]
        self.assertIn("/mnt/with space", targets)
        spaced = next(e for e in self.entries if e.target.endswith("My_Share"))
        self.assertEqual(spaced.source, "//nas/My Share")
        self.assertEqual(spaced.option("vers"), "3.0")
        self.assertEqual(spaced.option("username"), "kris")
        self.assertEqual(len(fsm.cifs_mounts(self.entries)), 6)

    def test_find_existing(self):
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://PHOTOS.example.net/shared"),
                                         self.entries, 1000)
        self.assertEqual(own.target, "/run/dolphin-fast-share-mount/1000/photos.example.net/Shared")
        self.assertEqual(foreign, [])
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://nas/Media/sub"), self.entries, 1000)
        self.assertIsNone(own, "another user's mount must not count as ours")
        self.assertEqual([e.target for e in foreign], ["/mnt/media"])
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://nas/Private"), self.entries, 1000)
        self.assertEqual((own, foreign), (None, []), "another user's tool mount is neither")
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://nas/Other"), self.entries, 1000)
        self.assertEqual((own, foreign), (None, []))

    def test_find_by_address_includes_own_mounts(self):
        addresses = {"photos.example.net": {"192.168.1.5"}, "192.168.1.5": {"192.168.1.5"},
                     "other.example": {"10.0.0.9"}}
        with mock.patch.object(fsm, "resolve", side_effect=lambda h: addresses.get(h, set())):
            own, foreign = fsm.find_by_address(fsm.parse_smb_url("smb://192.168.1.5/Shared"),
                                               self.entries, 1000)
            self.assertEqual(own.source, "//photos.example.net/Shared")
            self.assertEqual(fsm.find_by_address(fsm.parse_smb_url("smb://other.example/Shared"),
                                                 self.entries, 1000), (None, []))

    def test_mount_containing(self):
        entry = fsm.mount_containing(
            "/run/dolphin-fast-share-mount/1000/nas/My_Share/deep/folder", self.entries)
        self.assertEqual(entry.source, "//nas/My Share")
        self.assertIsNone(fsm.mount_containing("/run/dolphin-fast-share-mount/1000/nas/My_Share2",
                                               self.entries))
        self.assertIsNone(fsm.mount_containing("/mnt/with space", self.entries), "nfs is not cifs")

    def test_source_key(self):
        self.assertEqual(fsm.source_key("//NAS/Media/"), ("nas", "media"))
        self.assertIsNone(fsm.source_key("//nas/Media/sub"))
        self.assertIsNone(fsm.source_key("server:/export"))


class HelperOutputTests(unittest.TestCase):
    def test_ok(self):
        result = fsm.parse_helper_output(0, "OK\t/run/x/1000/nas/S\t3.1.1\n", "")
        self.assertTrue(result.ok)
        self.assertEqual(result.lines, [["OK", "/run/x/1000/nas/S", "3.1.1"]])

    def test_error_line(self):
        result = fsm.parse_helper_output(10, "", "noise\nEAUTH\tLogin failed\tmount error(13)\n")
        self.assertEqual((result.ok, result.code, result.message, result.detail),
                         (False, "EAUTH", "Login failed", "mount error(13)"))

    def test_pkexec_dismissed_and_sudo(self):
        dismissed = fsm.parse_helper_output(126, "", "Error executing command as another user: "
                                                     "Request dismissed\n")
        self.assertEqual(dismissed.code, "EAUTHORIZE")
        sudo = fsm.parse_helper_output(1, "", "sudo: no password was provided\n")
        self.assertEqual(sudo.code, "EAUTHORIZE")
        other = fsm.parse_helper_output(1, "", "something odd\n")
        self.assertEqual(other.code, "EINTERNAL")

    def test_explain(self):
        ref = fsm.parse_smb_url("smb://photos.example.net/Media2")
        title, body = fsm.explain(fsm.HelperResult(False, [], "ENOSHARE", "x"), ref, "jordan")
        self.assertEqual(body, "Share 'Media2' not found on photos.example.net (mount error 2).")
        title, body = fsm.explain(fsm.HelperResult(False, [], "EAUTH", "x"), ref, "jordan")
        self.assertIn("jordan@photos.example.net", body)
        title, body = fsm.explain(fsm.HelperResult(
            False, [], "EDENIED", "The login worked, but the server refused access to the share"),
            ref, "jordan")
        self.assertEqual((title, body), ("Access denied",
                         "jordan@photos.example.net, share 'Media2': The login worked, but the "
                         "server refused access to the share."))
        title, body = fsm.explain(fsm.HelperResult(
            False, [], "EAUTH", "Login failed: wrong user name or password"), ref, "jordan")
        self.assertEqual(body, "Login failed for jordan@photos.example.net: wrong user name or password.")
        title, body = fsm.explain(fsm.HelperResult(
            False, [], "EAUTH", "Login failed: wrong user name or password, or no access to this share"),
            ref, "jordan")
        self.assertIn("or no access to the share 'Media2' (mount error 13)", body)
        title, body = fsm.explain(fsm.HelperResult(
            False, [], "EMISSING", "mount.cifs is missing (package cifs-utils)"), ref, "jordan")
        self.assertEqual(title, "Required component missing")
        self.assertIn("cifs-utils", body)


class HelperTrustTests(unittest.TestCase):
    def test_user_owned_file_is_refused(self):
        with tempfile.NamedTemporaryFile() as handle:
            os.chmod(handle.name, 0o755)
            problem = fsm.is_root_secure(handle.name)
            if os.geteuid() != 0:
                self.assertIsNotNone(problem)

    def test_symlinked_folders_are_followed(self):
        if not os.path.islink("/bin") or os.stat("/usr/bin/bash").st_uid != 0:
            self.skipTest("/bin is not a symbolic link here")
        self.assertIsNone(fsm.is_root_secure("/bin/bash"))

    def test_programs_come_from_system_folders_only(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = os.path.join(folder, "pkexec")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"PATH": folder}):
                self.assertNotEqual(fsm.which("pkexec"), fake)
                self.assertNotIn(fake, str(fsm.Elevator(mock.Mock()).methods("/x")))

    def test_owner_and_folder_checks(self):
        helper = "/opt/test/fast-share-mount-helper"
        real_lstat = os.lstat

        def fake(file_uid, folder_uid):
            def lstat(path):
                if path == helper:
                    return os.stat_result((0o100755, 0, 0, 1, file_uid, 0, 0, 0, 0, 0))
                if path == "/opt/test":
                    return os.stat_result((0o040755, 0, 0, 1, folder_uid, 0, 0, 0, 0, 0))
                return real_lstat(path)
            return lstat

        with mock.patch.object(fsm.os.path, "realpath", side_effect=lambda p: p):
            with mock.patch.object(fsm.os, "lstat", side_effect=fake(1000, 0)):
                self.assertTrue(fsm.is_root_secure(helper).startswith(helper))
            with mock.patch.object(fsm.os, "lstat", side_effect=fake(0, 1000)):
                self.assertTrue(fsm.is_root_secure(helper).startswith("/opt/test can be changed"))
            if os.stat("/opt").st_uid == 0 and not os.stat("/opt").st_mode & 0o022:
                with mock.patch.object(fsm.os, "lstat", side_effect=fake(0, 0)):
                    self.assertIsNone(fsm.is_root_secure(helper))

    def test_run_never_leaves_a_name_to_path(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = os.path.join(folder, "fsm-only-in-path")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\necho FAKE\n")
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"PATH": folder}):
                self.assertEqual(fsm.run(["fsm-only-in-path"]).returncode, 127)
                with mock.patch.object(fsm.subprocess, "run") as called:
                    fsm.run(["true"])
                self.assertEqual(called.call_args.args[0][0], fsm.which("true"))
                self.assertTrue(os.path.isabs(called.call_args.args[0][0]))

    def test_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            link = os.path.join(folder, "helper")
            os.symlink("/usr/bin/env", link)
            self.assertIn("not a regular file", fsm.is_root_secure(link))

    def test_root_owned_system_file_is_accepted(self):
        path = "/usr/bin/env"
        if not os.path.isfile(path) or os.path.islink(path) or os.stat(path).st_uid != 0:
            self.skipTest("/usr/bin/env is not a plain root-owned file here")
        self.assertIsNone(fsm.is_root_secure(path))

    def test_find_helper_reports_unsafe_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = os.path.join(folder, "fast-share-mount-helper")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
            with mock.patch.object(fsm, "HELPER_CANDIDATES", (fake,)):
                helper, reason = fsm.find_helper()
            if os.geteuid() != 0:
                self.assertIsNone(helper)
                self.assertIn("not safe to run as root", reason)
            with mock.patch.object(fsm, "HELPER_CANDIDATES", (os.path.join(folder, "none"),)):
                helper, reason = fsm.find_helper()
            self.assertIn("not installed", reason)


class MiscTests(unittest.TestCase):
    def test_wallet_key(self):
        self.assertEqual(fsm.wallet_key("jordan", "nas", None), "jordan@nas")
        self.assertEqual(fsm.wallet_key("jordan", "nas", "CORP"), "CORP;jordan@nas")

    def test_package_manager(self):
        cases = {("opensuse-tumbleweed", "opensuse suse"): "zypper",
                 ("fedora", ""): "dnf", ("ubuntu", "debian"): "apt", ("kubuntu", "ubuntu debian"): "apt",
                 ("endeavouros", "arch"): "pacman", ("gentoo", ""): None}
        for (ident, like), expected in cases.items():
            with self.subTest(ident=ident):
                with mock.patch.object(fsm, "os_release", return_value={"ID": ident, "ID_LIKE": like}):
                    self.assertEqual(fsm.package_manager(), expected)
                    if expected == "zypper":
                        self.assertEqual(fsm.install_hint("mount.cifs"),
                                         "Install with: sudo zypper install cifs-utils")

    def test_stat_probe(self):
        self.assertTrue(fsm.path_responds("/"))
        self.assertFalse(fsm.path_responds("/nonexistent/path/for/test"))


class CommandLineTests(unittest.TestCase):
    """The script as a program, with an isolated home and no desktop session."""

    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "XDG_RUNTIME_DIR": home, "XDG_STATE_HOME": os.path.join(home, "state"),
                   "XDG_CONFIG_HOME": os.path.join(home, "config"),
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
                   # An empty mount table: these runs must never reach a real
                   # helper and unmount the developer's own shares.
                   "FAST_SHARE_MOUNT_MOUNTINFO": "/dev/null"}
            return subprocess.run([USER_SIDE, *args], capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL, timeout=60, check=False)

    def test_version(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"fast-share-mount {fsm.VERSION}")

    def test_list(self):
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fast Share Mount mounts:", result.stdout)

    def test_bad_url_exit_status(self):
        result = self.run_cli("mount", "--no-open", "smb://")
        self.assertEqual(result.returncode, 2)
        self.assertIn("names no server", result.stderr)

    def test_server_that_does_not_answer(self):
        result = self.run_cli("mount", "--no-open", "smb://nas.invalid/")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not answer as a server", result.stderr)
        self.assertIn("workgroup", result.stderr)

    def test_unmount_of_a_plain_folder(self):
        result = self.run_cli("unmount", "/tmp")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not on a Fast Share Mount mount", result.stderr)

    def test_choose_with_nothing_mounted(self):
        result = self.run_cli("unmount", "--choose", "/tmp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to unmount", result.stderr)

    def test_unmount_all_with_nothing_mounted(self):
        result = self.run_cli("unmount", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to unmount", result.stderr)

    def test_shebang_is_isolated_system_python(self):
        with open(USER_SIDE, encoding="utf-8") as handle:
            self.assertEqual(handle.readline(), "#!/usr/bin/python3 -I\n")


class OutputTests(unittest.TestCase):
    def test_list_hides_other_users_tool_mounts(self):
        with open(os.path.join(FIXTURES, "mountinfo.txt"), encoding="utf-8") as handle:
            entries = fsm.parse_mountinfo(handle.read())
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": home,
                                             "XDG_CONFIG_HOME": home}), \
                mock.patch.object(fsm.syslog, "syslog"), \
                mock.patch.object(fsm.os, "getuid", return_value=1000), \
                mock.patch.object(fsm, "read_mounts", return_value=entries), \
                mock.patch.object(fsm, "path_responds", return_value=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(fsm.main(["list"]), 0)
        text = out.getvalue()
        self.assertIn("//nas/My Share", text)
        self.assertIn("/mnt/media", text)
        self.assertNotIn("Private", text)
        self.assertNotIn("/1001/", text)
        self.assertIn("2 Fast Share Mount mount(s) of other users, not shown", text)

    def test_non_utf8_names_do_not_crash(self):
        with tempfile.TemporaryDirectory() as home:
            config = os.path.join(home, "config")
            os.makedirs(config)
            with open(os.path.join(config, "dolphin-fast-share-mount.conf"), "wb") as handle:
                handle.write(b"user=j\xf6rg\n")
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "XDG_RUNTIME_DIR": home, "XDG_STATE_HOME": os.path.join(home, "state"),
                   "XDG_CONFIG_HOME": config, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
                   "FAST_SHARE_MOUNT_MOUNTINFO": "/dev/null"}
            result = subprocess.run([os.fsencode(USER_SIDE), b"unmount", b"/tmp/caf\xe9"],
                                    capture_output=True, env=env, stdin=subprocess.DEVNULL,
                                    timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"nothing to unmount", result.stderr)
            listed = subprocess.run([USER_SIDE, "list"], capture_output=True, env=env,
                                    stdin=subprocess.DEVNULL, timeout=60, check=False)
            self.assertEqual(listed.returncode, 0, listed.stderr)


class KioFuseTests(unittest.TestCase):
    """kio-fuse paths that KIO hands to the service menu instead of smb:// URLs."""

    RUNTIME = "/run/user/1000"
    FOLDER = "/run/user/1000/kio-fuse-AbCdEf/smb/jordan@nas.example.net/Media/Films"

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.RUNTIME, "HOME": home.name,
                                     "XDG_CONFIG_HOME": f"{home.name}/config",
                                     "XDG_STATE_HOME": f"{home.name}/state"}).start()
        self.run = mock.patch.object(fsm, "run").start()
        self.addCleanup(mock.patch.stopall)

    def reply(self, stdout, returncode=0, stderr=""):
        self.run.return_value = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_fuse_path_becomes_its_smb_address(self):
        self.reply("('smb://jordan@nas.example.net/Media/Films',)\n")
        ref = fsm.parse_smb_url(fsm.remote_url(self.FOLDER))
        self.assertEqual((ref.user, ref.host, ref.share, ref.subpath),
                         ("jordan", "nas.example.net", "Media", ("Films",)))
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[0], "gdbus")
        self.assertIn("org.kde.KIOFuse.VFS.remoteUrl", argv)
        self.assertEqual(argv[-1], self.FOLDER)

    def test_fuse_file_url_is_decoded_before_asking(self):
        self.reply("('smb://nas.example.net/Media/My Films',)")
        url = "file:///run/user/1000/kio-fuse-AbCdEf/smb/nas.example.net/Media/My%20Films"
        self.assertEqual(fsm.remote_url(url), "smb://nas.example.net/Media/My Films")
        self.assertEqual(self.run.call_args.args[0][-1],
                         "/run/user/1000/kio-fuse-AbCdEf/smb/nas.example.net/Media/My Films")

    def test_reply_in_double_quotes(self):
        self.reply('("smb://nas.example.net/Media/Jordan\'s",)')
        self.assertEqual(fsm.remote_url(self.FOLDER), "smb://nas.example.net/Media/Jordan's")

    def test_other_locations_are_left_alone(self):
        for text in ("smb://nas.example.net/Media", "/home/jordan/Films",
                     "/tmp/kio-fuse-AbCdEf/smb/nas/Media",
                     "/run/user/1000/kio-fuse-AbCdEf/../../../../etc",
                     "/run/user/10000/kio-fuse-AbCdEf/smb/nas/Media"):
            self.assertEqual(fsm.remote_url(text), text)
        self.run.assert_not_called()

    def test_no_answer_from_kio_fuse_is_an_error_naming_the_path(self):
        self.reply("", returncode=1, stderr="Error: GDBus.Error:org.kde.KIOFuse.VFS.Error."
                                            "RemoteURLNotFound: no remote URL")
        with self.assertRaises(fsm.UsageError) as caught:
            fsm.remote_url(self.FOLDER)
        self.assertIn(self.FOLDER, str(caught.exception))
        self.assertIn("RemoteURLNotFound", str(caught.exception))

    def test_unreadable_reply_is_an_error(self):
        for stdout in ("garbage", "('one', 'two')", "(42,)", "('',)"):
            self.reply(stdout)
            with self.assertRaises(fsm.UsageError):
                fsm.remote_url(self.FOLDER)

    def test_fuse_path_of_another_protocol_is_refused_by_the_parser(self):
        self.reply("('sftp://jordan@nas.example.net/home',)")
        with self.assertRaises(fsm.UsageError) as caught:
            fsm.parse_smb_url(fsm.remote_url(self.FOLDER.replace("/smb/", "/sftp/")))
        self.assertIn("Not an smb:// address", str(caught.exception))

    def test_mount_command_mounts_the_share_behind_a_fuse_path(self):
        self.reply("('smb://jordan@nas.example.net/Media/Films',)")
        args = fsm.build_parser().parse_args(["mount", "--no-open", self.FOLDER])
        app = fsm.App(args)
        app.ui = FakeUI()
        with mock.patch.object(app, "mount_one", return_value=0) as mount_one:
            self.assertEqual(app.cmd_mount(), 0)
        ref = mount_one.call_args.args[0]
        self.assertEqual((ref.host, ref.share, ref.user), ("nas.example.net", "Media", "jordan"))
        self.assertEqual(app.ui.errors, [])


def dns_labels(*labels):
    return b"".join(bytes([len(x.encode())]) + x.encode() for x in labels) + b"\0"


def mdns_packet(records):
    """An mDNS response with RECORDS as (owner bytes, type, rdata bytes)."""
    import struct
    body = b"".join(owner + struct.pack(">HHIH", rtype, 1, 120, len(rdata)) + rdata
                    for owner, rtype, rdata in records)
    return struct.pack(">HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + body


def smb_records(instance, target, address=None):
    import struct
    owner = dns_labels(instance, "_smb", "_tcp", "local")
    records = [(dns_labels("_smb", "_tcp", "local"), 12, owner),
               (owner, 33, struct.pack(">HHH", 0, 0, 445) + dns_labels(*target.split(".")))]
    if address:
        records.append((dns_labels(*target.split(".")), 1, bytes(map(int, address.split(".")))))
    return records


class DisplayNameTests(unittest.TestCase):
    def test_reply_gives_instance_and_target(self):
        services, addresses = fsm.parse_mdns_reply(mdns_packet(
            smb_records("Jordan's NAS", "nas.local", "192.0.2.7")))
        self.assertEqual(services, {"Jordan's NAS": "nas.local"})
        self.assertEqual(addresses, {"nas.local": {"192.0.2.7"}})

    def test_compressed_names_are_followed(self):
        import struct
        header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        owner = dns_labels("Media Box", "_smb", "_tcp", "local")
        # the target "mediabox" + pointer to "local" inside the owner name
        local_at = 12 + len(owner) - len(dns_labels("local"))
        target = bytes([8]) + b"mediabox" + struct.pack(">H", 0xC000 | local_at)
        rdata = struct.pack(">HHH", 0, 0, 445) + target
        packet = header + owner + struct.pack(">HHIH", 33, 1, 120, len(rdata)) + rdata
        self.assertEqual(fsm.parse_mdns_reply(packet)[0], {"Media Box": "mediabox.local"})

    def test_malformed_packets_raise_instead_of_hanging(self):
        import struct
        loop = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + struct.pack(">H", 0xC00C)
        good = mdns_packet(smb_records("NAS", "nas.local"))
        for packet in (loop, good[:-5], good[:7], b""):
            with self.assertRaises((ValueError, IndexError, struct.error)):
                fsm.parse_mdns_reply(packet)

    def test_record_longer_than_the_packet_is_refused(self):
        import struct
        owner = dns_labels("NAS", "_smb", "_tcp", "local")
        rdata = struct.pack(">HHH", 0, 0, 445) + dns_labels("nas", "local")
        packet = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + owner + \
            struct.pack(">HHIH", 33, 1, 120, len(rdata) + 40) + rdata
        with self.assertRaises(ValueError):
            fsm.parse_mdns_reply(packet)

    def test_other_services_do_not_count(self):
        import struct
        owner = dns_labels("Printer", "_ipp", "_tcp", "local")
        packet = mdns_packet([(owner, 33, struct.pack(">HHH", 0, 0, 631) + dns_labels("printer", "local"))])
        self.assertEqual(fsm.parse_mdns_reply(packet)[0], {})

    def lookup(self, host, packets, addresses=()):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recvfrom.side_effect = [(p, ("192.0.2.1", 5353)) for p in packets] + [fsm.socket.timeout()]
        with mock.patch.object(fsm.socket, "socket", return_value=sock), \
                mock.patch.object(fsm.socket, "getaddrinfo",
                                  return_value=[(2, 2, 17, "", (a, 0)) for a in addresses]):
            return fsm.server_display_name(host, timeout=1.0), sock

    def test_server_is_found_by_host_name_in_any_case(self):
        name, sock = self.lookup("NAS.local", [mdns_packet(smb_records("Other", "other.local")),
                                               mdns_packet(smb_records("Jordan's NAS", "nas.local"))])
        self.assertEqual(name, "Jordan's NAS")
        self.assertEqual(sock.sendto.call_args.args[1], ("224.0.0.251", 5353))

    def test_server_reached_by_another_name_is_found_by_address(self):
        name, _ = self.lookup("nas.example.net",
                              [mdns_packet(smb_records("Jordan's NAS", "nas.local", "192.0.2.7"))],
                              addresses=["192.0.2.7"])
        self.assertEqual(name, "Jordan's NAS")

    def test_no_match_and_broken_packets_give_none(self):
        name, _ = self.lookup("nas.example.net",
                              [b"\x00garbage", mdns_packet(smb_records("Other", "other.local", "192.0.2.9"))],
                              addresses=["192.0.2.7"])
        self.assertIsNone(name)

    def test_folder_label(self):
        self.assertEqual(fsm.folder_label("photos.example.net", "Photos"), "Photos")
        self.assertEqual(fsm.folder_label("photos.example.net", None), "photos")
        self.assertEqual(fsm.folder_label("192.0.2.7", None), "192.0.2.7")
        self.assertEqual(fsm.folder_label("nas", "Bad/Name\x07 "), "BadName")
        long = fsm.folder_label("nas", "é" * 40)
        self.assertEqual(long, "é" * 32)
        self.assertEqual(len(long.encode()), 64)


class ChooseUnmountTests(unittest.TestCase):
    ROOT = "/run/dolphin-fast-share-mount/1000"

    def entry(self, target, source):
        return fsm.MountEntry(target, source, "cifs", "rw,vers=3.1.1")

    def app(self, picked, targets=()):
        args = fsm.build_parser().parse_args(["unmount", "--choose", *targets])
        app = fsm.App(args)
        app.uid = 1000
        app.ui = FakeUI()
        self.offered = []
        self.labels = []
        app.ui.choose_with_all = lambda text, items, verb, all_label: \
            self.labels.append((verb, all_label)) or self.offered.append(items) or picked
        app.unmount_entry = mock.Mock(return_value=0)
        mock.patch.object(fsm, "read_mounts", return_value=[
            self.entry(f"{self.ROOT}/photos.example.net/Photos-Backups", "//photos.example.net/Backups"),
            self.entry(f"{self.ROOT}/nas/Nas-Media", "//nas/Media"),
            self.entry("/run/dolphin-fast-share-mount/1001/nas/Nas-Media", "//nas/Media"),
            self.entry("/mnt/fstab-share", "//nas/Other"),
        ]).start()
        self.addCleanup(mock.patch.stopall)
        return app

    def test_only_own_mounts_are_offered_and_the_clicked_one_is_ticked(self):
        app = self.app([], targets=[f"{self.ROOT}/nas/Nas-Media/Films/2024"])
        self.assertEqual(app.cmd_unmount(), 0)
        self.assertEqual(self.offered[0], [
            ("Nas-Media    //nas/Media", True),
            ("Photos-Backups    //photos.example.net/Backups", False),
        ])
        app.unmount_entry.assert_not_called()
        self.assertEqual(self.labels, [("unmount", "All 2 mounted shares")])

    def test_each_picked_share_is_unmounted(self):
        app = self.app([1, 0])
        self.assertEqual(app.cmd_unmount(), 0)
        unmounted = [c.args[0].target for c in app.unmount_entry.call_args_list]
        self.assertEqual(unmounted, [f"{self.ROOT}/photos.example.net/Photos-Backups",
                                     f"{self.ROOT}/nas/Nas-Media"])

    def test_cancel_unmounts_nothing(self):
        app = self.app(None)
        self.assertEqual(app.cmd_unmount(), 0)
        app.unmount_entry.assert_not_called()

    def test_no_way_to_ask_is_an_error(self):
        app = self.app(None)

        def unable(text, items):
            raise fsm.UsageError("kdialog or zenity is needed to choose a share")
        app.ui.choose_with_all = lambda *a: unable(*a[:2])
        self.assertEqual(app.cmd_unmount(), 1)
        self.assertEqual(app.ui.errors[0][0], "Cannot ask")
        app.unmount_entry.assert_not_called()

    def test_choose_and_all_together_are_refused(self):
        args = fsm.build_parser().parse_args(["unmount", "--choose", "--all"])
        app = fsm.App(args)
        app.ui = FakeUI()
        self.assertEqual(app.cmd_unmount(), 2)


class ChooseManyDialogTests(unittest.TestCase):
    ITEMS = [("Nas-Media    //nas/Media", True), ("Photos-Backups    //photos/Backups", False)]

    def ui(self, tool, stdout, returncode=0):
        mock.patch.object(fsm, "which", lambda name: f"/usr/bin/{name}" if name == tool else None).start()
        self.run = mock.patch.object(fsm, "run", return_value=subprocess.CompletedProcess(
            [], returncode, stdout, "")).start()
        self.addCleanup(mock.patch.stopall)
        return fsm.UI(True, mock.Mock())

    def test_kdialog_checklist(self):
        ui = self.ui("kdialog", "1\n0\n")
        self.assertEqual(ui.choose_many("Unmount which shares?", self.ITEMS), [1, 0])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--geometry") + 1], "480x260")
        self.assertEqual(argv[argv.index("--checklist") + 2:],
                         ["0", self.ITEMS[0][0], "on", "1", self.ITEMS[1][0], "off"])

    def test_dialog_grows_with_long_names_and_many_shares_within_limits(self):
        ui = self.ui("kdialog", "")
        ui.choose_many("x", [("n" * 60, False)] * 4)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--geometry") + 1], f"{9 * 60 + 140}x{170 + 34 * 4}")
        ui.choose_many("x", [("n" * 500, False)] * 50)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--geometry") + 1], "1000x640")

    def test_zenity_checklist(self):
        ui = self.ui("zenity", "1\n")
        self.assertEqual(ui.choose_many("Unmount which shares?", self.ITEMS), [1])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[-6:], ["TRUE", "0", self.ITEMS[0][0], "FALSE", "1", self.ITEMS[1][0]])

    def test_ok_button_names_the_action(self):
        ui = self.ui("kdialog", "")
        ui.choose_many("x", self.ITEMS, "mount")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--ok-label") + 1], "Mount")

    def test_all_item_stands_for_every_share(self):
        ui = self.ui("kdialog", "0\n2\n")
        self.assertEqual(ui.choose_with_all("x", self.ITEMS, "unmount", "All 2 mounted shares"), [0, 1])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--checklist") + 2:argv.index("--checklist") + 5],
                         ["0", "All 2 mounted shares", "off"])
        self.assertEqual(self.ui("kdialog", "2\n").choose_with_all("x", self.ITEMS, "unmount", "All"), [1])
        self.assertIsNone(self.ui("kdialog", "", 1).choose_with_all("x", self.ITEMS, "unmount", "All"))

    def test_no_all_item_for_a_single_share(self):
        ui = self.ui("kdialog", "0\n")
        self.assertEqual(ui.choose_with_all("x", self.ITEMS[:1], "mount", "All"), [0])
        self.assertNotIn("All", self.run.call_args.args[0])

    def test_cancel_and_junk_replies(self):
        self.assertIsNone(self.ui("kdialog", "", returncode=1).choose_many("x", self.ITEMS))
        self.assertEqual(self.ui("kdialog", "7\nzz\n0\n0\n").choose_many("x", self.ITEMS), [0])

    def test_no_dialog_program_or_terminal_raises(self):
        mock.patch.object(fsm, "which", return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        with self.assertRaises(fsm.UsageError):
            fsm.UI(True, mock.Mock()).choose_many("x", self.ITEMS)
        with mock.patch.object(fsm.sys.stdin, "isatty", return_value=False), \
                self.assertRaises(fsm.UsageError):
            fsm.UI(False, mock.Mock()).choose_many("x", self.ITEMS)

    def test_terminal_answers(self):
        ui = fsm.UI(False, mock.Mock())
        for answer, expected in (("", [0]), ("all", [0, 1]), ("2 1", [1, 0]), ("2,2", [1]),
                                 ("3", None), ("x", None)):
            with mock.patch.object(fsm.sys.stdin, "isatty", return_value=True), \
                    mock.patch("builtins.input", return_value=answer), \
                    mock.patch("sys.stdout", new_callable=io.StringIO), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(ui.choose_many("x", self.ITEMS), expected, repr(answer))


class ShareListTests(unittest.TestCase):
    def listing(self, stdout, returncode=0, stderr="", tool="kioclient"):
        mock.patch.object(fsm, "which", lambda name: f"/usr/bin/{name}" if name == tool else None).start()
        self.run = mock.patch.object(fsm, "run", return_value=subprocess.CompletedProcess(
            [], returncode, stdout, stderr)).start()
        self.addCleanup(mock.patch.stopall)
        return fsm.list_shares("smb://jordan@nas.example.net/")

    def test_shares_are_listed_without_hidden_or_unmountable_ones(self):
        shares, failure = self.listing("Media\nbackups\nIPC$\nBad,Name\nMedia\n\n.\n")
        self.assertIsNone(failure)
        self.assertEqual(shares, ["backups", "Media"])
        self.assertEqual(self.run.call_args.args[0], ["kioclient", "ls", "smb://jordan@nas.example.net/"])

    def test_kio_failure_and_absence(self):
        shares, failure = self.listing("", returncode=1, stderr="noise\nkioclient: Access denied")
        self.assertEqual((shares, failure), ([], "kioclient: Access denied"))
        shares, failure = self.listing("", tool=None)
        self.assertEqual(shares, [])
        self.assertIn("kioclient", failure)


class SharePickerTests(unittest.TestCase):
    def app(self, url, picked, shares=("Backups", "Media"), mounts=(), reachable=True):
        args = fsm.build_parser().parse_args(["mount", url])
        app = fsm.App(args)
        app.uid = 1000
        app.ui = FakeUI()
        self.offered = []
        app.ui.choose_with_all = lambda text, items, verb, all_label: \
            self.offered.append((text, items, verb, all_label)) or picked
        self.mounted = []
        app.mount_one = lambda ref: self.mounted.append(ref) or 0
        mock.patch.object(fsm, "tcp_probe", return_value=None if reachable else "refused").start()
        mock.patch.object(fsm, "list_shares", return_value=(list(shares), None)).start()
        mock.patch.object(fsm, "read_mounts", return_value=list(mounts)).start()
        app.launch_folders = mock.Mock()
        self.addCleanup(mock.patch.stopall)
        return app

    def test_picked_shares_are_mounted_with_the_address_user(self):
        app = self.app("smb://jordan@nas.example.net/", [1, 0])
        self.assertEqual(app.cmd_mount(), 0)
        self.assertEqual(self.offered[0][0], "Mount which shares on nas.example.net?")
        self.assertEqual(self.offered[0][2], "mount")
        self.assertEqual(self.offered[0][3], "All 2 shares on nas.example.net")
        self.assertEqual([(r.share, r.user, r.host) for r in self.mounted],
                         [("Media", "jordan", "nas.example.net"), ("Backups", "jordan", "nas.example.net")])

    def test_mounted_shares_are_marked(self):
        own = fsm.MountEntry("/run/dolphin-fast-share-mount/1000/nas.example.net/Nas-Media",
                             "//nas.example.net/Media", "cifs", "rw")
        app = self.app("smb://nas.example.net/", [], mounts=[own])
        app.cmd_mount()
        self.assertEqual(self.offered[0][1], [("Backups", False), ("Media    (mounted)", False)])

    def test_cancel_mounts_nothing(self):
        app = self.app("smb://nas.example.net/", None)
        self.assertEqual(app.cmd_mount(), 0)
        self.assertEqual(self.mounted, [])

    def test_unreachable_server_is_reported_without_listing(self):
        app = self.app("smb://WORKGROUP/", [0], reachable=False)
        self.assertEqual(app.cmd_mount(), 1)
        self.assertIn("workgroup", app.ui.errors[0][1])
        fsm.list_shares.assert_not_called()
        self.assertEqual(self.offered, [])

    def test_shares_in_the_selection_and_from_the_picker_are_mounted_once(self):
        app = self.app("smb://nas.example.net/", [0, 1])
        app.args.urls = ["smb://nas.example.net/Media/", "smb://nas.example.net/"]
        self.assertEqual(app.cmd_mount(), 0)
        self.assertEqual([r.share for r in self.mounted], ["Media", "Backups"])


class OpenFoldersTests(unittest.TestCase):
    def app(self, mode, urls):
        args = fsm.build_parser().parse_args(["mount", *urls])
        app = fsm.App(args)
        app.config["open_mode"] = mode
        app.ui = FakeUI()
        app.mount_one = lambda ref: app.open_folder(f"/m/{ref.share}") or 0
        mock.patch.object(fsm, "which", lambda name: f"/usr/bin/{name}").start()
        self.popen = mock.patch.object(fsm.subprocess, "Popen").start()
        self.addCleanup(mock.patch.stopall)
        return app

    def test_several_mounts_open_with_one_dolphin_call(self):
        app = self.app("tab", ["smb://nas/A", "smb://nas/B", "smb://nas/A/sub"])
        self.assertEqual(app.cmd_mount(), 0)
        self.assertEqual([c.args[0] for c in self.popen.call_args_list],
                         [["/usr/bin/dolphin", "/m/A", "/m/B"]])

    def test_new_window_mode_opens_one_window_with_all(self):
        app = self.app("window", ["smb://nas/A", "smb://nas/B"])
        app.cmd_mount()
        self.assertEqual([c.args[0] for c in self.popen.call_args_list],
                         [["/usr/bin/dolphin", "--new-window", "/m/A", "/m/B"]])

    def test_none_mode_opens_nothing(self):
        app = self.app("none", ["smb://nas/A"])
        app.cmd_mount()
        self.popen.assert_not_called()


class FakeKWallet:
    """kwallet-query and the KWallet D-Bus calls as they behave in KDE
    Frameworks 6 (checked against kwalletd6 on openSUSE, 2026-10-26):
    kwallet-query -r on a missing entry exits 0 and prints an error message
    on stdout, and kwallet-query -w exits 0 but only overwrites an entry
    that already exists."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.folder = bool(self.entries)
        self.argv_seen = []

    def __call__(self, argv, input_text=None, timeout=30, env=None):
        self.argv_seen.append(list(argv))
        done = lambda out="", code=0: subprocess.CompletedProcess(argv, code, out, "")
        if argv[0] == "kwallet-query":
            key = argv[argv.index("-r") + 1] if "-r" in argv else argv[argv.index("-w") + 1]
            if "-r" in argv:
                if key in self.entries:
                    return done(self.entries[key] + "\n")
                missing = "" if self.folder else \
                    "The folder dolphin-fast-share-mount does not exist!\n"
                return done(missing + f"Failed to read entry {key} value from the kdewallet wallet.\n")
            if key in self.entries:
                text = input_text or ""
                self.entries[key] = text[:-1] if text.endswith("\n") else text
            return done()
        if argv[0] == "gdbus":
            method = argv[argv.index("--method") + 1].rsplit(".", 1)[1]
            args = argv[argv.index("--method") + 2:]
            if method == "open":
                return done("(1234,)")
            if method == "createFolder":
                self.folder = True
                return done("(true,)")
            if method == "hasEntry":
                return done("(true,)" if args[2] in self.entries else "(false,)")
            if method == "readPassword":
                return done(repr((self.entries.get(args[2], ""),)))
            if method == "writePassword":
                self.entries[args[2]] = args[3]
                return done("(0,)")
            if method == "removeEntry":
                self.entries.pop(args[2], None)
                return done("(0,)")
        raise AssertionError(f"unexpected command {argv}")


class PasswordStoreTests(unittest.TestCase):
    def store(self, wallet):
        mock.patch.object(fsm, "run", wallet).start()
        mock.patch.object(fsm, "which", lambda name: f"/usr/bin/{name}").start()
        mock.patch.object(fsm.PasswordStore, "_kwallet_service",
                          staticmethod(lambda: ("org.kde.kwalletd6", "/modules/kwalletd6"))).start()
        mock.patch.object(fsm.PasswordStore, "_wallet_name", staticmethod(lambda: "kdewallet")).start()
        self.addCleanup(mock.patch.stopall)
        store = fsm.PasswordStore(mock.Mock())
        self.assertEqual(store.kind, "kwallet")
        return store

    def test_missing_entry_is_no_password(self):
        # kwallet-query's error message must never be taken for a password
        self.assertIsNone(self.store(FakeKWallet()).get("kkf@photos.example.net"))

    def test_saved_password_is_read(self):
        store = self.store(FakeKWallet({"jordan@nas": "s3cret, with=comma"}))
        self.assertEqual(store.get("jordan@nas"), "s3cret, with=comma")

    def test_new_password_is_really_stored_and_never_in_argv(self):
        wallet = FakeKWallet()
        store = self.store(wallet)
        for password in ("first", "it's \"quoted\"", " spaced ", "ünïcødé"):
            self.assertTrue(store.put("jordan@nas", password))
            self.assertEqual(wallet.entries["jordan@nas"], password)
            self.assertEqual(store.get("jordan@nas"), password)
        self.assertFalse(any(password in " ".join(argv) for argv in wallet.argv_seen
                             for password in ("first", "spaced", "ünïcødé")))

    def test_write_that_does_not_stick_is_reported_and_cleaned_up(self):
        wallet = FakeKWallet()
        store = self.store(wallet)
        original = wallet.__call__

        def dropping(argv, input_text=None, timeout=30, env=None):
            if argv[0] == "kwallet-query" and "-w" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original(argv, input_text, timeout, env)
        mock.patch.object(fsm, "run", dropping).start()
        self.assertFalse(store.put("jordan@nas", "lost"))
        self.assertNotIn("jordan@nas", wallet.entries)


class FakeUI:
    def __init__(self, passwords=(), yes=False):
        self.passwords = list(passwords)
        self.yes = yes
        self.notes, self.errors, self.prompts = [], [], []

    def notify(self, title, body, urgent=False):
        self.notes.append((title, body))

    def error(self, title, body):
        self.errors.append((title, body))

    def ask_yes_no(self, question):
        return self.yes

    def ask_password(self, prompt):
        self.prompts.append(prompt)
        return self.passwords.pop(0) if self.passwords else None


OWN = "/run/dolphin-fast-share-mount/1000/nas/Media"


class MountFlowTests(unittest.TestCase):
    """App.mount_one with the system around it replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        environment = {"HOME": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name,
                       "XDG_STATE_HOME": self.tmp.name, "XDG_RUNTIME_DIR": self.tmp.name}
        patches = [
            mock.patch.dict(os.environ, environment),
            mock.patch.object(fsm.syslog, "syslog"),
            mock.patch.object(fsm.os, "getuid", return_value=1000),
            mock.patch.object(fsm, "tcp_probe", return_value=None),
            mock.patch.object(fsm, "find_helper", return_value=("/usr/libexec/helper", None)),
            mock.patch.object(fsm, "PasswordStore"),
            mock.patch.object(fsm, "resolve", return_value=set()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        fsm.PasswordStore.return_value.kind = None
        self.addCleanup(self.tmp.cleanup)
        self.responds = mock.patch.object(fsm, "path_responds", return_value=True)
        self.responds.start()
        self.addCleanup(self.responds.stop)

    def app(self, ui, mounts=(), helper_results=()):
        args = fsm.build_parser().parse_args(["mount", "--no-open", "--user", "kris",
                                              "smb://nas/Media/Films"])
        app = fsm.App(args)
        app.ui = ui
        app.opened = []
        app.open_folder = app.opened.append
        mock.patch.object(fsm, "read_mounts", return_value=list(mounts)).start()
        # no network in tests: the DNS-SD lookup finds nothing
        mock.patch.object(fsm, "server_display_name", return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        app.elevator = mock.Mock()
        app.elevator.run_helper.side_effect = list(helper_results)
        return app

    @staticmethod
    def entry(target, source="//nas/Media"):
        return fsm.MountEntry(target, source, "cifs", "rw,vers=3.1.1,username=kris")

    @staticmethod
    def ok(path=OWN):
        return fsm.HelperResult(True, [["OK", path, "3.1.1"]])

    @staticmethod
    def denied():
        return fsm.HelperResult(False, [], "EAUTH", "Login failed")

    def test_own_mount_is_reused(self):
        app = self.app(FakeUI(), mounts=[self.entry(OWN)])
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media/Films")), 0)
        app.elevator.run_helper.assert_not_called()
        self.assertEqual(app.opened, [OWN])
        self.assertEqual(app.ui.notes[0][0], "Already mounted")

    def test_usable_foreign_mount_is_opened(self):
        app = self.app(FakeUI(), mounts=[self.entry("/mnt/media")])
        with mock.patch.object(fsm.os, "access", return_value=True):
            self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        app.elevator.run_helper.assert_not_called()
        self.assertEqual(app.opened, ["/mnt/media"])

    def test_unreadable_foreign_mount_gets_a_mount_of_its_own(self):
        app = self.app(FakeUI(passwords=["pw"]), mounts=[self.entry("/root/private")],
                       helper_results=[self.ok()])
        with mock.patch.object(fsm.os, "access", return_value=False):
            self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        app.elevator.run_helper.assert_called_once()
        self.assertEqual(app.opened, [OWN])

    def test_wrong_password_is_asked_again(self):
        app = self.app(FakeUI(passwords=["one", "two", "three"]),
                       helper_results=[self.denied(), self.denied(), self.ok()])
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        passwords = [c.args[2] for c in app.elevator.run_helper.call_args_list]
        self.assertEqual(passwords, ["one", "two", "three"])
        self.assertEqual(app.ui.errors, [])

    def test_three_wrong_passwords_end_with_an_error(self):
        app = self.app(FakeUI(passwords=["one", "two", "three", "four"]),
                       helper_results=[self.denied()] * 3)
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 1)
        self.assertEqual(app.elevator.run_helper.call_count, 3)
        self.assertEqual(app.ui.errors[0][0], "Login failed")

    def test_refused_share_does_not_ask_for_the_password_again(self):
        refused = fsm.HelperResult(False, [], "EDENIED",
                                   "The login worked, but the server refused access to the share")
        ui = FakeUI(passwords=["one", "two"])
        app = self.app(ui, helper_results=[refused])
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 1)
        self.assertEqual(app.elevator.run_helper.call_count, 1)
        self.assertEqual(len(ui.prompts), 1)
        self.assertEqual(ui.errors[0][0], "Access denied")

    def test_cancelled_prompt_mounts_nothing(self):
        app = self.app(FakeUI(passwords=[]))
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 1)
        app.elevator.run_helper.assert_not_called()

    def test_unreachable_server_mounts_nothing(self):
        app = self.app(FakeUI(passwords=["pw"]))
        with mock.patch.object(fsm, "tcp_probe", return_value="connection refused on port 445"):
            self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 1)
        app.elevator.run_helper.assert_not_called()
        self.assertIn("connection refused", app.ui.errors[0][1])

    def test_password_limit_is_512_bytes(self):
        app = self.app(FakeUI(passwords=["é" * 256]), helper_results=[self.ok()])
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        app.elevator.run_helper.assert_called_once()
        app = self.app(FakeUI(passwords=["é" * 256 + "a"]))
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 1)
        app.elevator.run_helper.assert_not_called()
        self.assertEqual(app.ui.errors[0][0], "Password too long")

    def test_password_is_saved_only_after_a_real_login(self):
        already = fsm.HelperResult(True, [["ALREADY", OWN, "3.1.1"]])
        app = self.app(FakeUI(passwords=["typo"], yes=True), helper_results=[already])
        fsm.PasswordStore.return_value.kind = "kwallet"
        fsm.PasswordStore.return_value.get.return_value = None
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        fsm.PasswordStore.return_value.put.assert_not_called()
        app = self.app(FakeUI(passwords=["right"], yes=True), helper_results=[self.ok()])
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        fsm.PasswordStore.return_value.put.assert_called_once_with("kris@nas", "right")

    def test_remember_is_asked_before_the_root_prompt_and_only_once(self):
        events = []
        ui = FakeUI(passwords=["typo", "right"], yes=True)
        ui.ask_password = lambda prompt: events.append("password") or ui.passwords.pop(0)
        ui.ask_yes_no = lambda question: events.append("remember?") or True
        app = self.app(ui, helper_results=[self.denied(), self.ok()])
        helper_results = list(app.elevator.run_helper.side_effect)
        app.elevator.run_helper.side_effect = lambda *a: events.append("root") or helper_results.pop(0)
        fsm.PasswordStore.return_value.kind = "kwallet"
        fsm.PasswordStore.return_value.get.return_value = None
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        self.assertEqual(events, ["password", "remember?", "root", "password", "root"])
        fsm.PasswordStore.return_value.put.assert_called_once_with("kris@nas", "right")

    def test_no_is_respected_and_nothing_is_saved(self):
        app = self.app(FakeUI(passwords=["right"], yes=False), helper_results=[self.ok()])
        fsm.PasswordStore.return_value.kind = "kwallet"
        fsm.PasswordStore.return_value.get.return_value = None
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        fsm.PasswordStore.return_value.put.assert_not_called()

    def test_folder_label_goes_to_the_helper_unless_turned_off(self):
        app = self.app(FakeUI(passwords=["pw"]), helper_results=[self.ok()])
        fsm.server_display_name.return_value = "Jordan's NAS"
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        args = app.elevator.run_helper.call_args.args[1]
        self.assertEqual(args[args.index("--label") + 1], "Jordan's NAS")
        app = self.app(FakeUI(passwords=["pw"]), helper_results=[self.ok()])
        fsm.server_display_name.return_value = None
        args = (app.mount_one(fsm.parse_smb_url("smb://nas.example.net/Media")),
                app.elevator.run_helper.call_args.args[1])
        self.assertEqual(args[1][args[1].index("--label") + 1], "nas")
        app = self.app(FakeUI(passwords=["pw"]), helper_results=[self.ok()])
        app.config["folder_name"] = "share"
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        self.assertNotIn("--label", app.elevator.run_helper.call_args.args[1])
        fsm.server_display_name.assert_not_called()

    def test_port_139_is_tried_when_445_is_closed(self):
        app = self.app(FakeUI(passwords=["pw"]), helper_results=[self.ok()])
        with mock.patch.object(fsm, "tcp_probe", side_effect=["connection refused on port 445", None]):
            self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://nas/Media")), 0)
        app.elevator.run_helper.assert_called_once()

    def test_guest_sends_no_password(self):
        app = self.app(FakeUI(), helper_results=[self.ok()])
        app.args.user = None
        self.assertEqual(app.mount_one(fsm.parse_smb_url("smb://guest@nas/Media")), 0)
        args, password = app.elevator.run_helper.call_args.args[1:3]
        self.assertIn("--guest", args)
        self.assertIsNone(password)


class ElevatorTests(unittest.TestCase):
    def test_no_polkit_agent_falls_back_to_sudo(self):
        elevator = fsm.Elevator(mock.Mock())
        methods = [("pkexec", ["/usr/bin/pkexec", "/h"], None),
                   ("sudo", ["/usr/bin/sudo", "-A", "--", "/h"], {})]
        replies = [subprocess.CompletedProcess([], 127, "", "No authentication agent found.\n"),
                   subprocess.CompletedProcess([], 0, "OK\t/m\t3.1.1\n", "")]
        with mock.patch.object(elevator, "methods", return_value=methods), \
                mock.patch.object(fsm, "run", side_effect=replies) as run:
            result = elevator.run_helper("/h", ["mount"], "pw")
        self.assertTrue(result.ok)
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["/usr/bin/pkexec", "/usr/bin/sudo"])
        self.assertEqual(run.call_args_list[1].kwargs["input_text"], "pw\n")

    def test_dismissed_prompt_does_not_fall_back(self):
        elevator = fsm.Elevator(mock.Mock())
        methods = [("pkexec", ["/usr/bin/pkexec", "/h"], None), ("sudo", ["/usr/bin/sudo"], {})]
        reply = subprocess.CompletedProcess([], 126, "", "Error executing command as another user: "
                                                        "Request dismissed\n")
        with mock.patch.object(elevator, "methods", return_value=methods), \
                mock.patch.object(fsm, "run", return_value=reply) as run:
            result = elevator.run_helper("/h", ["mount"], "pw")
        self.assertEqual(result.code, "EAUTHORIZE")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()

# End of File #
