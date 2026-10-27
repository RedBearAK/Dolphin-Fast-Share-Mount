#!/bin/bash
#
# Run every check: bash syntax, shellcheck (when installed), and the unit tests.
#
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

scripts=(install.sh uninstall.sh libexec/fast-share-mount-helper tests/run.sh tests/integration/samba-test.sh)
for script in "${scripts[@]}"; do
    bash -n "$script"
done
echo "bash -n: ok"

shellcheck=${SHELLCHECK:-$(command -v shellcheck || true)}
if [[ -n $shellcheck ]]; then
    "$shellcheck" -x "${scripts[@]}"
    echo "shellcheck: ok"
else
    echo "shellcheck: not installed, skipped"
fi

python3 -I -c 'import ast, sys; ast.parse(open("bin/fast-share-mount").read())'
cd tests
python3 -B -m unittest -v test_user_side test_helper test_install 2>&1

# End of File #
