# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Every bundled board file, driven through the board class its platform
uses, with the hardware mocked: it loads, exposes its functions as quick
methods and its pins as commands, and every function runs."""

import os

import pytest
from conftest import BOARD_CLASSES, board_params, load_board

from pytactl import boardconfig

# Quick methods most boards have, and the boards that do not, with why.
REQUIRED_FUNCTIONS = ("powerOn", "powerOff", "bootToEDL")
MISSING_REQUIRED = {
    "ftdi-15.toml": ("powerOn", "powerOff"),  # CSM Lite: power functions only
    "ftdi-16.toml": ("powerOn", "powerOff"),
    "psoc-24.toml": ("powerOn", "powerOff"),
    "ftdi-23.toml": ("powerOn", "powerOff", "bootToEDL"),  # M.2 card: no keys
    "ftdi-69.toml": ("powerOn", "powerOff", "bootToEDL"),
    "ftdi-29.toml": ("powerOn", "powerOff", "bootToEDL"),  # RF switch box
    "ftdi-30.toml": ("powerOn", "powerOff", "bootToEDL"),  # power pins on a UART
    "ftdi-42.toml": ("powerOn", "powerOff", "bootToEDL"),  # SMART LABEL: no script
    "ftdi-56.toml": ("powerOn", "powerOff", "bootToEDL"),  # drives a missing usb1
    "ftdi-67.toml": ("powerOn", "powerOff", "bootToEDL"),  # drives a missing usb1
    "ftdi-73.toml": ("powerOn", "powerOff", "bootToEDL"),  # drives a missing usb1
    "ftdi-41.toml": ("powerOn", "bootToEDL"),  # spowerOn/fpowerOn, two EDL entries
    "ftdi-60.toml": ("powerOn", "powerOff"),
    "psoc-31.toml": ("bootToEDL",),  # bootToNADEDL/bootToEAPEDL
}


@pytest.mark.parametrize("path", board_params())
def test_board_loads_through_its_board_class(path, patch_usb_find, monkeypatch):
    config = boardconfig.load_board(path)
    board = load_board(path, patch_usb_find, monkeypatch)

    assert isinstance(board, BOARD_CLASSES[config.platform])
    assert board.config.path == config.path
    for name in config.functions:
        assert name in board.quick_methods
        assert callable(getattr(board, name))
    assert set(board.commands) == set(config.commands)
    assert set(board.pins) == set(config.pins)


@pytest.mark.parametrize("path", board_params())
def test_every_function_runs(path, patch_usb_find, monkeypatch):
    """Each function of the board's script runs end to end through the mocked
    hardware, as the REST API's QuickMethod wrapper calls it."""
    board = load_board(path, patch_usb_find, monkeypatch)
    for name in board.config.functions:
        assert board.quick_methods[name].call() == {}


@pytest.mark.parametrize("path", board_params())
def test_required_functions(path):
    config = boardconfig.load_board(path)
    missing = tuple(name for name in REQUIRED_FUNCTIONS if name not in config.functions)
    assert missing == MISSING_REQUIRED.get(os.path.basename(path), ())
