#!/usr/bin/env python3

# Copyright (c) 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import os
import sys
from argparse import ArgumentParser

from . import __version__, boardconfig

logger = logging.getLogger()


def _setup_logging(level):
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def build_parser():
    parser = ArgumentParser(
        prog="pytactl",
        description="Test Automation Controller (TAC/Alpaca) for Qualcomm debug boards.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Options shared by every subcommand.
    base = ArgumentParser(add_help=False)
    base.add_argument("--log-level", default="DEBUG", help="Log level (default: DEBUG)")

    # Options shared by the board-driving subcommands.
    common = ArgumentParser(add_help=False)
    common.add_argument(
        "--serial",
        nargs="+",
        help="Debug board serial number(s)",
    )
    # Where to load board files from.
    boards = ArgumentParser(add_help=False)
    boards.add_argument(
        "--boards-dir",
        default=boardconfig.BOARDS_DIR,
        help="Directory of board files, with their profiles in a 'profiles' "
        "directory beside them (default: the board files bundled with pytactl)",
    )
    board_file = ArgumentParser(add_help=False)
    board_file.add_argument(
        "--board-file",
        help="Drive a board described by this board file, without looking for "
        "a connected board; use for trying a board file out.",
    )

    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="COMMAND")

    subparsers.add_parser(
        "list",
        parents=[base],
        help="List connected debug boards and their serial numbers",
    )

    subparsers.add_parser(
        "shell",
        parents=[base, common, boards, board_file],
        help="Run the interactive shell",
    )

    oneshot = subparsers.add_parser(
        "oneshot",
        parents=[base, common, boards, board_file],
        help="Run a single command and exit",
    )
    oneshot.add_argument("command", help="Command to run, e.g. bootToEDL")
    oneshot.add_argument(
        "value",
        nargs="?",
        help="Optional integer value for pin commands, e.g. 1",
    )

    subparsers.add_parser(
        "listconfigs",
        parents=[base, boards],
        help="List the board files and check that each one loads",
    )

    showconfig = subparsers.add_parser(
        "showconfig",
        parents=[base, boards],
        help="Show a board's pins, timings and functions, and which file each "
        "comes from",
    )
    showconfig.add_argument(
        "board", help="Board file, or its name in the boards directory (ftdi-65)"
    )
    showconfig.add_argument(
        "--scripts", action="store_true", help="Include the function bodies"
    )

    service = subparsers.add_parser(
        "service", parents=[base, common, boards], help="Run the REST API service"
    )
    service.add_argument(
        "--hostname",
        default="0.0.0.0",
        help="Host name the server attaches to (default: 0.0.0.0)",
    )
    service.add_argument(
        "--port",
        default=5000,
        type=int,
        help="Port on the host to attach to (default: 5000)",
    )
    return parser


def _list_boards():
    from .debugboard import Board

    boards = Board.list_boards()
    if not boards:
        print("No connected debug boards found.")
        return

    print("Connected debug boards:")
    for board in boards:
        serial = board["serial"] or "<no serial reported>"
        vid_pid = f"{board['vid']:04x}:{board['pid']:04x}"
        print(f"  {board['type']:<14} vid:pid={vid_pid}  serial={serial}")


def _list_configs(boards_dir):
    """Print every board file in ``boards_dir``; False if any fails to load."""
    files = boardconfig.board_files(boards_dir)
    print(f"Board files in {boards_dir}:")
    if not files:
        print("  none")
        return False
    broken = 0
    for path in files:
        name = os.path.basename(path)[: -len(boardconfig.EXTENSION)]
        try:
            config = boardconfig.load_board(path)
        except boardconfig.ConfigError as error:
            broken += 1
            print(f"  {name:<16} INVALID")
            for line in str(error).splitlines():
                print(f"      {line}")
            continue
        keys = [
            f"{key}={getattr(config, key)!r}"
            for key in ("usb_product", "board_id", "serial_prefix")
            if getattr(config, key)
        ]
        if config.fallback:
            keys.append("fallback")
        print(
            f"  {name:<16} {config.platform:<8} {config.name}"
            + (f"  [{'; '.join(keys)}]" if keys else "")
        )
    if broken:
        print(f"{broken} board file(s) do not load")
        return False
    return True


def _show_config(boards_dir, board, scripts):
    path = board
    if not os.path.isfile(path):
        path = os.path.join(boards_dir, board + boardconfig.EXTENSION)
    try:
        config = boardconfig.load_board(path)
    except boardconfig.ConfigError as error:
        print(error)
        return False
    print(boardconfig.describe(config, functions=scripts))
    return True


def _single_board_serial(boards):
    if not boards:
        return None, "no connected debug boards found"

    if len(boards) > 1:
        serials = ", ".join(
            board.get("serial") or "<no serial reported>" for board in boards
        )
        return None, f"multiple connected debug boards found: {serials}"

    serial = boards[0].get("serial")
    if not serial:
        return None, "the connected debug board did not report a serial number"

    return serial, None


def _resolve_serial(args, parser, command):
    if args.serial:
        return args.serial[0]

    if args.board_file:
        return None

    from .debugboard import Board

    serial, error = _single_board_serial(Board.list_boards())
    if serial:
        logger.info("Using only connected debug board serial %s", serial)
        return serial

    parser.error(f"{command} requires --serial or --board-file ({error})")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.mode == "list":
        _list_boards()
    elif args.mode == "listconfigs":
        if not _list_configs(args.boards_dir):
            sys.exit(1)
    elif args.mode == "showconfig":
        if not _show_config(args.boards_dir, args.board, args.scripts):
            sys.exit(1)
    elif args.mode == "service":
        if not args.serial:
            parser.error("service requires --serial")
        from .service import run_service

        run_service(args.serial, args.boards_dir, args.hostname, args.port)
    elif args.mode == "oneshot":
        from .shell import run_oneshot

        serial = _resolve_serial(args, parser, "oneshot")
        run_oneshot(args.command, serial, args.board_file, args.boards_dir, args.value)
    else:  # shell
        from .shell import run_shell

        serial = _resolve_serial(args, parser, "shell")
        run_shell(serial, args.board_file, args.boards_dir)


if __name__ == "__main__":
    main()
