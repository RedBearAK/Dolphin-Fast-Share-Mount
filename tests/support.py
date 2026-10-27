"""Shared paths and loaders for the tests.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
HELPER = os.path.join(ROOT, "libexec", "fast-share-mount-helper")
USER_SIDE = os.path.join(ROOT, "bin", "fast-share-mount")
INSTALLER = os.path.join(ROOT, "install.sh")
UNINSTALLER = os.path.join(ROOT, "uninstall.sh")

sys.dont_write_bytecode = True


def load_user_side():
    """Import bin/fast-share-mount (no .py suffix) as a module."""
    if "fast_share_mount" in sys.modules:
        return sys.modules["fast_share_mount"]
    loader = importlib.machinery.SourceFileLoader("fast_share_mount", USER_SIDE)
    spec = importlib.util.spec_from_loader("fast_share_mount", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fast_share_mount"] = module
    loader.exec_module(module)
    return module

# End of File #
