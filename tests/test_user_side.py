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
        result = self.run_cli("mount", "--no-open", "smb://nas/")
        self.assertEqual(result.returncode, 2)
        self.assertIn("server or workgroup", result.stderr)

    def test_unmount_of_a_plain_folder(self):
        result = self.run_cli("unmount", "/tmp")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not on a Fast Share Mount mount", result.stderr)

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
        mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.RUNTIME}).start()
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
