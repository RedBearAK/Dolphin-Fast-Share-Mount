"""Tests for install.sh and uninstall.sh.

The user part is installed into a temporary home folder and the system part
into a DESTDIR staging folder, so nothing outside the test folders changes
and no root rights are needed.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import configparser
import os
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree

from support import INSTALLER, UNINSTALLER

APP_ID = "dolphin-fast-share-mount"
POLICY = "io.github.redbearak.dolphin-fast-share-mount.policy"


def all_files(root: str) -> list:
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(folder, name), root))
        for name in _dirs:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                found.append(os.path.relpath(path, root))
    return sorted(found)


def snapshot(root: str) -> dict:
    """Content and mode of every file below root."""
    result = {}
    for relative in all_files(root):
        path = os.path.join(root, relative)
        with open(path, "rb") as handle:
            result[relative] = (handle.read(), stat.S_IMODE(os.lstat(path).st_mode))
    return result


class UserPartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.env = {
            "HOME": self.home,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": os.path.join(self.tmp.name, "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
        }
        os.makedirs(self.env["XDG_RUNTIME_DIR"], mode=0o700)
        self.command = os.path.join(self.home, ".local/bin/fast-share-mount")
        self.menus = os.path.join(self.home, ".local/share/kio/servicemenus")
        self.config = os.path.join(self.home, ".config", f"{APP_ID}.conf")
        self.own_dir = os.path.join(self.home, ".local/share", APP_ID)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, path, *args):
        return subprocess.run([path, *args], capture_output=True, text=True, env=self.env,
                              stdin=subprocess.DEVNULL, timeout=120, check=False)

    def install(self):
        result = self.script(INSTALLER, "--user-only")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def uninstall(self, *args):
        return self.script(UNINSTALLER, "--user-only", *args)

    def assert_installed(self):
        self.assertTrue(os.path.isfile(self.command) and not os.path.islink(self.command))
        self.assertEqual(stat.S_IMODE(os.stat(self.command).st_mode), 0o755)
        for name in ("dolphin-fast-share-mount.desktop", "dolphin-fast-share-mount-unmount.desktop"):
            path = os.path.join(self.menus, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755, name)
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(path, encoding="utf-8")
            for section in parser.sections():
                if section.startswith("Desktop Action"):
                    exec_line = parser[section]["Exec"]
                    self.assertTrue(exec_line.startswith(f'"{self.command}" '), exec_line)
                    self.assertIn("--gui", exec_line)
                    self.assertTrue(exec_line.endswith("%U"))
        self.assertTrue(os.path.isfile(self.config))
        with open(os.path.join(self.own_dir, "installed-files.txt"), encoding="utf-8") as handle:
            listed = handle.read().split()
        self.assertIn(self.command, listed)
        self.assertTrue(os.access(os.path.join(self.own_dir, "uninstall.sh"), os.X_OK))

    def test_install_is_idempotent(self):
        self.install()
        self.assert_installed()
        before = snapshot(self.home)
        self.install()
        self.assertEqual(before, snapshot(self.home))

    def test_settings_are_kept(self):
        self.install()
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("user=kkf\n")
        result = self.install()
        self.assertIn("kept your settings", result.stdout)
        with open(self.config, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "user=kkf\n")

    def test_reinstall_repairs_damage(self):
        self.install()
        os.remove(os.path.join(self.menus, "dolphin-fast-share-mount.desktop"))
        os.chmod(os.path.join(self.menus, "dolphin-fast-share-mount-unmount.desktop"), 0o644)
        os.remove(self.command)
        os.symlink("/etc/hostname", self.command)
        with open(os.path.join(self.own_dir, "uninstall.sh"), "w", encoding="utf-8") as handle:
            handle.write("garbage")
        old_file = os.path.join(self.home, ".local/bin/fast-share-mount-old")
        with open(old_file, "w", encoding="utf-8") as handle:
            handle.write("from an older version")
        with open(os.path.join(self.own_dir, "installed-files.txt"), "a", encoding="utf-8") as handle:
            handle.write(old_file + "\n/etc/passwd\n")
        self.install()
        self.assert_installed()
        self.assertFalse(os.path.exists(old_file), "file from the old manifest not pruned")
        self.assertTrue(os.path.exists("/etc/passwd"))
        with open(os.path.join(self.own_dir, "uninstall.sh"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("#!/bin/bash"))

    def test_folder_in_the_way_is_reported(self):
        os.makedirs(self.command)
        result = self.script(INSTALLER, "--user-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is a folder", result.stderr)

    def test_uninstall_removes_everything(self):
        self.install()
        state = os.path.join(self.home, ".local/state", APP_ID)
        os.makedirs(state)
        with open(os.path.join(state, "log"), "w", encoding="utf-8") as handle:
            handle.write("log line\n")
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.home), [])
        again = self.uninstall()
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)

    def test_uninstall_from_the_installed_copy(self):
        self.install()
        result = self.script(os.path.join(self.own_dir, "uninstall.sh"), "--user-only")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.home), [])

    def test_uninstall_keep_config(self):
        self.install()
        result = self.uninstall("--keep-config", "--keep-passwords")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.home), [f".config/{APP_ID}.conf"])

    def test_uninstall_of_a_damaged_install(self):
        self.install()
        os.remove(self.command)
        os.remove(os.path.join(self.own_dir, "installed-files.txt"))
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.home), [])


class SystemPartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = self.tmp.name
        self.env = {"DESTDIR": self.dest, "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": self.dest}

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, path, *args):
        return subprocess.run([path, *args], capture_output=True, text=True, env=self.env,
                              stdin=subprocess.DEVNULL, timeout=120, check=False)

    def test_install_reinstall_uninstall(self):
        for _ in range(2):
            result = self.script(INSTALLER, "--system-only")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.dest), sorted([
            "usr/local/libexec/fast-share-mount-helper",
            f"usr/local/share/{APP_ID}/installed-files.txt",
            f"usr/local/share/{APP_ID}/uninstall.sh",
            f"usr/share/polkit-1/actions/{POLICY}",
        ]))
        helper = os.path.join(self.dest, "usr/local/libexec/fast-share-mount-helper")
        self.assertEqual(stat.S_IMODE(os.stat(helper).st_mode), 0o755)
        policy = ElementTree.parse(os.path.join(self.dest, "usr/share/polkit-1/actions", POLICY))
        action = policy.getroot().find("action")
        self.assertEqual(action.get("id"), "io.github.redbearak.dolphin-fast-share-mount.mount")
        annotate = action.find("annotate")
        self.assertEqual(annotate.get("key"), "org.freedesktop.policykit.exec.path")
        self.assertEqual(annotate.text, "/usr/local/libexec/fast-share-mount-helper")
        self.assertEqual(action.find("defaults/allow_active").text, "auth_admin_keep")

        result = self.script(UNINSTALLER, "--system-only")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.dest), [])

    def test_prefix_usr(self):
        result = self.script(INSTALLER, "--system-only", "--prefix", "/usr")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        policy = ElementTree.parse(os.path.join(self.dest, "usr/share/polkit-1/actions", POLICY))
        self.assertEqual(policy.getroot().find("action/annotate").text,
                         "/usr/libexec/fast-share-mount-helper")
        result = self.script(UNINSTALLER, "--system-only", "--prefix", "/usr")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(all_files(self.dest), [])

    def test_other_prefixes_refused(self):
        result = self.script(INSTALLER, "--system-only", "--prefix", "/opt")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(all_files(self.dest), [])


if __name__ == "__main__":
    unittest.main()

# End of File #
