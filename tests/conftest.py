# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Shared fixtures and helpers for the pytactl test suite.

The tests mock the USB layer and the GPIO/serial hardware, so that every board
file bundled in ``pytactl/boards/`` can be driven through the board class its
platform uses - FtdiBoard, PsocBoard or Pic32cxBoard - as
``pytactl.shell`` and ``pytactl.service`` do.
"""

import os
from unittest.mock import MagicMock

import pytest

from pytactl import boardconfig, debugboard

BOARD_FILES = boardconfig.board_files()

requires_boards = pytest.mark.skipif(
    not BOARD_FILES, reason="the board files bundled in pytactl/boards are not present"
)

# USB vendor/product pairs that Board.create_board() dispatches on.
FTDI_VENDOR = debugboard.Board.ID_VENDOR_FTDI  # 0x0403
FTDI_PRODUCT = debugboard.Board.ID_PRODUCT_FTDI  # 0x6011
QCOM_VENDOR = debugboard.Board.ID_VENDOR_QCOM  # 0x05C6
QCOM_PRODUCT = debugboard.Board.ID_PRODUCT_QCOM  # 0x9302

BOARD_CLASSES = {
    "FTDI": debugboard.FtdiBoard,
    "PSOC": debugboard.PsocBoard,
    "PIC32CX": debugboard.Pic32cxBoard,
}


def board_params():
    """pytest params for every bundled board file, by file name."""
    if not BOARD_FILES:
        return [pytest.param(None, id="no-boards", marks=requires_boards)]
    return [pytest.param(path, id=os.path.basename(path)) for path in BOARD_FILES]


def make_usb_device(vendor, product_id, serial, product_str=""):
    """Build a fake pyusb device exposing the attributes debugboard reads."""
    dev = MagicMock(name="usb_device")
    dev.idVendor = vendor
    dev.idProduct = product_id
    dev.serial_number = serial
    dev.product = product_str
    return dev


@pytest.fixture
def patch_usb_find(monkeypatch):
    """Return a helper that makes ``usb.core.find`` return a given device."""

    def _install(device):
        monkeypatch.setattr(debugboard.usb.core, "find", lambda **kwargs: device)
        return device

    return _install


@pytest.fixture(autouse=True)
def mock_hardware(monkeypatch):
    """Replace every real hardware touch point with harmless fakes.

    - ``GpioAsyncController`` (FTDI GPIO) becomes a MagicMock, so FtdiPort/FtdiPin
      logic runs without a real FTDI chip.
    - ``PsocPort`` (which opens a serial port in __init__) is swapped for a fake.
    - ``sleep`` is neutralised so config scripts with ``delay`` run instantly.
    """
    monkeypatch.setattr(debugboard, "GpioAsyncController", MagicMock())
    monkeypatch.setattr(debugboard, "sleep", lambda *a, **k: None)

    class _FakePsocPort(debugboard.Port):
        def __init__(self, serialid):
            debugboard.Port.__init__(self, None, serialid)
            self.writes = []
            self.calls = []

        def write(self, value, pin=None):
            self.writes.append((value, pin))

        def call_method(self, method, value):
            self.calls.append((method, value))

        def close(self):
            pass

    monkeypatch.setattr(debugboard, "PsocPort", _FakePsocPort)

    class _FakePic32cxPort(debugboard.Port):
        """Stand-in for Pic32cxPort, which otherwise opens a real CDC serial
        port via udev in __init__."""

        def __init__(self, serialid):
            debugboard.Port.__init__(self, None, serialid)
            self.writes = []

        def write(self, value, pin=None):
            self.writes.append((value, pin))

        def close(self):
            pass

    monkeypatch.setattr(debugboard, "Pic32cxPort", _FakePic32cxPort)


def connect(platform, patch_usb_find, monkeypatch, key=None):
    """Plug in a fake board of ``platform`` and return what create_board makes.

    ``key`` is what the board reports: its USB product string (FTDI), board
    id (PSOC) or serial prefix (PIC32CX).
    """
    if platform == "FTDI":
        patch_usb_find(
            make_usb_device(FTDI_VENDOR, FTDI_PRODUCT, "FTDI_SERIAL", key or "")
        )
        return debugboard.Board.create_board("FTDI_SERIAL")
    if platform == "PSOC":
        patch_usb_find(make_usb_device(QCOM_VENDOR, QCOM_PRODUCT, "PSOC_SERIAL"))
        # The board id is read over a serial console; report ``key`` instead.
        monkeypatch.setattr(
            debugboard.PsocBoard, "_PsocBoard__get_board_id", lambda self: key
        )
        return debugboard.Board.create_board("PSOC_SERIAL")
    # The PIC32CX board is invisible to libusb, and found through udev.
    patch_usb_find(None)
    monkeypatch.setattr(
        debugboard.Pic32cxBoard, "detect", staticmethod(lambda serial: True)
    )
    return debugboard.Board.create_board(f"{key or 'NONE'}XX01")


def load_board(path, patch_usb_find, monkeypatch):
    """Drive the board file ``path`` through its platform's board class.

    The board is connected as a board nothing else matches, with find_board
    handing over this board file: how a board is recognised is tested on its
    own, and this reaches boards no key leads to as well.
    """
    config = boardconfig.load_board(path)
    monkeypatch.setattr(boardconfig, "find_board", lambda *args, **kwargs: config)
    return connect(config.platform, patch_usb_find, monkeypatch)
