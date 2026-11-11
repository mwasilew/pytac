# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Board configurations: what a debug board's pins are wired to, and the
scripts that drive them.

Every board is a TOML file in a boards directory (the package's
``pytactl/boards/`` by default). A board file says which *profiles* it
builds on and what is particular to it; profiles, in ``profiles/`` beside the
board files, hold what boards share:

* a platform's default pin map (``ftdi``, ``psoc``),
* scripts - functions such as ``powerOn`` or ``bootToEDL`` - with default
  timings for their delays (``common``),
* small overlays that change a few of those functions (``common-no-usb1``).

A file is made of the same sections whether it is a profile or a board::

    extends = ["ftdi", "common"]   # profiles, applied in order
    platform = "FTDI"              # FTDI, PSOC or PIC32CX
    gpio_buses = ["C", "D"]        # FTDI buses driven as GPIO ports

    [pins]                         # physical pin -> command
    D1 = "battery"
    D6 = { command = "TC_READY_N", initial = 1 }
    C2 = false                     # not connected: drop an inherited pin
    46 = { initial = 0 }           # no command: only set when connecting

    [timings]                      # milliseconds, used as $name in scripts
    edl = 1300

    [functions]                    # one statement per line
    powerOn = '''
    powerOff
    powerOnTheDevice
    pkey 1
    delay $powerKeyPress
    pkey 0
    '''
    bootToSecondaryEDL = false     # drop an inherited function

A board file adds what identifies the board: ``name``, ``description`` and
how pytactl recognises it - ``usb_product`` (the USB product string of an
FTDI board), ``board_id`` (the id a PSOC board reports) or ``serial_prefix``
(the part of a PIC32CX board's serial number before "XX"). The one FTDI board
with ``fallback = true`` is used for FTDI boards nothing else matches.

Layers apply in order - the profiles a file extends, then the file itself -
and a later layer wins: a pin, timing or function it names replaces the
inherited one, and giving a command to a different pin moves it there.

A script statement is one of::

    pkey 1           drive the pin of command "pkey" to 1 (or 0)
    delay 800        wait 800 ms
    delay $edl       wait for the board's "edl" timing
    powerOffDevice   run another function

with ``//`` or ``#`` starting a comment. A pin marked ``inverted`` is driven
at the inverse of the commanded level, and ``initial`` is the level commanded
when the board is connected (0 by default). A pin without a command is set to
its initial level when the board is connected, and left alone after that.

Every name a script uses is checked when the board is loaded, and an error
names the file and line it comes from.
"""

import keyword
import os
import re
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

BOARDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards")
PROFILES_DIRNAME = "profiles"
EXTENSION = ".toml"

PLATFORMS = ("FTDI", "PSOC", "PIC32CX")
FTDI_BUSES = "ABCD"

# Keys a profile may set, and the ones only a board file may add.
_LAYER_KEYS = {"extends", "platform", "gpio_buses", "pins", "timings", "functions"}
_BOARD_KEYS = {
    "name",
    "description",
    "usb_product",
    "board_id",
    "serial_prefix",
    "fallback",
}
_PIN_KEYS = {"command", "inverted", "initial"}

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FTDI_PIN = re.compile(r"[A-D][0-7]\Z")
_NUMBERED_PIN = re.compile(r"[0-9]+\Z")


class ConfigError(Exception):
    """A board or profile file pytactl cannot use; the message says where."""


@dataclass(frozen=True)
class Location:
    """Where something was defined: a file, and a line in it when known."""

    path: str
    line: int | None = None

    @property
    def layer(self):
        """The file's name without its extension: the profile or board name."""
        return os.path.basename(self.path)[: -len(EXTENSION)]

    def __str__(self):
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class Pin:
    """A physical pin and the command that drives it."""

    pin: str  # as written: "D1" on FTDI, "53" on PSOC and PIC32CX
    command: str | None  # None: only set to its initial level when connecting
    inverted: bool = False
    initial: int = 0
    origin: Location | None = None

    @property
    def bus(self):
        """The FTDI bus letter, or ``None`` on a platform without buses."""
        return self.pin[0] if self.pin[0].isalpha() else None

    @property
    def number(self):
        return int(self.pin[1:]) if self.bus else int(self.pin)


@dataclass
class Statement:
    kind: str  # "set", "delay" or "call"
    target: str  # command, timing name ("" for a literal delay) or function
    value: int  # level for "set", milliseconds for a literal "delay"
    location: Location

    def __str__(self):
        if self.kind == "set":
            return f"{self.target} {self.value}"
        if self.kind == "delay":
            return f"delay ${self.target}" if self.target else f"delay {self.value}"
        return self.target


@dataclass
class Function:
    name: str
    statements: list
    source: str  # the text as written, comments included
    origin: Location


@dataclass
class Timing:
    value: int
    origin: Location


@dataclass
class BoardConfig:
    """A board with every layer applied: what pytactl drives."""

    path: str
    name: str = ""
    description: str = ""
    usb_product: list = field(default_factory=list)
    board_id: list = field(default_factory=list)
    serial_prefix: list = field(default_factory=list)
    fallback: bool = False
    extends: list = field(default_factory=list)
    platform: str = ""
    gpio_buses: list = field(default_factory=list)
    pins: dict = field(default_factory=dict)  # physical pin -> Pin
    timings: dict = field(default_factory=dict)  # name -> Timing
    functions: dict = field(default_factory=dict)  # name -> Function
    # What a layer removed, and where: physical pin / function -> Location.
    removed_pins: dict = field(default_factory=dict)
    removed_functions: dict = field(default_factory=dict)

    @property
    def commands(self):
        """Command -> Pin, for the pins that have a command."""
        return {pin.command: pin for pin in self.pins.values() if pin.command}

    def delay_ms(self, statement):
        """The milliseconds a ``delay`` statement waits on this board."""
        if statement.target:
            return self.timings[statement.target].value
        return statement.value


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def _read(path):
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    return data, _key_lines(text)


def _key_lines(text):
    """``{(section, key): (line, value starts on the next line)}`` of a file.

    Good enough for the files pytactl reads: a ``key =`` at the start of a
    line, outside a multi-line string, under a plain ``[section]`` header.
    """
    lines = {}
    section = ""
    in_string = None
    for number, line in enumerate(text.splitlines(), 1):
        if in_string:
            if line.count(in_string) % 2:
                in_string = None
            continue
        stripped = line.strip()
        header = re.match(r"\[([A-Za-z0-9_.-]+)\]\s*(#.*)?$", stripped)
        if header:
            section = header.group(1)
            continue
        match = re.match(r"""\s*("([^"]*)"|'([^']*)'|[A-Za-z0-9_-]+)\s*=(.*)$""", line)
        if not match:
            continue
        key = match.group(2) or match.group(3) or match.group(1)
        rest = match.group(4).strip()
        for quote in ('"""', "'''"):
            if rest.startswith(quote) and rest.count(quote) % 2:
                in_string = quote
                lines[(section, key)] = (number, rest == quote)
                break
        else:
            lines[(section, key)] = (number, False)
    return lines


def _location(path, key_lines, section, key):
    line, _ = key_lines.get((section, key), (None, False))
    return Location(path, line)


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def parse_function(name, text, origin, first_line=None):
    """Parse a function body into statements.

    ``first_line`` is the file line of the body's first line, when known, so
    that every statement can say where it is.
    """
    statements = []
    errors = []
    for offset, raw in enumerate(text.splitlines()):
        line = first_line + offset if first_line else None
        location = Location(origin.path, line or origin.line)
        code = re.split(r"//|#", raw, maxsplit=1)[0].strip()
        if not code:
            continue
        words = code.split()
        if words[0] == "delay" and len(words) == 2:
            argument = words[1]
            if argument.isdigit():
                statements.append(Statement("delay", "", int(argument), location))
            elif argument.startswith("$") and _NAME.match(argument[1:]):
                statements.append(Statement("delay", argument[1:], 0, location))
            else:
                errors.append(f"{location} ({name}): cannot wait for {argument!r}")
        elif len(words) == 2 and _NAME.match(words[0]):
            if words[1] not in ("0", "1"):
                errors.append(
                    f"{location} ({name}): {words[0]} takes 0 or 1, not {words[1]!r}"
                )
            else:
                statements.append(Statement("set", words[0], int(words[1]), location))
        elif len(words) == 1 and _NAME.match(words[0]):
            statements.append(Statement("call", words[0], 0, location))
        else:
            errors.append(f"{location} ({name}): cannot read {code!r}")
    return Function(name, statements, text, origin), errors


# ---------------------------------------------------------------------------
# Applying layers
# ---------------------------------------------------------------------------


def _as_list(value):
    return list(value) if isinstance(value, list) else [value]


def _apply(config, path, data, key_lines, is_board, errors):
    def where(section="", key=""):
        return _location(path, key_lines, section, key)

    allowed = _LAYER_KEYS | (_BOARD_KEYS if is_board else set())
    for key in data:
        if key not in allowed:
            hint = " (only a board file can set it)" if key in _BOARD_KEYS else ""
            errors.append(f"{where('', key)}: unknown key {key!r}{hint}")

    for key in ("name", "description"):
        if key in data:
            setattr(config, key, str(data[key]))
    for key in ("usb_product", "serial_prefix"):
        if key in data:
            setattr(config, key, [str(value) for value in _as_list(data[key])])
    if "board_id" in data:
        config.board_id = [int(value) for value in _as_list(data["board_id"])]
    if "fallback" in data:
        config.fallback = bool(data["fallback"])

    if "platform" in data:
        if data["platform"] not in PLATFORMS:
            errors.append(
                f"{where('', 'platform')}: platform must be one of "
                f"{', '.join(PLATFORMS)}, not {data['platform']!r}"
            )
        config.platform = data["platform"]
    if "gpio_buses" in data:
        config.gpio_buses = [str(bus) for bus in data["gpio_buses"]]

    assigned = {}
    for key, value in data.get("pins", {}).items():
        location = where("pins", key)
        if value is False:
            config.pins.pop(key, None)
            config.removed_pins[key] = location
            continue
        spec = {"command": value} if isinstance(value, str) else value
        if not isinstance(spec, dict) or not ({"command", "initial"} & set(spec)):
            errors.append(
                f"{location}: pin {key} needs a command, an initial level, or false"
            )
            continue
        unknown = set(spec) - _PIN_KEYS
        if unknown:
            errors.append(f"{location}: pin {key} has unknown keys {sorted(unknown)}")
        command = spec.get("command")
        if command is None:
            pass
        elif command in assigned:
            errors.append(
                f"{location}: command {command!r} is given to both "
                f"{assigned[command]} and {key}"
            )
        if command is not None:
            assigned[command] = key
        pin = Pin(
            key,
            command,
            bool(spec.get("inverted", False)),
            int(spec.get("initial", 0)),
            location,
        )
        if pin.initial not in (0, 1):
            errors.append(f"{location}: initial must be 0 or 1")
        config.removed_pins.pop(key, None)
        config.pins[key] = pin
    # A command given to a pin in this layer leaves the pin it had before.
    for key, pin in list(config.pins.items()):
        if pin.command and pin.command in assigned and assigned[pin.command] != key:
            del config.pins[key]

    for key, value in data.get("timings", {}).items():
        location = where("timings", key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{location}: timing {key} must be milliseconds, >= 0")
            continue
        config.timings[key] = Timing(value, location)

    for key, value in data.get("functions", {}).items():
        line, next_line = key_lines.get(("functions", key), (None, False))
        location = Location(path, line)
        if value is False:
            config.functions.pop(key, None)
            config.removed_functions[key] = location
            continue
        if not isinstance(value, str):
            errors.append(f"{location}: function {key} must be text, or false")
            continue
        first_line = (line + 1 if next_line else line) if line else None
        function, function_errors = parse_function(key, value, location, first_line)
        errors.extend(function_errors)
        config.removed_functions.pop(key, None)
        config.functions[key] = function


def _layers(path, profiles_dir, stack, seen, out):
    data, key_lines = _read(path)
    for name in _as_list(data.get("extends", [])):
        profile = os.path.join(profiles_dir, f"{name}{EXTENSION}")
        if not os.path.isfile(profile):
            raise ConfigError(f"{path}: extends {name!r}, but there is no {profile}")
        if profile in stack:
            raise ConfigError(f"{path}: extends {name!r}, which extends {path}")
        if profile not in seen:
            _layers(profile, profiles_dir, [*stack, path], seen, out)
    seen.add(path)
    out.append((path, data, key_lines))


def load_board(path, profiles_dir=None, check=True):
    """Load the board file ``path`` with every layer applied.

    Profiles are looked up in ``profiles_dir``, by default the ``profiles``
    directory beside the board file. Raises `ConfigError` naming every
    problem found, each with the file and line it comes from; without
    ``check``, only problems that stop the layers being applied are raised.
    """
    path = os.path.abspath(path)
    if profiles_dir is None:
        profiles_dir = os.path.join(os.path.dirname(path), PROFILES_DIRNAME)
    layers = []
    _layers(path, profiles_dir, [], set(), layers)

    config = BoardConfig(path=path)
    config.extends = [Location(layer).layer for layer, _, _ in layers[:-1]]
    errors = []
    for layer, data, key_lines in layers:
        _apply(config, layer, data, key_lines, layer == path, errors)
    if check:
        errors.extend(validate(config))
    if errors:
        raise ConfigError("\n".join(errors))
    return config


# ---------------------------------------------------------------------------
# Checking the result
# ---------------------------------------------------------------------------


def validate(config):
    """Problems with a board as its layers leave it, as messages."""
    errors = []
    where = Location(config.path)
    if not config.platform:
        errors.append(f"{where}: no platform; extend a platform profile or set one")
    if config.platform == "FTDI" and not config.gpio_buses:
        errors.append(f"{where}: an FTDI board needs gpio_buses")
    for bus in config.gpio_buses:
        if bus not in FTDI_BUSES or len(bus) != 1:
            errors.append(f"{where}: {bus!r} is not an FTDI bus")

    for key, pin in config.pins.items():
        if config.platform == "FTDI":
            if not _FTDI_PIN.match(key):
                errors.append(f"{pin.origin}: {key} is not an FTDI pin (A0 to D7)")
            elif pin.bus not in config.gpio_buses:
                errors.append(
                    f"{pin.origin}: pin {key} ({pin.command or 'no command'}) is "
                    f"on bus {pin.bus}, "
                    f"which is not a GPIO bus (gpio_buses = {config.gpio_buses})"
                )
        elif config.platform and not _NUMBERED_PIN.match(key):
            errors.append(f"{pin.origin}: {key} is not a {config.platform} pin number")
        if pin.command is None:
            continue
        if not _NAME.match(pin.command) or keyword.iskeyword(pin.command):
            errors.append(f"{pin.origin}: {pin.command!r} is not a usable command name")

    commands = config.commands
    for name in sorted(set(commands) & set(config.functions)):
        errors.append(
            f"{config.functions[name].origin}: function {name} has the name of "
            f"the command of pin {commands[name].pin} ({commands[name].origin})"
        )

    for function in config.functions.values():
        if not _NAME.match(function.name) or keyword.iskeyword(function.name):
            errors.append(f"{function.origin}: {function.name!r} is not a usable name")
        for statement in function.statements:
            problem = None
            if statement.kind == "set" and statement.target not in commands:
                problem = f"no pin has the command {statement.target!r}"
            elif statement.kind == "call" and statement.target not in config.functions:
                problem = f"there is no function {statement.target!r}"
            elif (
                statement.kind == "delay"
                and statement.target
                and statement.target not in config.timings
            ):
                problem = f"there is no timing {statement.target!r}"
            if problem:
                errors.append(f"{statement.location} ({function.name}): {problem}")

    for name in _recursive(config):
        errors.append(f"{config.functions[name].origin}: {name} calls itself")
    return errors


def _recursive(config):
    """Functions that end up calling themselves."""
    found = []
    for start in config.functions:
        pending = [start]
        visited = set()
        while pending:
            name = pending.pop()
            function = config.functions.get(name)
            for statement in function.statements if function else []:
                if statement.kind != "call":
                    continue
                if statement.target == start:
                    found.append(start)
                    pending = []
                    break
                if statement.target not in visited:
                    visited.add(statement.target)
                    pending.append(statement.target)
    return found


# ---------------------------------------------------------------------------
# Finding boards
# ---------------------------------------------------------------------------


def board_files(directory=BOARDS_DIR):
    """The board files in ``directory``, sorted."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        os.path.join(directory, name)
        for name in names
        if name.endswith(EXTENSION) and os.path.isfile(os.path.join(directory, name))
    ]


def _identity(path):
    """The keys a board file is recognised by, read without its profiles."""
    data, _ = _read(path)
    return {
        "usb_product": [str(v) for v in _as_list(data.get("usb_product", []))],
        "board_id": [int(v) for v in _as_list(data.get("board_id", []))],
        "serial_prefix": [str(v) for v in _as_list(data.get("serial_prefix", []))],
        "fallback": bool(data.get("fallback", False)),
    }


def find_board(
    directory=BOARDS_DIR,
    *,
    usb_product=None,
    board_id=None,
    serial_prefix=None,
    fallback=False,
):
    """Load the board file in ``directory`` recognised by the key given.

    Exactly one of ``usb_product``, ``board_id`` and ``serial_prefix`` is
    matched against the board files' keys of that name; with ``fallback``,
    the board marked ``fallback = true`` is taken when nothing matches.
    Returns ``None`` if no board matches.
    """
    files = board_files(directory)
    identities = {path: _identity(path) for path in files}
    for key, value in (
        ("usb_product", usb_product),
        ("board_id", board_id),
        ("serial_prefix", serial_prefix),
    ):
        if value is None:
            continue
        for path in files:
            if value in identities[path][key]:
                return load_board(path)
    if fallback:
        for path in files:
            if identities[path]["fallback"]:
                return load_board(path)
    return None


# ---------------------------------------------------------------------------
# Describing a board
# ---------------------------------------------------------------------------


def describe(config, functions=False):
    """A human-readable account of a board and where each part comes from.

    With ``functions``, function bodies are included.
    """
    out = []
    title = config.name or Location(config.path).layer
    out.append(f"{title} ({config.platform}) - {config.path}")
    if config.description and config.description != config.name:
        out.append(f"  {config.description}")
    keys = [
        f"{key} = {getattr(config, key)}"
        for key in ("usb_product", "board_id", "serial_prefix")
        if getattr(config, key)
    ]
    if config.fallback:
        keys.append("fallback for unrecognised FTDI boards")
    if keys:
        out.append("  recognised by: " + "; ".join(keys))
    out.append("  extends: " + (", ".join(config.extends) or "nothing"))
    if config.gpio_buses:
        out.append("  GPIO buses: " + ", ".join(config.gpio_buses))

    out.append("")
    out.append("pins:")
    for key, pin in sorted(config.pins.items(), key=lambda item: _pin_order(item[0])):
        flags = ["inverted"] if pin.inverted else []
        flags += [f"initial {pin.initial}"] if pin.initial else []
        command = pin.command or "(no command)"
        detail = f"{command} ({', '.join(flags)})" if flags else command
        out.append(f"  {key:>4}  {detail:32} {pin.origin.layer}")
    for key, location in sorted(config.removed_pins.items()):
        out.append(f"  {key:>4}  {'(not connected)':32} {location.layer}")

    out.append("")
    out.append("timings:")
    for name, timing in config.timings.items():
        out.append(f"  {name:24} {timing.value:>7} ms  {timing.origin.layer}")

    out.append("")
    out.append("functions:")
    for name, function in config.functions.items():
        out.append(f"  {name:32} {function.origin.layer}")
        if functions:
            out.extend(
                f"      {line}" for line in function.source.strip("\n").splitlines()
            )
    for name, location in config.removed_functions.items():
        out.append(f"  {name + ' (removed)':32} {location.layer}")
    return "\n".join(out)


def _pin_order(key):
    return (key[0], int(key[1:])) if key[:1].isalpha() else ("", int(key))
