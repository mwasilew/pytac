# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""One-off conversion of pytactl/tac_configs/ into pytactl/boards/.

Reads the configs in the format they had before (a *.pinout.json per board,
scripts in .alpaca files, devicelist.json) and writes the profiles and board
files of the new format (see pytactl/boardconfig.py):

* the pin maps of TAC_FTDI_13 (the ALPACA LITE default) and TAC_PSOC_44 become
  the ftdi and psoc profiles;
* each shared script (common.alpaca, its overlays, the family scripts) becomes
  a profile of its own, its timings defaulting to the value most boards using
  it have; the pwr-off and pic32cx family profiles carry their family's pin
  map too;
* each board file extends the profiles that leave it shortest and lists only
  what differs from them.

What changes on the way:

* pins with no command (placeholders left by the QTAC UI) are dropped, except
  that D6 on FTDI boards, held high, is named TC_READY_N as in TAC_FTDI_13;
* FTDI pins on a bus that is not a GPIO port, which have never driven
  anything, are dropped, and so are functions that drive a pin the board does
  not have or call one that is dropped: they could only fail;
* logComment lines go, since pytactl announces each function it runs;
* 12vpoweroff, not a usable name, becomes poweroff12v;
* devicelist.json entries that have no config of their own are dropped: the
  FTDI ones fall back to TAC_FTDI_13 anyway, and the PSOC ones were pointed
  at an FTDI pin map.

Every board file written is loaded back and checked against what it was
converted from.
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pytactl import boardconfig, tacconfig  # noqa: E402

SOURCE = os.path.join(REPO_ROOT, "pytactl", "tac_configs")
DESTINATION = boardconfig.BOARDS_DIR

PLATFORM_BASES = {"FTDI": ("ftdi", "TAC_FTDI_13"), "PSOC": ("psoc", "TAC_PSOC_44")}
SCRIPT_PROFILES = {
    "common.alpaca": "common",
    "common-no-usb1.alpaca": "common-no-usb1",
    "common-usb3456.alpaca": "common-usb3456",
    "common-uefi-pkey-confirm.alpaca": "common-uefi-pkey-confirm",
    "common-battery-edl.alpaca": "common-battery-edl",
    "battery-pkey.alpaca": "battery-pkey",
    "pwr-off.alpaca": "pwr-off",
    "pic32cx.alpaca": "pic32cx",
}
# Family profiles that carry their family's pin map, from this board.
PIN_FAMILIES = {"pwr-off": "TAC_FTDI_37", "pic32cx": "TAC_PIC32CXAuto_81"}
RENAMED_COMMANDS = {"12vpoweroff": "poweroff12v"}
# Catalog entries added on main after this branch forked (be3c09c).
EXTRA_SERIAL_PREFIXES = {"TAC_PIC32CXAuto_54": ["KARUSSELLV2"]}

PROFILE_DOCS = {
    "ftdi": """
Pin map of the FTDI (Alpaca) debug boards, as the ALPACA LITE default
(TAC_FTDI_13) has it. Buses C and D are GPIO ports; D6 (TC_READY_N) is held
high.""",
    "psoc": """
Pin map of the PSOC debug boards, as most of them have it (TAC_PSOC_44).""",
    "common": """
Functions of boards with the common battery/USB/power-key/volume-key/EDL
wiring. Every delay is a timing a board can change.""",
    "common-no-usb1": """
For boards that switch only one USB line: replaces the two power functions
of common.""",
    "common-usb3456": """
For boards that also switch usb3456 with the other USB lines: replaces the
two power functions of common.""",
    "common-uefi-pkey-confirm": """
For boards that confirm the UEFI menu with the power key alone: replaces
bootToUEFI of common.""",
    "common-battery-edl": """
For MTPs that reset themselves while volume-down is held: adds
bootToBatteryEDL and batteryReset, which enter EDL or restart the board without
cutting the battery. Both hold voldn for batteryResetHold, press the power
key for batteryResetPress and release voldn batteryResetRelease later.""",
    "battery-pkey": """
Functions of boards whose power functions switch the battery and the power
key together (the CSM boards).""",
    "pwr-off": """
Pin map and functions of the boards powered through a single pwr_off line,
with sw_dnld/ss_force_edl boot straps (RIDE MICRO 4.0, IQ-9075 and IQ-8275
EVKs). powerOffHold is how long the board stays off in every power cycle.""",
    "pic32cx": """
Pin map and functions of the PIC32CX boards. powerOffHold is how long the
battery stays disconnected in every power cycle; edl and fastboot serve both
the primary and the secondary sequence.""",
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


# ---------------------------------------------------------------------------
# Reading the old format
# ---------------------------------------------------------------------------


def clean_source(text):
    """Function body lines as the new format writes them."""
    lines = []
    for line in text.splitlines()[1:]:  # without the "def" line
        line = line.strip()
        if not line or line.startswith("logComment"):
            continue
        for old, new in RENAMED_COMMANDS.items():
            line = re.sub(rf"\b{old}\b", new, line)
        lines.append(line)
    return "\n".join(lines) + "\n" if lines else ""


def statements(source):
    function, errors = boardconfig.parse_function(
        "f", source, boardconfig.Location("-")
    )
    assert not errors, errors
    return [str(statement) for statement in function.statements]


def functions_of(script):
    """``{name: source}`` of an Alpaca script, in order."""
    return {
        name: clean_source(text)
        for name, text in tacconfig.split_functions(script)
        if name is not None
    }


def platform_of(raw):
    return (
        "PIC32CX"
        if raw["platform_type"].startswith("PIC32CX")
        else raw["platform_type"]
    )


def read_board(path, catalog):
    raw = json.load(open(path))
    stem = os.path.basename(path)[: -len(tacconfig.PINOUT_EXTENSION)]
    platform = platform_of(raw)
    gpio = sorted(b["bus"] for b in raw.get("bus", []) if b.get("bus_function") == 2)
    notes = []

    pins = {}
    dead = []
    shadowed = []
    for pin in raw["pins"]:
        key = f"{pin.get('bus', '')}{pin['pin_number']}"
        command = RENAMED_COMMANDS.get(pin["command"], pin["command"])
        initial = int(bool(pin.get("initial_value")))
        if not _IDENTIFIER.match(command or ""):
            if platform == "PSOC":
                # PSOC pins are set to their initial level, low or high, when
                # the board connects; keep doing that.
                command = None
            elif not initial:
                # An FTDI port starts low and PIC32CX pins are not set when
                # connecting: a placeholder held low does nothing.
                continue
            else:
                assert platform == "FTDI" and key == "D6", (stem, key)
                command = "TC_READY_N"
        if platform == "FTDI" and pin["bus"] not in gpio:
            dead.append(f"{key} ({command})")
            continue
        for other, (taken, inverted, level) in list(pins.items()):
            if command and taken == command:
                # pytactl has always driven the last pin given a command; the
                # other still had its initial level set when connecting.
                del pins[other]
                shadowed.append(f"{command} to {other}")
                if platform == "PSOC" or (platform == "FTDI" and level):
                    pins[other] = (None, inverted, level)
        pins[key] = (command, bool(pin.get("inverted")), initial)
    if shadowed:
        notes.append(
            "The upstream config also gives "
            + ", ".join(shadowed)
            + ". pytactl has always driven the last pin given a command, "
            "which is the one that keeps it here."
        )
    if dead:
        notes.append(
            "The upstream config also maps "
            + ", ".join(dead)
            + f", on buses that are not GPIO ports here ({', '.join(gpio)}): "
            "they have never driven anything, and are left out."
        )

    loaded = tacconfig.load_pinout(path)
    script, _ = tacconfig.normalize_function_names(loaded.get("script", ""))
    functions = functions_of(script)
    variables = {
        v["name"]: int(v["default_value"])
        for v in raw.get("variables", [])
        if v.get("name")
    }

    # Functions that drive a pin the board does not have, or call one that is
    # gone, could only ever fail: leave them out, saying why.
    commands = {command for command, _, _ in pins.values() if command}
    removed = {}
    changed = True
    while changed:
        changed = False
        for name, source in functions.items():
            if name in removed:
                continue
            reasons, calls = [], []
            for line in source.splitlines():
                words = re.split(r"//|#", line, maxsplit=1)[0].split()
                if not words:
                    continue
                if words[0] == "delay":
                    if words[1].startswith("$") and words[1][1:] not in variables:
                        reasons.append(f"waits for ${words[1][1:]}, never declared")
                elif len(words) == 2 and words[0] not in commands:
                    reasons.append(f"drives {words[0]}")
                elif len(words) == 1 and (
                    words[0] not in functions or words[0] in removed
                ):
                    calls.append(words[0])
            if reasons or calls:
                removed[name] = (sorted(set(reasons)), sorted(set(calls)))
                changed = True
    for name in removed:
        del functions[name]

    used = {
        line.split()[1][1:]
        for source in functions.values()
        for line in source.splitlines()
        if line.startswith("delay $")
    }
    timings = {name: value for name, value in variables.items() if name in used}

    entries = [e for e in catalog if e.get("configPath") == os.path.basename(path)]
    identity = {}
    # The device list's name for the board, from the entry with the config's
    # own id where several entries share the config.
    own = [e for e in entries if e.get("platform_id") == raw.get("platform_id")]
    entry = entries[0] if len(entries) == 1 else (own[0] if own else None)
    source = entry or raw
    identity["name"] = " ".join(source["name"].split())
    identity["description"] = " ".join(source["description"].split())
    descriptors = []
    for entry in entries:
        descriptor = entry.get("usb_descriptor")
        if not descriptor:
            continue
        # pytactl took the first device list entry with a descriptor.
        first = next(e for e in catalog if e.get("usb_descriptor") == descriptor)
        if first is entry:
            descriptors.append(descriptor)
        else:
            notes.append(
                f"The device list also gives this board the descriptor "
                f"{descriptor!r}, but an earlier entry, for {first['configPath']}, "
                "has it, so pytactl never found this board by it."
            )
    if platform == "FTDI" and descriptors:
        identity["usb_product"] = descriptors
    if platform == "PSOC":
        identity["board_id"] = sorted({e["platform_id"] for e in entries})
    if platform == "PIC32CX":
        identity["serial_prefix"] = descriptors + EXTRA_SERIAL_PREFIXES.get(stem, [])
    if stem == PLATFORM_BASES["FTDI"][1]:
        identity["fallback"] = True
    if not entries:
        notes.append("No device list entry points at this board.")

    shared = [
        SCRIPT_PROFILES[ref]
        for ref in json.load(open(path)).get(tacconfig.SCRIPT_REF, [])
        if ref in SCRIPT_PROFILES
    ]
    return {
        "stem": stem,
        "platform": platform,
        "gpio": gpio,
        "pins": pins,
        "functions": functions,
        "timings": timings,
        "removed": removed,
        "identity": identity,
        "shared": shared,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Writing TOML
# ---------------------------------------------------------------------------


def toml_string(value):
    return json.dumps(value, ensure_ascii=False)


def toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else toml_string(key)


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return toml_string(value)


def toml_pin(spec):
    if spec is False:
        return "false"
    command, inverted, initial = spec
    if command and not inverted and not initial:
        return toml_string(command)
    parts = [f"command = {toml_string(command)}"] if command else []
    if inverted:
        parts.append("inverted = true")
    if initial or not command:
        parts.append(f"initial = {initial}")
    return "{ " + ", ".join(parts) + " }"


def comment(text):
    out = []
    for paragraph in text.strip().split("\n\n"):
        words = paragraph.split()
        line = "#"
        for word in words:
            if len(line) + 1 + len(word) > 78:
                out.append(line)
                line = "#"
            line += " " + word
        out.append(line)
        out.append("#")
    return "\n".join(out[:-1])


def render(header, top, pins, timings, functions):
    """Text of a TOML file. ``functions`` maps a name to its source or to
    ``(False, reason)``."""
    blocks = [comment(header)] if header else []
    if top:
        blocks.append("\n".join(top_line(key, value) for key, value in top.items()))
    if pins:
        lines = ["[pins]"]
        for key in sorted(pins, key=boardconfig._pin_order):
            lines.append(f"{toml_key(key)} = {toml_pin(pins[key])}")
        blocks.append("\n".join(lines))
    if timings:
        lines = ["[timings]"]
        lines += [f"{toml_key(name)} = {value}" for name, value in timings.items()]
        blocks.append("\n".join(lines))
    if functions:
        entries = []
        for name, value in functions.items():
            if isinstance(value, tuple):
                entries.append(f"{comment(value[1])}\n{toml_key(name)} = false")
                continue
            assert '"""' not in value and "\\" not in value, name
            entries.append(f'{toml_key(name)} = """\n{value.rstrip()}\n"""')
        blocks.append("[functions]\n" + "\n\n".join(entries))
    return "\n\n".join(blocks) + "\n"


# Keys whose one-item lists are written as a plain value.
SCALAR_WHEN_SINGLE = ("usb_product", "board_id", "serial_prefix")


def top_line(key, value):
    if key in SCALAR_WHEN_SINGLE and isinstance(value, list) and len(value) == 1:
        value = value[0]
    return f"{key} = {toml_value(value)}"


# ---------------------------------------------------------------------------
# Converting
# ---------------------------------------------------------------------------


def mode(values):
    return collections.Counter(values).most_common(1)[0][0]


def write_profiles(boards, directory):
    by_stem = {board["stem"]: board for board in boards}
    profiles_dir = os.path.join(directory, boardconfig.PROFILES_DIRNAME)
    os.makedirs(profiles_dir, exist_ok=True)

    for platform, (name, stem) in PLATFORM_BASES.items():
        base = by_stem[stem]
        top = {"platform": platform}
        if platform == "FTDI":
            top["gpio_buses"] = base["gpio"]
        text = render(PROFILE_DOCS[name], top, base["pins"], {}, {})
        open(os.path.join(profiles_dir, name + boardconfig.EXTENSION), "w").write(text)

    for alpaca, name in SCRIPT_PROFILES.items():
        functions = functions_of(open(os.path.join(SOURCE, alpaca)).read())
        used = []
        for source in functions.values():
            for line in source.splitlines():
                if line.startswith("delay $") and line.split()[1][1:] not in used:
                    used.append(line.split()[1][1:])
        users = [board for board in boards if name in board["shared"]]
        timings = {
            timing: mode(b["timings"][timing] for b in users if timing in b["timings"])
            for timing in used
        }
        top, pins = {}, {}
        if name in PIN_FAMILIES:
            family = by_stem[PIN_FAMILIES[name]]
            top["platform"] = family["platform"]
            if family["gpio"]:
                top["gpio_buses"] = family["gpio"]
            pins = family["pins"]
        text = render(PROFILE_DOCS[name], top, pins, timings, functions)
        open(os.path.join(profiles_dir, name + boardconfig.EXTENSION), "w").write(text)
    return profiles_dir


def resolve(extends, profiles_dir):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
        handle.write(f"extends = {toml_value(extends)}\n")
    try:
        return boardconfig.load_board(handle.name, profiles_dir, check=False)
    finally:
        os.unlink(handle.name)


def overrides(board, base):
    """What a board file over ``base`` has to say to make ``board``."""
    pins = {}
    for key, spec in board["pins"].items():
        have = base.pins.get(key)
        if not have or (have.command, have.inverted, have.initial) != spec:
            pins[key] = spec
    moved = {spec[0] for spec in pins.values() if spec[0]}
    for key, have in base.pins.items():
        if key not in board["pins"] and have.command not in moved:
            pins[key] = False

    functions = {}
    for name in base.functions:
        if name not in board["functions"]:
            reason = board["removed"].get(name)
            text = (
                removal_reason(name, *reason)
                if reason
                else f"This board has no {name}."
            )
            functions[name] = (False, text)
    for name, source in board["functions"].items():
        have = base.functions.get(name)
        if not have or [str(s) for s in have.statements] != statements(source):
            functions[name] = source

    timings = {
        name: value
        for name, value in board["timings"].items()
        if name not in base.timings or base.timings[name].value != value
    }
    top = {}
    if base.platform != board["platform"]:
        top["platform"] = board["platform"]
    if board["platform"] == "FTDI" and base.gpio_buses != board["gpio"]:
        top["gpio_buses"] = board["gpio"]
    return top, pins, timings, functions


def removal_reason(name, reasons, calls):
    parts = list(reasons) + (
        [f"calls {', '.join(calls)}, left out too"] if calls else []
    )
    return f"{name} {'; '.join(parts)}: it could only fail."


def removal_note(removed, names):
    """One sentence on the functions left out of the upstream script."""
    reasons = sorted({reason for name in names for reason in removed[name][0]})
    drives = [r.split()[1] for r in reasons if r.startswith("drives ")]
    others = [r for r in reasons if not r.startswith("drives ")]
    why = []
    if drives:
        subject = "it drives" if len(names) == 1 else "they drive"
        why.append(f"{subject} {', '.join(drives)}, which this board has no pin for")
    why += others
    callers = [name for name in names if not removed[name][0]]
    direct = [name for name in names if removed[name][0]]
    text = (
        f"Left out of the upstream script, as they could only fail: "
        f"{', '.join(direct)} ({'; '.join(why)})"
    )
    if callers:
        text += f", and {', '.join(callers)}, which call them"
    return text + "."


def candidates(board, family_platforms):
    """The extends lists to try for a board: its platform's pin map or its
    family profile, with any of the shared scripts it used, in order; any
    family profile of its platform; or nothing at all."""
    base = PLATFORM_BASES.get(board["platform"], (None,))[0]
    shared = list(board["shared"])
    if shared and shared[0] in PIN_FAMILIES:
        base = shared.pop(0)
    options = []
    if base:
        for mask in range(2 ** len(shared), 0, -1):
            chosen = [p for i, p in enumerate(shared) if mask & (1 << i)]
            options.append([base, *chosen])
        options.append([base])
    for family, platform in family_platforms.items():
        if platform == board["platform"] and [family] not in options:
            options.append([family])
    options.append([])
    return options


def convert(boards, directory):
    profiles_dir = write_profiles(boards, directory)
    by_stem = {board["stem"]: board for board in boards}
    family_platforms = {
        family: by_stem[stem]["platform"] for family, stem in PIN_FAMILIES.items()
    }

    written = []
    for board in boards:
        best = None
        for extends in candidates(board, family_platforms):
            base = resolve(extends, profiles_dir)
            parts = overrides(board, base)
            cost = sum(len(part) for part in parts)
            if best is None or cost < best[0]:
                best = (cost, extends, parts)
        _, extends, (top, pins, timings, functions) = best
        header = f"{board['stem']} of the upstream TAC config set."
        for note in board["notes"]:
            header += "\n\n" + note
        unsaid = [name for name in board["removed"] if name not in functions]
        if unsaid:
            header += "\n\n" + removal_note(board["removed"], unsaid)
        head = dict(board["identity"])
        if extends:
            head["extends"] = extends
        head.update(top)
        name = f"{board['platform'].lower()}-{board['stem'].rsplit('_', 1)[1]}"
        path = os.path.join(directory, name + boardconfig.EXTENSION)
        open(path, "w").write(render(header, head, pins, timings, functions))
        written.append((path, board))
    return written


def check(path, board):
    """The board file loads, and is what it was converted from."""
    config = boardconfig.load_board(path)
    pins = {
        key: (pin.command, pin.inverted, pin.initial)
        for key, pin in config.pins.items()
    }
    assert pins == board["pins"], (path, pins, board["pins"])
    functions = {
        name: [str(s) for s in function.statements]
        for name, function in config.functions.items()
    }
    expected = {name: statements(source) for name, source in board["functions"].items()}
    assert functions == expected, path
    for name, value in board["timings"].items():
        assert config.timings[name].value == value, (path, name)
    if board["platform"] == "FTDI":
        assert config.gpio_buses == board["gpio"], path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--destination", default=DESTINATION)
    args = parser.parse_args()

    catalog = json.load(open(os.path.join(args.source, "devicelist.json")))["catalog"]
    boards = [
        read_board(path, catalog)
        for path in sorted(glob.glob(os.path.join(args.source, "*.pinout.json")))
    ]
    os.makedirs(args.destination, exist_ok=True)
    for path, board in convert(boards, args.destination):
        check(path, board)
    print(f"Wrote {len(boards)} board files to {args.destination}")


if __name__ == "__main__":
    main()
