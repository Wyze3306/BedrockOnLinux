"""What the launcher's self-update may replace, by how it was installed.

'file' swaps the release's .pyz in for argv[0]. That is right for a .pyz,
and wrong for the console script pip writes in a venv or ~/.local/bin -- the
installed package stays behind, at the old version (#306) -- and for
`python3 -m bol`, where argv[0] is bol/__main__.py itself.
"""
# SPDX-License-Identifier: MIT

import os
from unittest import mock

from bol import update


def _kind(argv0, package):
    environ = {k: v for k, v in os.environ.items() if k != "APPIMAGE"}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(update, "_self_path", return_value=argv0), \
            mock.patch.object(update, "_package_dir", return_value=package):
        return update.update_kind()


def _file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_a_pip_install_is_left_to_pip(tmp_path):
    venv = tmp_path / "venv"
    script = _file(venv / "bin/bedrock-on-linux")
    package = venv / "lib/python3.14/site-packages/bol"
    assert _kind(script, package) == "system"


def test_a_distribution_built_wheel_is_left_to_the_package_manager(tmp_path):
    script = _file(tmp_path / "bin/bedrock-on-linux")
    package = tmp_path / "lib/python3/dist-packages/bol"
    assert _kind(script, package) == "system"


def test_python_m_bol_in_a_checkout_is_left_to_git(tmp_path):
    (tmp_path / ".git").mkdir()
    main = _file(tmp_path / "bol/__main__.py")
    assert _kind(main, main.parent) == "git"


def test_python_m_bol_never_swaps_a_pyz_in_for_its_own_main(tmp_path):
    main = _file(tmp_path / "bol/__main__.py")
    assert _kind(main, main.parent) == "system"


def test_a_writable_pyz_is_still_replaced_by_the_new_one(tmp_path):
    pyz = _file(tmp_path / "bin/bedrock-on-linux.pyz")
    assert _kind(pyz, pyz / "bol") == "file"


def test_the_checkout_script_is_still_left_to_git(tmp_path):
    (tmp_path / ".git").mkdir()
    script = _file(tmp_path / "bedrock-on-linux")
    assert _kind(script, tmp_path / "bol") == "git"
