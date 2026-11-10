# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for pin polarity: a pin marked ``inverted`` is driven at the inverse
of the commanded level, initial value included."""

import pytest

from pytactl import debugboard


class FakeFtdiPort:
    def __init__(self):
        self.status = 0
        self.writes = []

    def write(self, value):
        self.status = value
        self.writes.append(value)


class FakePinPort:
    def __init__(self):
        self.writes = []

    def write(self, value, pin):
        self.writes.append((pin, value))


def pin_config(inverted, initial_value=False):
    return {
        "bus": "C",
        "command": "battery",
        "pin_number": "3",
        "initial_value": initial_value,
        "input": False,
        "inverted": inverted,
    }


@pytest.mark.parametrize(
    ("inverted", "initial", "levels"),
    [
        (False, False, [0, 1, 0]),
        (False, True, [1, 1, 0]),
        (True, False, [1, 0, 1]),
        (True, True, [0, 0, 1]),
    ],
)
def test_ftdi_pin_level(inverted, initial, levels):
    pin = debugboard.FtdiPin(None, pin_config(inverted, initial))
    port = FakeFtdiPort()

    pin.setPort(port)  # drives the initial value
    pin.set(1)
    pin.set(0)

    assert [int(bool(write & (1 << 3))) for write in port.writes] == levels
    assert pin.value == 0  # the commanded value, not the level driven


@pytest.mark.parametrize("pin_class", [debugboard.PsocPin, debugboard.Pic32cxPin])
@pytest.mark.parametrize(("inverted", "levels"), [(False, [1, 0]), (True, [0, 1])])
def test_serial_pin_level(pin_class, inverted, levels):
    pin = pin_class(None, pin_config(inverted))
    port = FakePinPort()
    pin.port = port

    pin.set(1)
    pin.set(0)

    assert port.writes == [(3, level) for level in levels]


def test_ftdi_pin_leaves_other_pins_alone():
    pin = debugboard.FtdiPin(None, pin_config(True))
    port = FakeFtdiPort()
    port.status = 0b1000_0001

    pin.port = port
    pin.set(1)

    assert port.status == 0b1000_0001
    pin.set(0)
    assert port.status == 0b1000_1001
