# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for Board.create_board(): the USB vendor/product dispatch that maps a
plugged-in device onto the correct board implementation."""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest
from conftest import connect, make_usb_device, requires_boards

from pytactl import debugboard

Board = debugboard.Board

# Quick methods that the directly-driven (script-less) boards hard-code.
BUGHOPPER_METHODS = (
    "powerOn",
    "powerOff",
    "bootToEDL",
    "reset",
    "forceUsbcHostMode",
    "wakeUp",
)


def test_create_board_returns_none_when_no_device(patch_usb_find, monkeypatch):
    monkeypatch.setattr(debugboard.usb.core, "find", lambda **kwargs: None)
    assert Board.create_board("missing") is None


def board_file(board):
    return os.path.basename(board.config.path)


@requires_boards
def test_ftdi_board_is_found_by_its_usb_product(patch_usb_find, monkeypatch):
    board = connect("FTDI", patch_usb_find, monkeypatch, "ALPACA-LITE MTP8975")
    assert isinstance(board, debugboard.FtdiBoard)
    assert board_file(board) == "ftdi-65.toml"


@requires_boards
def test_unknown_ftdi_board_falls_back_to_alpaca_lite(patch_usb_find, monkeypatch):
    board = connect("FTDI", patch_usb_find, monkeypatch, "No Such Board")
    assert board_file(board) == "ftdi-13.toml"
    assert board.config.fallback


@requires_boards
def test_psoc_board_is_found_by_its_board_id(patch_usb_find, monkeypatch):
    board = connect("PSOC", patch_usb_find, monkeypatch, 7)
    assert isinstance(board, debugboard.PsocBoard)
    assert board_file(board) == "psoc-7.toml"


@requires_boards
def test_unknown_psoc_board_exits(patch_usb_find, monkeypatch):
    with pytest.raises(SystemExit):
        connect("PSOC", patch_usb_find, monkeypatch, 99999)


@requires_boards
@pytest.mark.parametrize("prefix", ["KARUSSELL", "KARUSSELLV2"])
def test_pic32cx_board_is_found_by_its_serial_prefix(
    prefix, patch_usb_find, monkeypatch
):
    """A PIC32CX board is invisible to libusb: usb.core.find returns nothing
    and create_board falls through to the udev-based Pic32cxBoard.detect."""
    board = connect("PIC32CX", patch_usb_find, monkeypatch, prefix)
    assert isinstance(board, debugboard.Pic32cxBoard)
    assert board_file(board) == "pic32cx-54.toml"
    for name in ("powerOn", "powerOff", "bootToEDL"):
        board.quick_methods[name].call()


@requires_boards
def test_unknown_pic32cx_board_exits(patch_usb_find, monkeypatch):
    with pytest.raises(SystemExit):
        connect("PIC32CX", patch_usb_find, monkeypatch, "UNKNOWN")


def test_create_board_dispatches_bughopper_v1(patch_usb_find):
    device = make_usb_device(Board.ID_VENDOR_FTDI, Board.ID_PRODUCT_BUGHOPPER_V1, "S3")
    patch_usb_find(device)
    board = Board.create_board("S3")
    assert isinstance(board, debugboard.BughopperV1Board)
    for name in BUGHOPPER_METHODS:
        assert name in board.quick_methods


def test_create_board_bughopper_v2_exits_without_hid(patch_usb_find, monkeypatch):
    """If the optional hid module isn't loaded, BughopperV2 detection aborts."""
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S4"
    )
    patch_usb_find(device)
    monkeypatch.delitem(sys.modules, "hid", raising=False)

    with pytest.raises(SystemExit):
        Board.create_board("S4")


def test_create_board_dispatches_bughopper_v2_with_hid(patch_usb_find, monkeypatch):
    """With a (fake) hid module present, BughopperV2 is constructed."""
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S5"
    )
    patch_usb_find(device)

    hid_device = MagicMock(name="hid_device")
    hid_device.serial = "S5"
    fake_hid = types.ModuleType("hid")
    fake_hid.Device = MagicMock(return_value=hid_device)

    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    monkeypatch.setattr(debugboard, "hid", fake_hid, raising=False)

    board = Board.create_board("S5")
    assert isinstance(board, debugboard.BughopperV2Board)
    for name in BUGHOPPER_METHODS:
        assert name in board.quick_methods


def test_bughopper_v2_hid_writes_prefix_report_id(patch_usb_find, monkeypatch):
    """Every HID write must begin with a 0x00 report-ID placeholder.

    hidapi consumes byte 0 of a write as the HID report ID. Without the 0x00
    prefix the command byte is swallowed as a (non-existent) report ID on
    Windows and the firmware never sees the GPIO command (the board silently
    fails to reboot). Assert the payload survives intact behind the prefix.
    """
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S6"
    )
    patch_usb_find(device)

    hid_device = MagicMock(name="hid_device")
    hid_device.serial = "S6"
    fake_hid = types.ModuleType("hid")
    fake_hid.Device = MagicMock(return_value=hid_device)

    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    monkeypatch.setattr(debugboard, "hid", fake_hid, raising=False)

    board = Board.create_board("S6")
    board.bootToEDL()

    writes = [call.args[0] for call in hid_device.write.call_args_list]
    assert writes, "bootToEDL wrote nothing"
    for payload in writes:
        assert payload[0] == 0x00, f"missing report-ID prefix: {list(payload)}"
        # byte 1 is the application command (CMD_GPIO), not stripped as report ID
        assert payload[1] == board.CMD_GPIO


def test_bughopper_v2_wake_up_pulses_vol_down(patch_usb_find, monkeypatch):
    """wakeUp asserts and releases VOL_DOWN only, leaving the other lines alone."""
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S7"
    )
    patch_usb_find(device)

    hid_device = MagicMock(name="hid_device")
    hid_device.serial = "S7"
    fake_hid = types.ModuleType("hid")
    fake_hid.Device = MagicMock(return_value=hid_device)

    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    monkeypatch.setattr(debugboard, "hid", fake_hid, raising=False)
    monkeypatch.setattr(debugboard, "sleep", lambda s: None)

    board = Board.create_board("S7")
    board.wakeUp()

    writes = [call.args[0] for call in hid_device.write.call_args_list]
    vol_down = board.VOL_DOWN_BIT
    assert writes == [
        bytes([0x00, board.CMD_GPIO, vol_down, vol_down]),
        bytes([0x00, board.CMD_GPIO, 0x00, vol_down]),
    ]


def test_bughopper_v1_wake_up_pulses_vol_down(patch_usb_find, monkeypatch):
    """wakeUp asserts and releases VOL_DOWN, keeping power enabled throughout."""
    device = make_usb_device(Board.ID_VENDOR_FTDI, Board.ID_PRODUCT_BUGHOPPER_V1, "S8")
    patch_usb_find(device)
    monkeypatch.setattr(debugboard, "sleep", lambda s: None)

    board = Board.create_board("S8")
    bitmodes = []
    monkeypatch.setattr(board, "_ftdi_set_bitmode", bitmodes.append)
    board.wakeUp()

    masks = board.POWER_DISABLE_MASK | board.EDL_MASK | board.VOL_DOWN_MASK
    assert bitmodes == [masks | board.VOL_DOWN_BIT, masks]
