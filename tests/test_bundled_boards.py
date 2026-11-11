# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Checks on the board files bundled with pytactl in ``pytactl/boards/``."""

import collections
import os

import pytest

from pytactl import boardconfig

BOARD_FILES = boardconfig.board_files()
PROFILES_DIR = os.path.join(boardconfig.BOARDS_DIR, boardconfig.PROFILES_DIRNAME)

requires_boards = pytest.mark.skipif(
    not BOARD_FILES, reason="the board files bundled in pytactl/boards are not present"
)


def board_id(path):
    return os.path.basename(path)


@requires_boards
@pytest.mark.parametrize("path", BOARD_FILES, ids=board_id)
def test_board_loads(path):
    config = boardconfig.load_board(path)
    assert config.name
    assert config.platform in boardconfig.PLATFORMS


@requires_boards
def test_every_board_is_recognised_by_its_own_keys():
    """No two boards claim the same USB product string, PSOC board id or
    PIC32CX serial prefix, and exactly one is the FTDI fallback."""
    claims = collections.defaultdict(list)
    fallbacks = []
    for path in BOARD_FILES:
        config = boardconfig.load_board(path)
        for key in ("usb_product", "board_id", "serial_prefix"):
            for value in getattr(config, key):
                claims[(key, value)].append(board_id(path))
        if config.fallback:
            fallbacks.append(board_id(path))
    assert {claim: paths for claim, paths in claims.items() if len(paths) > 1} == {}
    assert len(fallbacks) == 1


@requires_boards
def test_every_profile_is_used():
    used = set()
    for path in BOARD_FILES:
        used.update(boardconfig.load_board(path).extends)
    profiles = {
        name[: -len(boardconfig.EXTENSION)]
        for name in os.listdir(PROFILES_DIR)
        if name.endswith(boardconfig.EXTENSION)
    }
    assert profiles - used == set()


@requires_boards
def test_find_board_reaches_every_recognisable_board():
    for path in BOARD_FILES:
        config = boardconfig.load_board(path)
        for key in ("usb_product", "board_id", "serial_prefix"):
            for value in getattr(config, key):
                found = boardconfig.find_board(**{key: value})
                assert found.path == config.path
