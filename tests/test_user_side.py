"""Tests for bin/fast-share-mount (the user side).

SPDX-License-Identifier: GPL-3.0-or-later
"""

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
        ref = fsm.parse_smb_url("smb://photos.rbns.cloud/Shared_Files")
        self.assertEqual((ref.host, ref.share, ref.subpath, ref.user, ref.domain, ref.port),
                         ("photos.rbns.cloud", "Shared_Files", (), None, None, None))
        self.assertEqual(ref.source, "//photos.rbns.cloud/Shared_Files")

    def test_user_domain_port_password_ignored(self):
        ref = fsm.parse_smb_url("smb://CORP;kkf:secret@NAS.local:4445/Media/Films/2024")
        self.assertEqual((ref.host, ref.share, ref.subpath, ref.user, ref.domain, ref.port),
                         ("nas.local", "Media", ("Films", "2024"), "kkf", "CORP", 4445))
        self.assertNotIn("secret", repr(ref))

    def test_backslash_domain_and_percent_encoding(self):
        ref = fsm.parse_smb_url("smb://CORP%5Ckkf@nas/My%20Share/Sub%20Folder/")
        self.assertEqual((ref.domain, ref.user, ref.share, ref.subpath),
                         ("CORP", "kkf", "My Share", ("Sub Folder",)))

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
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(fsm.UsageError) as caught:
                    fsm.parse_smb_url(url)
                self.assertIn(expected, str(caught.exception))

    def test_password_not_echoed_in_errors(self):
        with self.assertRaises(fsm.UsageError) as caught:
            fsm.parse_smb_url("ftp://kkf:hunter2@nas/Share")
        self.assertNotIn("hunter2", str(caught.exception))

    def test_key_is_case_insensitive(self):
        self.assertEqual(fsm.parse_smb_url("smb://NAS/Media").key,
                         fsm.parse_smb_url("smb://nas/media").key)

    def test_redact(self):
        self.assertEqual(fsm.redact("smb://kkf:hunter2@nas/x"), "smb://kkf:***@nas/x")
        self.assertEqual(fsm.redact("username=k,password=hunter2,uid=1"),
                         "username=k,password=***,uid=1")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_values(self):
        config, warnings = fsm.parse_config("user=kkf\nopen_mode=window\n# comment\n\n")
        self.assertEqual(config["user"], "kkf")
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

    def test_config_is_not_executed(self):
        config, warnings = fsm.parse_config("user=$(touch /tmp/should-not-exist)\n")
        self.assertEqual(config["user"], "")
        self.assertTrue(warnings)

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
        self.assertEqual(len(fsm.cifs_mounts(self.entries)), 5)

    def test_find_existing(self):
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://PHOTOS.rbns.cloud/shared"),
                                         self.entries, 1000)
        self.assertEqual(own.target, "/run/dolphin-fast-share-mount/1000/photos.rbns.cloud/Shared")
        self.assertIsNone(foreign)
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://nas/Media/sub"), self.entries, 1000)
        self.assertIsNone(own, "another user's mount must not count as ours")
        self.assertEqual(foreign.target, "/mnt/media")
        own, foreign = fsm.find_existing(fsm.parse_smb_url("smb://nas/Other"), self.entries, 1000)
        self.assertEqual((own, foreign), (None, None))

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
        ref = fsm.parse_smb_url("smb://photos.rbns.cloud/Media2")
        title, body = fsm.explain(fsm.HelperResult(False, [], "ENOSHARE", "x"), ref, "kkf")
        self.assertEqual(body, "Share 'Media2' not found on photos.rbns.cloud (mount error 2).")
        title, body = fsm.explain(fsm.HelperResult(False, [], "EAUTH", "x"), ref, "kkf")
        self.assertIn("kkf@photos.rbns.cloud", body)
        title, body = fsm.explain(fsm.HelperResult(
            False, [], "EMISSING", "mount.cifs is missing (package cifs-utils)"), ref, "kkf")
        self.assertEqual(title, "Required component missing")
        self.assertIn("cifs-utils", body)


class HelperTrustTests(unittest.TestCase):
    def test_user_owned_file_is_refused(self):
        with tempfile.NamedTemporaryFile() as handle:
            os.chmod(handle.name, 0o755)
            problem = fsm.is_root_secure(handle.name)
            if os.geteuid() != 0:
                self.assertIsNotNone(problem)

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
        self.assertEqual(fsm.wallet_key("kkf", "nas", None), "kkf@nas")
        self.assertEqual(fsm.wallet_key("kkf", "nas", "CORP"), "CORP;kkf@nas")

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
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"}
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


if __name__ == "__main__":
    unittest.main()

# End of File #
