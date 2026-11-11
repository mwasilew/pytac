# Copyright (c)i 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import sys
from cmd import Cmd

from . import boardconfig
from .debugboard import Board, ConfigScriptError

logger = logging.getLogger()


def make_h(name, comment=None):
    def h(cls, *args):
        print("Set GPIO value to HIGH (1) or LOW (0)")
        print("When called without parameters, GPIO is set to LOW")
        if comment:
            print(comment)

    return h


def make_f(quick_method):
    def f(cls, *args):
        quick_method.call()

    return f


class AlpacaCmd(Cmd):
    def do_quit(self, line):
        print("Quitting")
        return True

    do_EOF = do_quit


def _create_board(serial, board_file, boards_dir):
    board = None
    try:
        if serial:
            board = Board.create_board(serial, boards_dir or boardconfig.BOARDS_DIR)
        if board_file:
            board = Board.create_from_config(board_file)
    except ConfigScriptError as error:
        # A defect in a board file, which the message describes and the user
        # can act on. A traceback would only bury it.
        logger.error("%s", error)
        sys.exit(1)

    if board is None:
        logger.error(f"Failed to create board with serial {serial}, board not found")
        sys.exit(1)
    return board


def run_oneshot(command, serial=None, board_file=None, boards_dir=None, value=None):
    board = _create_board(serial, board_file, boards_dir)

    func = getattr(board, command, None)
    if not callable(func):
        available = sorted([*board.quick_methods, *board.commands])
        logger.error(
            f"Unknown command '{command}'. Available commands: {', '.join(available)}"
        )
        sys.exit(1)

    if value is None:
        func()
    else:
        try:
            value = int(value)
        except ValueError:
            logger.error(f"Command '{command}' value must be an integer, got '{value}'")
            sys.exit(1)
        func(value)


def run_shell(serial=None, board_file=None, boards_dir=None):
    board = _create_board(serial, board_file, boards_dir)

    for pin in board.pins.values():
        if not pin.command:
            continue
        method_name = f"do_{pin.command}"
        help_name = f"help_{pin.command}"
        logger.debug(f"Adding {method_name}")
        method = pin.set
        help_f = make_h(pin.command)
        setattr(AlpacaCmd, method_name, method)
        setattr(AlpacaCmd, help_name, help_f)

    for name, method in board.quick_methods.items():
        method_name = f"do_{name}"
        logger.debug(f"Adding {method_name}")
        setattr(AlpacaCmd, method_name, make_f(method))

    cmd = AlpacaCmd()
    cmd.cmdloop()
