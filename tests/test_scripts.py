# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Running a board file's script: what a quick method does, and how a board
file pytactl cannot use reaches the user."""

import logging
import textwrap

import pytest
from conftest import connect

from pytactl import boardconfig, debugboard, shell

BOARD = '''
name = "Test"
platform = "FTDI"
gpio_buses = ["D"]

[pins]
D1 = { command = "battery", inverted = true }
D3 = "pkey"
D6 = { initial = 1 }

[timings]
press = 800

[functions]
powerOff = """
battery 1
"""
powerOn = """
powerOff
battery 0
delay 30
pkey 1
delay $press   // hold the key
pkey 0
"""
'''


def write(tmp_path, text, name="board.toml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip())
    return str(path)


@pytest.fixture
def steps(monkeypatch):
    """Record what a board does: commanded pin levels and delays."""
    recorded = []
    original = debugboard.DummyPin.set

    def set_pin(pin, value):
        recorded.append((pin.command, value))
        original(pin, value)

    monkeypatch.setattr(debugboard.DummyPin, "set", set_pin)
    monkeypatch.setattr(
        debugboard.Board, "delay", lambda board, ms: recorded.append(("delay", ms))
    )
    return recorded


def test_quick_method_runs_its_statements_in_order(tmp_path, steps):
    board = debugboard.Board.create_from_config(write(tmp_path, BOARD))
    steps.clear()

    board.powerOn()

    assert steps == [
        ("battery", 1),
        ("battery", 0),
        ("delay", 30),
        ("pkey", 1),
        ("delay", 800),
        ("pkey", 0),
    ]


def test_pins_start_at_their_initial_level(tmp_path, patch_usb_find, monkeypatch):
    """Connecting drives every pin to its initial level: battery is inverted,
    so commanded 0 drives D1 high, and D6 has no command but starts high."""
    config = boardconfig.load_board(write(tmp_path, BOARD))
    monkeypatch.setattr(boardconfig, "find_board", lambda *args, **kwargs: config)

    board = connect("FTDI", patch_usb_find, monkeypatch)

    assert board.ports["D"].status == 0b0100_0010


def test_pin_without_a_command_is_not_a_command(tmp_path):
    board = debugboard.Board.create_from_config(write(tmp_path, BOARD))

    assert set(board.commands) == {"battery", "pkey"}
    assert set(board.pins) == {"D1", "D3", "D6"}
    assert set(board.quick_methods) == {"powerOff", "powerOn"}


def test_function_shadowing_a_board_method_is_refused(tmp_path):
    path = write(tmp_path, BOARD + 'delay = """\npkey 1\n"""\n')
    with pytest.raises(debugboard.ConfigScriptError, match="delay would replace"):
        debugboard.Board.create_from_config(path)


def test_cli_reports_a_board_file_defect_without_a_traceback(tmp_path, caplog):
    path = write(tmp_path, BOARD.replace("delay $press", "delay $hold"))

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as excinfo:
        shell.run_oneshot("powerOn", board_file=path)

    assert excinfo.value.code == 1
    assert f"{path}:" in caplog.text
    assert "(powerOn): there is no timing 'hold'" in caplog.text
