# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for the parts of scripts/sync_upstream_configs.py that deal with the
config layout: loading configs whose script is split between .alpaca files,
describing a difference, and removing a config."""

import os
import sys

import pytest

from pytactl import tacconfig

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if not os.path.isfile(os.path.join(SCRIPTS, "sync_upstream_configs.py")):
    # The source distribution ships the tests but not the maintainer scripts.
    pytest.skip(
        "scripts/sync_upstream_configs.py is not present", allow_module_level=True
    )
sys.path.insert(0, SCRIPTS)

import sync_upstream_configs as sync  # noqa: E402

SCRIPT = (
    "def powerOn()\n\tbattery 0\n\tdelay $powerKeyPress\n\tpkey 1\n\n"
    "def bootToEDL()\n\tpedl 1\n\tdelay $edl\n\tpedl 0\n"
)


def config(script=SCRIPT, **values):
    variables = {"powerKeyPress": "800", "edl": "1300", **values}
    return {
        "format": tacconfig.PINOUT_FORMAT,
        "name": "Test Board",
        "script": script,
        "variables": [{"name": k, "default_value": v} for k, v in variables.items()],
    }


def test_load_config_assembles_the_script(tmp_path):
    tacconfig.write_pinout(str(tmp_path), "TAC_FTDI_999.pinout.json", config())

    loaded = sync.load_config(str(tmp_path / "TAC_FTDI_999.pinout.json"))

    assert loaded["script"] == SCRIPT
    assert sync.load_config(str(tmp_path / "missing.pinout.json")) is None


def test_a_timing_change_names_the_function_it_affects():
    detail = sync.describe(config(), config(edl="2000"))
    assert detail == "changed: `variables` (`bootToEDL`)"


def test_a_script_change_names_the_function_it_affects():
    changed = SCRIPT.replace("\tpkey 1\n", "\tpkey 1\n\tvolup 1\n")
    assert sync.describe(config(), config(changed)) == "changed: `script` (`powerOn`)"


def test_a_layout_change_is_reported_as_such():
    relaid = SCRIPT.replace("\tdelay $edl\n", "\t// hold\n\tdelay $edl\n")
    assert sync.describe(config(), config(relaid)) == "changed: `script` (layout only)"


def test_other_fields_are_reported_without_functions():
    upstream = dict(config(), name="Renamed")
    assert sync.describe(config(), upstream) == "changed: `name`"


def test_remove_config_removes_the_board_script(tmp_path):
    tacconfig.write_pinout(str(tmp_path), "TAC_FTDI_999.pinout.json", config())
    assert (tmp_path / "TAC_FTDI_999.alpaca").exists()

    sync.remove_config(str(tmp_path), "TAC_FTDI_999.pinout.json")

    assert os.listdir(tmp_path) == []
