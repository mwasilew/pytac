# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Minimal smoke tests that confirm the pytest setup itself is functional.

These intentionally stay on the import/CLI-parser path and avoid the hardware
and config-loading code, so they run with no debug board attached. Deeper board
and config coverage lives in the dedicated test suite.
"""

import os

import pytest

import pytactl
from pytactl import boardconfig
from pytactl.cli import build_parser, main


def test_version_is_defined():
    assert isinstance(pytactl.__version__, str) and pytactl.__version__


def test_boards_dir_is_in_the_package():
    path = boardconfig.BOARDS_DIR
    assert os.path.basename(path) == "boards"
    assert path.startswith(os.path.dirname(pytactl.__file__))
    assert os.path.isdir(path)


def test_parser_dispatches_subcommand():
    parser = build_parser()
    args = parser.parse_args(["list"])
    assert args.mode == "list"


def test_listconfigs_exits_nonzero_without_board_files(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["listconfigs", "--boards-dir", str(tmp_path)])
    assert excinfo.value.code == 1
    assert "none" in capsys.readouterr().out


def test_listconfigs_lists_the_bundled_boards(capsys):
    main(["listconfigs"])
    out = capsys.readouterr().out
    assert "ftdi-65" in out and "ALPACA-LITE MTP8975" in out
    assert "INVALID" not in out


def test_showconfig_says_where_things_come_from(capsys):
    main(["showconfig", "ftdi-65"])
    out = capsys.readouterr().out
    assert "extends: ftdi, common, common-no-usb1, common-battery-edl" in out
    assert "msmreset" in out and "bootToSecondaryEDL (removed)" in out


def test_showconfig_of_an_unknown_board_fails(capsys):
    with pytest.raises(SystemExit):
        main(["showconfig", "no-such-board"])
