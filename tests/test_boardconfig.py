# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for :mod:`pytactl.boardconfig`: board files built on profiles, the
script statements, and the checks that say where a problem is."""

import textwrap

import pytest

from pytactl import boardconfig

FTDI = """
platform = "FTDI"
gpio_buses = ["C", "D"]

[pins]
D1 = { command = "battery", inverted = true }
D3 = "pkey"
C6 = "usb1"
D6 = { command = "ready", initial = 1 }
"""

SCRIPT = '''
[timings]
press = 800
settle = 30

[functions]
powerOff = """
battery 1
usb1 1
"""
powerOn = """
powerOff
battery 0   // power
delay $settle
pkey 1
delay $press
pkey 0
"""
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


@pytest.fixture
def boards(tmp_path):
    """A boards directory with an ftdi pin map and a script profile."""
    write(tmp_path / "profiles" / "ftdi.toml", FTDI)
    write(tmp_path / "profiles" / "script.toml", SCRIPT)
    return tmp_path


def board(boards, text, name="board"):
    return boardconfig.load_board(write(boards / f"{name}.toml", text))


def error(boards, text):
    with pytest.raises(boardconfig.ConfigError) as excinfo:
        board(boards, text)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def test_board_takes_everything_from_its_profiles(boards):
    config = board(boards, 'extends = ["ftdi", "script"]\n')

    assert config.platform == "FTDI"
    assert config.extends == ["ftdi", "script"]
    assert sorted(config.commands) == ["battery", "pkey", "ready", "usb1"]
    assert config.pins["D1"].inverted
    assert config.pins["D6"].initial == 1
    assert [str(s) for s in config.functions["powerOn"].statements] == [
        "powerOff",
        "battery 0",
        "delay $settle",
        "pkey 1",
        "delay $press",
        "pkey 0",
    ]
    assert config.timings["press"].value == 800


def test_later_layers_win(boards):
    config = board(
        boards,
        '''
        extends = ["ftdi", "script"]

        [pins]
        D3 = { command = "pkey", inverted = true }

        [timings]
        press = 1500

        [functions]
        powerOff = """
        battery 1
        """
        ''',
    )

    assert config.pins["D3"].inverted
    assert config.pins["D3"].origin.layer == "board"
    assert config.timings["press"].value == 1500
    assert config.timings["press"].origin.layer == "board"
    assert config.timings["settle"].origin.layer == "script"
    assert [str(s) for s in config.functions["powerOff"].statements] == ["battery 1"]
    assert config.functions["powerOn"].origin.layer == "script"


def test_giving_a_command_to_another_pin_moves_it(boards):
    config = board(boards, 'extends = ["ftdi", "script"]\n[pins]\nC2 = "usb1"\n')

    assert config.commands["usb1"].pin == "C2"
    assert "C6" not in config.pins


def test_false_removes_a_pin_or_function(boards):
    config = board(
        boards,
        """
        extends = ["ftdi"]

        [pins]
        D6 = false
        """,
    )

    assert "D6" not in config.pins
    assert config.removed_pins["D6"].layer == "board"


def test_removing_a_function_others_call_is_an_error(boards):
    message = error(
        boards,
        """
        extends = ["ftdi", "script"]

        [functions]
        powerOff = false
        """,
    )

    assert "there is no function 'powerOff'" in message
    assert "(powerOn)" in message


def test_pin_without_a_command_only_has_an_initial_level(boards):
    config = board(boards, 'extends = ["ftdi"]\n[pins]\nC0 = { initial = 1 }\n')

    assert config.pins["C0"].command is None
    assert config.pins["C0"].initial == 1
    assert "C0" not in {pin.pin for pin in config.commands.values()}


def test_a_board_can_stand_alone(boards):
    config = board(
        boards,
        """
        platform = "PSOC"

        [pins]
        53 = "battery"
        """,
    )

    assert config.extends == []
    assert config.pins["53"].number == 53
    assert config.pins["53"].bus is None


def test_profiles_can_extend_profiles(boards):
    write(boards / "profiles" / "both.toml", 'extends = ["ftdi", "script"]\n')
    config = board(boards, 'extends = ["both"]\n')

    assert config.extends == ["ftdi", "script", "both"]
    assert "powerOn" in config.functions


# ---------------------------------------------------------------------------
# Script statements
# ---------------------------------------------------------------------------


def test_statements_and_comments():
    function, errors = boardconfig.parse_function(
        "f",
        "pkey 1 // press\n\n# a comment\ndelay 800\ndelay $edl\npowerOff\n",
        boardconfig.Location("x.toml", 10),
        11,
    )

    assert errors == []
    assert [(s.kind, s.target, s.value) for s in function.statements] == [
        ("set", "pkey", 1),
        ("delay", "", 800),
        ("delay", "edl", 0),
        ("call", "powerOff", 0),
    ]
    assert [s.location.line for s in function.statements] == [11, 14, 15, 16]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("pkey 2", "pkey takes 0 or 1"),
        ("delay soon", "cannot wait for 'soon'"),
        ("pkey 1 now", "cannot read 'pkey 1 now'"),
        ("logComment hello there", "cannot read"),
    ],
)
def test_statements_that_cannot_be_read(line, message):
    _, errors = boardconfig.parse_function(
        "f", line, boardconfig.Location("x.toml", 3), 4
    )
    assert len(errors) == 1
    assert errors[0].startswith("x.toml:4 (f): ")
    assert message in errors[0]


# ---------------------------------------------------------------------------
# Checks, and where they point
# ---------------------------------------------------------------------------


def test_errors_name_the_file_and_line(boards):
    message = error(
        boards,
        '''
        extends = ["ftdi", "script"]

        [functions]
        broken = """
        pkey 1
        volup 1
        delay $nope
        missing
        """
        ''',
    )

    path = boards / "board.toml"
    assert f"{path}:6 (broken): no pin has the command 'volup'" in message
    assert f"{path}:7 (broken): there is no timing 'nope'" in message
    assert f"{path}:8 (broken): there is no function 'missing'" in message


def test_ftdi_pin_on_a_bus_that_is_not_gpio(boards):
    message = error(boards, 'extends = ["ftdi"]\n[pins]\nA3 = "uart"\n')
    assert "pin A3 (uart) is on bus A, which is not a GPIO bus" in message


@pytest.mark.parametrize(
    ("pins", "message"),
    [
        ('X9 = "a"', "X9 is not an FTDI pin"),
        ('C1 = "a"\nC2 = "a"', "command 'a' is given to both C1 and C2"),
        ('C1 = "12v"', "'12v' is not a usable command name"),
        ('C1 = { command = "a", level = 1 }', "unknown keys ['level']"),
        ("C1 = { inverted = true }", "needs a command, an initial level, or false"),
    ],
)
def test_bad_pins(boards, pins, message):
    assert message in error(boards, f'extends = ["ftdi"]\n[pins]\n{pins}\n')


def test_function_named_like_a_command(boards):
    message = error(
        boards,
        'extends = ["ftdi"]\n[functions]\npkey = """\nbattery 1\n"""\n',
    )
    assert "function pkey has the name of the command of pin D3" in message


def test_recursion(boards):
    message = error(
        boards,
        'extends = ["ftdi"]\n[functions]\na = """\nb\n"""\nb = """\na\n"""\n',
    )
    assert "a calls itself" in message and "b calls itself" in message


def test_unknown_and_board_only_keys(boards):
    write(boards / "profiles" / "named.toml", 'name = "x"\n')
    assert "unknown key 'colour'" in error(boards, 'extends = ["ftdi"]\ncolour = 1\n')
    assert "only a board file can set it" in error(boards, 'extends = ["named"]\n')


def test_missing_profile_and_cycles(boards):
    assert "there is no" in error(boards, 'extends = ["nowhere"]\n')
    write(boards / "profiles" / "a.toml", 'extends = ["b"]\n')
    write(boards / "profiles" / "b.toml", 'extends = ["a"]\n')
    assert "extends 'a'" in error(boards, 'extends = ["a"]\n')


def test_a_board_needs_a_platform(boards):
    assert "no platform" in error(boards, 'extends = ["script"]\n')


def test_unreadable_toml(boards):
    assert "board.toml" in error(boards, "extends = [\n")


# ---------------------------------------------------------------------------
# Finding a board, and describing one
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(boards):
    write(boards / "one.toml", 'usb_product = "ONE"\nextends = ["ftdi"]\n')
    write(boards / "two.toml", 'board_id = [2, 22]\nplatform = "PSOC"\n')
    write(boards / "three.toml", 'serial_prefix = "THREE"\nplatform = "PIC32CX"\n')
    write(boards / "fallback.toml", 'fallback = true\nextends = ["ftdi"]\n')
    return boards


def test_find_board(catalog):
    find = boardconfig.find_board
    assert find(catalog, usb_product="ONE").path.endswith("one.toml")
    assert find(catalog, board_id=22).path.endswith("two.toml")
    assert find(catalog, serial_prefix="THREE").path.endswith("three.toml")
    assert find(catalog, usb_product="nope") is None
    assert find(catalog, usb_product="nope", fallback=True).path.endswith(
        "fallback.toml"
    )


def test_describe_says_where_everything_comes_from(boards):
    config = board(
        boards,
        """
        name = "Test"
        usb_product = "TEST"
        extends = ["ftdi", "script"]

        [pins]
        C6 = false

        [timings]
        press = 1500

        [functions]
        powerOff = '''
        battery 1
        '''
        """,
    )

    text = boardconfig.describe(config)

    assert "recognised by: usb_product = ['TEST']" in text
    assert "extends: ftdi, script" in text
    assert "battery (inverted)" in text
    assert "(not connected)" in text and "C6" in text
    lines = {line.split()[0]: line for line in text.splitlines() if line.strip()}
    assert lines["press"].endswith("board")
    assert lines["settle"].endswith("script")
    assert lines["powerOff"].endswith("board")
    assert lines["powerOn"].endswith("script")
    assert "      battery 1" in boardconfig.describe(config, functions=True)
