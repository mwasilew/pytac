# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Conversion between TAC configuration formats.

Upstream qcom-test-automation-controller historically stored one combined
``.tcnf`` file per debug board: hardware pinout, automation script and Qt UI
layout (tabs, buttons, labels, tooltips, grid cells) in a single JSON object.
`PR #54 <https://github.com/qualcomm/qcom-test-automation-controller/pull/54>`_
splits that into two sibling files:

* ``TAC_<CHIP>_<ID>.pinout.json`` - the *shared* hardware description: pins,
  bus map, script variables and the Alpaca script. Self-describing
  (``format == "tac-pinout"``), schema
  ``https://qualcomm.github.io/tac/schemas/pinout-1.0.json``.
* ``TAC_<CHIP>_<ID>.tcnf`` - a slim UI overlay that points back at its pinout
  file through ``pinout_ref`` and carries only UI concerns.

pytactl goes one step further when it writes a pinout file: the Alpaca script
is moved out into ``.alpaca`` files, listed in the pinout's ``script_ref``.
JSON has no multi-line strings, so an inline script is one long line and any
edit to it diffs as a change to the whole script; in a file of its own it
diffs line by line. A pinout file with the script inline, as upstream writes
it, loads unchanged.

The files in ``script_ref`` are combined into one script: a function defined
in a later file replaces the one of the same name from an earlier file, and
``script_omit`` names functions to leave out (see `merge_scripts`). That lets
boards share scripts (`SHARED_SCRIPTS`), with their delays variables set in
the pinout, and keep only what is particular to them in a
``TAC_<CHIP>_<ID>.alpaca`` of their own. `factor_script` works out the split
when a config is written.

pytactl drives hardware and has no UI, so it wants the pinout half and nothing
else. This module is the Python port of the upstream split (see
``PlatformConfiguration::splitConfiguration``/``mergeConfiguration`` and the
``TACConfigSplit`` utility added by that PR), so pytactl can convert the configs
itself instead of waiting for the split to land upstream, and can consume
upstream's ``*.pinout.json`` files unchanged once it does.

Pin polarity differs too: in a config pytactl writes, a pin marked
``inverted`` is driven at the inverse of the commanded level, while upstream
only draws the pin's indicator inverted (except on FT232H). Upstream's flags
are cleared on conversion; see `clear_upstream_inversion`.

The one place the conversion is deliberately more than a mechanical split is
`drop_shadowed_pins`: ``enabled`` is a UI field and does not survive into the
pinout file, but pytactl needs it, so the decision it used to drive at load time
is baked into the converted pinout instead. See that function for details.
"""

import glob
import json
import logging
import os
import re
import subprocess

logger = logging.getLogger()

# Self-describing envelope of the shared pinout file. Consumers key off
# "format" before parsing anything else.
PINOUT_FORMAT = "tac-pinout"
PINOUT_SCHEMA_VERSION = "1.0"
PINOUT_SCHEMA_URL = "https://qualcomm.github.io/tac/schemas/pinout-1.0.json"
PINOUT_EXTENSION = ".pinout.json"

# Provenance of an imported config: which upstream repository, revision and file
# it was converted from. pytactl's own addition - upstream's pinout schema sets
# "additionalProperties": false, so an annotated config no longer validates
# against it. That is a deliberate trade for traceability of the vendored set:
# every config carries the qcom-test-automation-controller commit it came from,
# so a file stays traceable even away from the directory it was imported into.
SOURCE_FIELD = "source"

# Link from the UI overlay to its pinout file, and the per-pin key the overlay
# uses to join back to a hardware pin.
PINOUT_REF = "pinout_ref"
PIN_REF = "ref"

# Link from a pinout file to the file(s) holding its Alpaca script, written in
# place of an inline "script" (see write_pinout). pytactl's own addition, like
# SOURCE_FIELD: upstream keeps the script inline. Either one file name or a
# list of them; see merge_scripts for how a list is combined.
SCRIPT_REF = "script_ref"
SCRIPT_EXTENSION = ".alpaca"

# Functions to leave out of a script assembled from several files: a shared
# file can define functions that a particular board does not support.
SCRIPT_OMIT = "script_omit"

_FUNCTION_DEF = re.compile(r"^def\s+(\w+)")

# Scripts shared between boards (see factor_script), by family: each base
# script defines the standard functions of boards wired alike, and the
# overlays listed with it either redefine some of those functions, for a
# difference several boards of the family share, or add functions several of
# them have. The common script is the base of the largest family. pytactl
# matches upstream scripts against the copies in SHARED_SCRIPT_DIR; a config
# directory written by pytactl holds a copy of each shared script its configs
# use.
COMMON_SCRIPT = "common.alpaca"
SHARED_SCRIPTS = {
    COMMON_SCRIPT: (
        "common-no-usb1.alpaca",
        "common-uefi-pkey-confirm.alpaca",
    ),
}
SHARED_SCRIPT_NAMES = tuple(
    name for base, overlays in SHARED_SCRIPTS.items() for name in (base, *overlays)
)
SHARED_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tac_configs"
)

# A board takes a family's shared scripts only when at least this many of its
# functions come from them; otherwise its script is kept whole.
MIN_SHARED_FUNCTIONS = 4

_DELAY_VARIABLE = re.compile(r"^delay \$(\w+)$")
_VARIABLE_USE = re.compile(r"\$(\w+)")

# Fields that identify a physical pin. Written flat into the pinout file and
# inside the overlay's "ref" object. FTDI uses all three, PSOC/PIC32CX only
# pin_number.
PIN_KEY_FIELDS = ("chip_index", "bus", "pin_number")

# Per-pin hardware fields -> pinout file. Any pin field that is neither a key
# field nor a hardware field (enabled, name, help_hint, group/tab_name,
# command_group, run_priority, and any future UI field) goes to the overlay.
PIN_HARDWARE_FIELDS = (
    "command",
    "input",
    "inverted",
    "initial_value",
    "priority",
    "initialization_priority",
    "classic_action",
)

# Top-level keys that belong only to the shared pinout file.
PINOUT_ONLY_FIELDS = (
    "usb_descriptor",
    "reset_enabled",
    "script",
    "chip_count",
    "bus",
    "supportedFirmwareVer",
)

# Top-level identity keys duplicated into both files (the overlay stays
# authoritative when a config is edited in the QTAC configuration editor).
IDENTITY_FIELDS = ("name", "description", "platform_type", "platform_id")

# Keys that straddle both files and so are handled separately from the
# top-level field partition above.
_SPLIT_ARRAYS = ("pins", "variables")

# Envelope keys, which exist only in the pinout file and are dropped on merge.
# The provenance annotation counts as one: it describes the file, not the board.
_ENVELOPE_FIELDS = ("$schema", "format", "schema_version", SOURCE_FIELD)


class ConfigFormatError(Exception):
    """Raised for a config file that is not in any format we understand."""


def is_pinout(config):
    """True when ``config`` is a parsed shared pinout file."""
    return isinstance(config, dict) and config.get("format") == PINOUT_FORMAT


def is_overlay(config):
    """True when ``config`` is a parsed UI overlay referencing a pinout file."""
    return isinstance(config, dict) and PINOUT_REF in config


def pinout_filename_for(filename):
    """Return the pinout file name for a config file name.

    ``TAC_FTDI_15.tcnf`` -> ``TAC_FTDI_15.pinout.json``. A name that already
    ends in ``.pinout.json`` is returned unchanged, so the mapping is
    idempotent.
    """
    name = os.path.basename(filename)
    if name.endswith(PINOUT_EXTENSION):
        return name
    base, _, extension = name.rpartition(".")
    # rpartition returns ("", "", name) when there is no dot at all.
    return (base or extension) + PINOUT_EXTENSION


def script_filename_for(filename):
    """Return the script file name for a pinout file name.

    ``TAC_FTDI_15.pinout.json`` -> ``TAC_FTDI_15.alpaca``.
    """
    return pinout_filename_for(filename)[: -len(PINOUT_EXTENSION)] + SCRIPT_EXTENSION


def _replace_key(config, old, new, value):
    """Copy of ``config`` with key ``old`` replaced by ``new`` = ``value``,
    in the same position, so a rewritten file keeps its key order."""
    return {
        (new if key == old else key): (value if key == old else item)
        for key, item in config.items()
    }


def read_script(path):
    """Read an Alpaca script file exactly as stored, line endings included."""
    with open(path, newline="") as handle:
        return handle.read()


def split_functions(script):
    """Split an Alpaca script into ``[(name, text), ...]``, one per function.

    A function runs from its ``def`` line up to the next one; ``text`` keeps
    its lines as written, without trailing blank lines. Anything before the
    first ``def`` (comments, in practice) comes first, with ``name`` ``None``.
    """
    blocks = []
    name, lines = None, []

    def flush():
        if name is not None or any(text.strip() for text in lines):
            blocks.append((name, "\n".join(lines).rstrip()))

    for line in script.splitlines():
        match = _FUNCTION_DEF.match(line)
        if match:
            flush()
            name, lines = match.group(1), []
        lines.append(line)
    flush()
    return blocks


def merge_scripts(scripts, omit=()):
    """Combine Alpaca scripts into one, the way a ``script_ref`` list is.

    Functions are taken in order; one defined again in a later script replaces
    the earlier definition, keeping its place. Functions named in ``omit`` are
    left out. Text outside any function (a file's header comment) comes first,
    so that it does not end up after, and so seemingly part of, a function. A
    single script with nothing to omit is returned unchanged.
    """
    if len(scripts) == 1 and not omit:
        return scripts[0]
    headers = []
    functions = {}
    for script in scripts:
        for name, text in split_functions(script):
            if name is None:
                headers.append(text)
            else:
                functions[name] = text
    blocks = headers + [text for name, text in functions.items() if name not in omit]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _script_refs(config):
    refs = config[SCRIPT_REF]
    return [refs] if isinstance(refs, str) else list(refs)


def inline_script(config, directory):
    """Return ``config`` with its ``script_ref`` replaced by the script itself.

    The referenced file(s) are read from ``directory`` (only the base name is
    used, as with ``pinout_ref``) and combined with `merge_scripts`, leaving
    out the functions named in ``script_omit``. A config with no
    ``script_ref`` is returned as is. Raises `ConfigFormatError` if a script
    file cannot be read.
    """
    if SCRIPT_REF not in config:
        return config
    scripts = []
    for ref in _script_refs(config):
        script_path = os.path.join(directory, os.path.basename(ref))
        try:
            scripts.append(read_script(script_path))
        except OSError as error:
            raise ConfigFormatError(
                f"cannot read the script file {ref!r} the config refers to: {error}"
            ) from error
    script = merge_scripts(scripts, config.get(SCRIPT_OMIT, ()))
    inlined = _replace_key(config, SCRIPT_REF, "script", script)
    inlined.pop(SCRIPT_OMIT, None)
    return inlined


# Platforms whose upstream driver drives a pin marked "inverted" at the
# inverse level. Everywhere else upstream draws the pin's indicator LED
# inverted and drives the commanded level as is.
_UPSTREAM_DRIVES_INVERTED = ("FT232H",)


def clear_upstream_inversion(config):
    """Return ``config`` with the ``inverted`` flags upstream only displays.

    In a config pytactl writes, ``inverted`` means the pin is driven at the
    inverse of the commanded level. Upstream's flag means that only on FT232H;
    on the other platforms it only inverts the indicator LED in the QTAC UI,
    while the commanded level is driven as is. Clearing it there keeps what is
    driven unchanged. Configs for FT232H are returned as they are.
    """
    if config.get("platform_type") in _UPSTREAM_DRIVES_INVERTED or not any(
        pin.get("inverted") for pin in config.get("pins", [])
    ):
        return config
    cleared = dict(config)
    cleared["pins"] = [
        dict(pin, inverted=False) if pin.get("inverted") else pin
        for pin in config["pins"]
    ]
    return cleared


def load_pinout(path):
    """Load a JSON config file, with any external script read back inline.

    A config pytactl did not write - one without ``script_ref``, as upstream
    writes them - has its display-only ``inverted`` flags cleared (see
    `clear_upstream_inversion`), so that it drives the same levels in pytactl
    as in QTAC.
    """
    with open(path) as handle:
        config = json.load(handle)
    if isinstance(config, dict):
        if SCRIPT_REF not in config:
            config = clear_upstream_inversion(config)
        config = inline_script(config, os.path.dirname(path))
    return config


def _tidy(text):
    """A function block without blank lines or trailing whitespace."""
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _join(blocks):
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _statements(text):
    """What a function does, for comparing it with a common one.

    The statements of a function block, with comments, ``logComment`` lines
    and spacing left out, and a run of literal delays added up into one.
    """
    statements = []
    for line in text.splitlines()[1:]:
        statement = " ".join(line.split("//", 1)[0].split())
        if not statement or statement.startswith("logComment"):
            continue
        previous = statements[-1] if statements else ""
        if (
            statement.startswith("delay ")
            and statement[6:].isdigit()
            and previous.startswith("delay ")
            and previous[6:].isdigit()
        ):
            statements[-1] = f"delay {int(previous[6:]) + int(statement[6:])}"
            continue
        statements.append(statement)
    return statements


def parse_common_script(script):
    """Parse the common script into ``{function: [slot, ...]}``.

    A slot is either a statement the board's function must have, or, for a
    ``delay $name`` line, a ``(name, optional)`` pair the board's function may
    fill with any delay: ``optional`` slots (marked ``// optional``) may also
    be absent, which is a delay of 0. A variable may appear in several slots,
    and then stands for the same delay in each of them.
    """
    functions = {}
    for name, text in split_functions(script):
        if name is None:
            continue
        slots = []
        for line in text.splitlines()[1:]:
            code, _, comment = line.partition("//")
            statement = " ".join(code.split())
            if not statement or statement.startswith("logComment"):
                continue
            match = _DELAY_VARIABLE.match(statement)
            if not match:
                slots.append(statement)
                continue
            slots.append((match.group(1), "optional" in comment))
        functions[name] = slots
    return functions


# Binding for a variable the board's script already uses by name.
_KEEP = object()


def _match_function(slots, statements, fixed, bound=None):
    """Match a board's function against a common one.

    Returns the bindings ``{variable: value}`` making the common function do
    what the board's does, ``bound`` included, or ``None`` if it cannot.
    ``value`` is the delay as a string, or `_KEEP` where the board already
    delays by that variable. ``fixed`` maps the variables the board's script
    uses to their declared values: those cannot take another value without
    changing what the script does. ``bound`` holds the bindings made for other
    functions, which a variable used here too has to agree with.
    """

    def walk(i, j, bindings):
        if i == len(slots):
            return bindings if j == len(statements) else None
        slot = slots[i]
        if isinstance(slot, str):
            if j < len(statements) and statements[j] == slot:
                return walk(i + 1, j + 1, bindings)
            return None
        variable, optional = slot

        def bind(value, next_j):
            if variable in bindings and bindings[variable] != value:
                return None
            return walk(i + 1, next_j, {**bindings, variable: value})

        if j < len(statements) and statements[j].startswith("delay "):
            argument = statements[j][6:]
            value = None
            if argument == "$" + variable:
                value = _KEEP
            elif argument.isdigit() and fixed.get(variable, argument) == argument:
                value = argument
            if value is not None:
                result = bind(value, j + 1)
                if result is not None:
                    return result
        if optional and fixed.get(variable, "0") == "0":
            return bind("0", j)
        return None

    return walk(0, 0, dict(bound or {}))


def read_shared_scripts():
    """``{name: text}`` of every shared script (`SHARED_SCRIPT_NAMES`)."""
    shared = {}
    for name in SHARED_SCRIPT_NAMES:
        with open(os.path.join(SHARED_SCRIPT_DIR, name)) as handle:
            shared[name] = handle.read()
    return shared


def script_behaviour(config):
    """What a loaded config's script does: ``{function: [statement, ...]}``.

    Variables are replaced by their values in the config, and comments,
    ``logComment`` lines, spacing and ``delay 0`` are left out, with a run of
    literal delays added up into one: two configs whose functions drive the
    same pins with the same timings compare equal, however the script is laid
    out or split between files.
    """
    values = {
        variable["name"]: str(variable.get("default_value"))
        for variable in config.get("variables", [])
        if variable.get("name")
    }
    script = re.sub(
        r"\$(\w+)(?!\w)",
        lambda match: values.get(match.group(1), match.group(0)),
        config.get("script", ""),
    )
    return {
        name: [s for s in _statements(text) if s != "delay 0"]
        for name, text in split_functions(script)
        if name is not None
    }


def _match_family(base_name, overlay_names, shared_scripts, match):
    """Match a board against one family of shared scripts.

    Each function of the base script the board does the same, given suitable
    delays, is matched; then each overlay whose functions the board all has,
    none already matched, and each does as the overlay does. Returns
    ``(used, matched, bindings, base_functions)``: the shared scripts used, the
    functions they provide, the variable bindings and the base's functions.
    """
    base = parse_common_script(shared_scripts[base_name])
    matched = set()
    bindings = {}
    for function, slots in base.items():
        binding = match(function, slots, bindings)
        if binding is not None:
            matched.add(function)
            bindings = binding

    used = [base_name]
    for overlay_name in overlay_names:
        overlay = parse_common_script(shared_scripts[overlay_name])
        if matched & set(overlay):
            continue
        trial = bindings
        for function, slots in overlay.items():
            trial = match(function, slots, trial)
            if trial is None:
                break
        if trial is None:
            continue
        used.append(overlay_name)
        matched |= set(overlay)
        bindings = trial
    return used, matched, bindings, list(base)


def factor_script(pinout, script_name, shared_scripts, families=None):
    """Split a config's script into shared scripts and what is left.

    ``shared_scripts`` is ``{name: text}`` of the shared scripts, and
    ``families`` maps each base script to its overlays (`SHARED_SCRIPTS` by
    default). The board is matched against each family (see `_match_family`):
    a function of the board's script that does what a shared function of the
    same name does, given suitable delays, is taken from the shared script.
    The family providing the most functions wins, the first listed on a tie.
    The delays this needs are declared in the pinout's ``variables`` (a
    variable the script did not use is given the value needed). The board's
    other functions, and shared ones it defines differently, stay in the
    board's own script; base functions it lacks are listed in ``script_omit``.
    A board getting fewer than `MIN_SHARED_FUNCTIONS` functions from any
    family keeps its script whole.

    Either way, functions are tidied (no blank lines or trailing whitespace)
    and line endings are LF. Returns ``(pinout, board_script)``: ``pinout``
    with ``script_ref`` (and ``script_omit``) in place of ``script``, and the
    text for ``script_name``, or ``None`` if the board needs no file of its own.
    """
    families = SHARED_SCRIPTS if families is None else families
    script = pinout["script"].replace("\r\n", "\n")
    # The shared scripts' header comments come along when a config written by
    # pytactl is loaded; they are the shared scripts', not the board's.
    for shared in shared_scripts.values():
        for name, text in split_functions(shared):
            if name is None:
                script = script.replace(text, "")
    blocks = [(name, _tidy(text)) for name, text in split_functions(script)]
    board_functions = {name: text for name, text in blocks if name is not None}
    declared = {
        variable["name"]: str(variable.get("default_value"))
        for variable in pinout.get("variables", [])
        if variable.get("name")
    }
    fixed = {name: declared.get(name) for name in _VARIABLE_USE.findall(script)}

    def match(function, slots, bound):
        if function not in board_functions:
            return None
        statements = _statements(board_functions[function])
        return _match_function(slots, statements, fixed, bound)

    best = None
    for base_name, overlay_names in families.items():
        family = _match_family(base_name, overlay_names, shared_scripts, match)
        if best is None or len(family[1]) > len(best[1]):
            best = family
    used, matched, bindings, base_functions = best or ([], set(), {}, [])

    if len(matched) < MIN_SHARED_FUNCTIONS:
        board_script = _join([text for _, text in blocks])
        refs = [script_name] if board_script else []
        return _replace_key(pinout, "script", SCRIPT_REF, refs), board_script or None

    variables = [dict(variable) for variable in pinout.get("variables", [])]
    by_name = {variable.get("name"): variable for variable in variables}
    for variable, value in bindings.items():
        if value is _KEEP:
            continue
        if variable in by_name:
            by_name[variable]["default_value"] = value
        else:
            variables.append({"name": variable, "default_value": value})

    board_script = _join([text for name, text in blocks if name not in matched])
    refs = used + ([script_name] if board_script else [])
    omit = [name for name in base_functions if name not in board_functions]
    factored = {}
    for key, value in pinout.items():
        if key == "script":
            factored[SCRIPT_REF] = refs
            if omit:
                factored[SCRIPT_OMIT] = omit
        elif key == "variables":
            factored[key] = variables
        else:
            factored[key] = value
    factored.setdefault("variables", variables)
    return factored, board_script or None


def _write_if_changed(path, text):
    try:
        with open(path) as handle:
            if handle.read() == text:
                return
    except OSError:
        pass
    with open(path, "w") as handle:
        handle.write(text)


def write_pinout(directory, name, pinout):
    """Write ``pinout`` as ``directory/name``, its script in ``.alpaca`` files.

    The script is split with `factor_script`: functions it shares with the
    shared scripts are taken from those, each one used also written into
    ``directory``, and the rest goes to `script_filename_for` ``name``. A
    stale script file of the board's own is removed. A pinout without a
    ``script`` key is written as is.
    """
    if "script" in pinout:
        script_name = script_filename_for(name)
        shared = read_shared_scripts()
        pinout, board_script = factor_script(pinout, script_name, shared)
        script_path = os.path.join(directory, script_name)
        if board_script is not None:
            _write_if_changed(script_path, board_script)
        elif os.path.exists(script_path):
            os.remove(script_path)
        for shared_name, text in shared.items():
            if shared_name in pinout[SCRIPT_REF]:
                _write_if_changed(os.path.join(directory, shared_name), text)
    _write_json(os.path.join(directory, name), pinout)


def copy_config(path, directory, name=None):
    """Copy the pinout file ``path`` into ``directory``, script file and all.

    ``name`` renames the copy (its script file follows the new name).
    """
    write_pinout(directory, name or os.path.basename(path), load_pinout(path))


def pin_key(key_holder):
    """Stable identity of a physical pin, built from the key fields present.

    ``key_holder`` is either a pin from a pinout file (key fields written flat)
    or the ``ref`` object of an overlay pin. Values are compared as strings
    because ``pin_number`` is a string for FTDI/PSOC and an integer for PIC32CX.
    """
    return tuple(
        str(key_holder[field]) for field in PIN_KEY_FIELDS if field in key_holder
    )


def normalize_script_indentation(script):
    """Indent every statement in an Alpaca script with a single tab.

    TAC config scripts are written with tabs: a ``def`` at column 0 and its body
    one tab in. A handful of upstream configs indent the odd line with spaces
    instead (TAC_FTDI_51, TAC_FTDI_52 and TAC_FTDI_77 each have two), which the
    parser rejects - see ``Board.parse_script``, which requires tabs rather than
    quietly accepting either. Conversion is the import boundary, so that is
    where the indentation is made canonical.

    Blank lines are left alone; they are not statements. Returns
    ``(script, lines_changed)``.
    """
    if not script:
        return script, 0

    changed = 0

    def to_tab(match):
        nonlocal changed
        if match.group(0) != "\t":
            changed += 1
        return "\t"

    # Leading whitespace of a line with content, without touching the line
    # ending: configs use both LF and CRLF and the parser reads both.
    normalized = re.sub(r"^[ \t]+(?=\S)", to_tab, script, flags=re.MULTILINE)
    return normalized, changed


# Upstream spells some quick methods two ways. Conversion settles on one, so
# that the same operation has the same name on every board.
FUNCTION_RENAMES = {
    "bootToFastBoot": "bootToFastboot",
    "bootToUEFIMenu": "bootToUEFI",
}


def normalize_function_names(script):
    """Rename functions spelled differently from the rest of the configs.

    Applies `FUNCTION_RENAMES` to definitions and calls alike. A function is
    not renamed when the script already defines the target name, since the
    two would collide; that is logged instead. Returns ``(script, renamed)``,
    ``renamed`` listing the ``(old, new)`` pairs applied.
    """
    defined = {name for name, _ in split_functions(script) if name}
    renamed = []
    for old, new in FUNCTION_RENAMES.items():
        if old not in defined:
            continue
        if new in defined:
            logger.warning(
                "Script defines both %s and %s; leaving %s as it is", old, new, old
            )
            continue
        script = re.sub(rf"\b{old}\b", new, script)
        renamed.append((old, new))
    return script, renamed


def git_source_info(directory):
    """Describe ``directory`` as a path inside a git checkout, or ``None``.

    Returns ``{"repository": ..., "commit": ..., "path": ...}`` where ``path``
    is ``directory`` relative to the top of the work tree. Used to annotate
    converted configs with the upstream revision they came from, so that
    re-running the conversion against a newer checkout re-stamps them.

    ``None`` when ``directory`` is not in a git checkout, when git is not
    installed, or when the checkout has no commit yet - provenance is recorded
    only when it is actually known.
    """

    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", directory, *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.debug("git %s failed in %s: %s", args[0], directory, error)
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    toplevel = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD")
    if not toplevel or not commit:
        return None

    source = {}
    remote = git("remote", "get-url", "origin")
    if remote:
        source["repository"] = remote
    source["commit"] = commit
    path = os.path.relpath(os.path.abspath(directory), toplevel)
    if path != ".":
        source["path"] = path.replace(os.sep, "/")
    return source


def annotate_source(pinout, source, filename=None):
    """Return ``pinout`` with a `SOURCE_FIELD` recording where it came from.

    ``source`` is the repository-level provenance (see `git_source_info`);
    ``filename`` names the upstream config file this pinout was converted from.
    The annotation is placed straight after the envelope so it is visible at the
    top of the file, and replaces any annotation already there.
    """
    annotated = {}
    for key in ("$schema", "format", "schema_version"):
        if key in pinout:
            annotated[key] = pinout[key]

    entry = dict(source)
    if filename:
        entry["file"] = filename
    annotated[SOURCE_FIELD] = entry

    for key, value in pinout.items():
        if key not in annotated:
            annotated[key] = value
    return annotated


def split_configuration(combined, pinout_ref=None):
    """Split a combined config into ``(pinout, overlay)``.

    Faithful port of upstream ``PlatformConfiguration::splitConfiguration``:
    identity fields are duplicated, hardware fields go to the pinout, and
    everything else - including any field this port has never heard of - lands
    in the UI overlay, so nothing is silently dropped.

    ``pinout_ref``, when given, is written into the overlay as the link back to
    its pinout file (upstream sets this while saving, not while splitting).
    """
    pinout = {
        "$schema": PINOUT_SCHEMA_URL,
        "format": PINOUT_FORMAT,
        "schema_version": PINOUT_SCHEMA_VERSION,
    }
    overlay = {}

    for key, value in combined.items():
        if key in _SPLIT_ARRAYS:
            continue
        if key in IDENTITY_FIELDS:
            pinout[key] = value
            overlay[key] = value
        elif key in PINOUT_ONLY_FIELDS:
            pinout[key] = value
        else:
            overlay[key] = value

    pinout_pins = []
    overlay_pins = []
    for pin in combined.get("pins", []):
        hardware_pin = {}
        ui_pin = {}
        ref = {}
        for key, value in pin.items():
            if key in PIN_KEY_FIELDS:
                hardware_pin[key] = value
                ref[key] = value
            elif key in PIN_HARDWARE_FIELDS:
                hardware_pin[key] = value
            else:
                ui_pin[key] = value
        ui_pin[PIN_REF] = ref
        pinout_pins.append(hardware_pin)
        overlay_pins.append(ui_pin)

    pinout["pins"] = pinout_pins
    overlay["pins"] = overlay_pins

    # Variable defaults travel with the hardware (the script substitutes them);
    # label/tooltip/type/layout stay in the overlay. Joined back on "name".
    pinout_variables = []
    overlay_variables = []
    for variable in combined.get("variables", []):
        hardware_variable = {}
        ui_variable = {}
        for key, value in variable.items():
            if key == "name":
                hardware_variable[key] = value
                ui_variable[key] = value
            elif key == "default_value":
                hardware_variable[key] = value
            else:
                ui_variable[key] = value
        pinout_variables.append(hardware_variable)
        overlay_variables.append(ui_variable)

    pinout["variables"] = pinout_variables
    overlay["variables"] = overlay_variables

    if pinout_ref:
        overlay[PINOUT_REF] = pinout_ref

    return pinout, overlay


def merge_configuration(pinout, overlay):
    """Rebuild a combined config from a pinout file and its UI overlay.

    Inverse of `split_configuration`, ported from upstream
    ``PlatformConfiguration::mergeConfiguration``. The overlay wins on the
    identity fields both files carry.
    """
    combined = {
        key: value
        for key, value in overlay.items()
        if key not in _SPLIT_ARRAYS and key != PINOUT_REF
    }

    for key, value in pinout.items():
        if key in _SPLIT_ARRAYS or key in _ENVELOPE_FIELDS:
            continue
        combined.setdefault(key, value)

    ui_pins = {pin_key(pin.get(PIN_REF, {})): pin for pin in overlay.get("pins", [])}
    combined_pins = []
    for hardware_pin in pinout.get("pins", []):
        pin = dict(hardware_pin)
        ui_pin = ui_pins.get(pin_key(hardware_pin))
        if ui_pin:
            pin.update({k: v for k, v in ui_pin.items() if k != PIN_REF})
        combined_pins.append(pin)
    combined["pins"] = combined_pins

    ui_variables = {
        variable.get("name"): variable for variable in overlay.get("variables", [])
    }
    combined_variables = []
    for hardware_variable in pinout.get("variables", []):
        variable = dict(hardware_variable)
        ui_variable = ui_variables.get(hardware_variable.get("name"))
        if ui_variable:
            variable.update(ui_variable)
        combined_variables.append(variable)
    combined["variables"] = combined_variables

    return combined


def shadowed_pin_keys(pins):
    """Keys of pins that a same-named enabled pin takes precedence over.

    ``enabled`` is a UI field: it says whether the QTAC app shows a control for
    a pin. It does not survive into the shared pinout file, but pytactl needs
    the distinction, because several configs wire the same command name to two
    physical pins and disable one of them (e.g. ``pshold`` in TAC_FTDI_63,
    ``msmreset`` in TAC_PSOC_66). pytactl binds a command to a pin by
    ``setattr``, so without the flag the *last* pin listed would win, silently
    driving the wrong physical line.

    A disabled pin is only shadowed when its command actually collides with an
    enabled pin; a disabled pin with a command of its own is kept, matching the
    behaviour pytactl applied at load time before the split.
    """
    enabled_commands = {pin.get("command") for pin in pins if pin.get("enabled", True)}
    return {
        pin_key(pin)
        for pin in pins
        if not pin.get("enabled", True) and pin.get("command") in enabled_commands
    }


def convert_configuration(combined):
    """Convert one combined config into the shared pinout config pytactl loads.

    This is `split_configuration` plus the `shadowed_pin_keys` resolution; the
    UI overlay is discarded. Returns the pinout dict.
    """
    pins = combined.get("pins", [])
    shadowed = shadowed_pin_keys(pins)
    pinout, _ = split_configuration(combined)
    if shadowed:
        logger.debug(
            "Dropping %d pin(s) shadowed by an enabled pin with the same command",
            len(shadowed),
        )
        pinout["pins"] = [pin for pin in pinout["pins"] if pin_key(pin) not in shadowed]
    return pinout


def convert_file(path):
    """Load any TAC config file and return it as a pinout config.

    Accepts all three shapes pytactl can be pointed at:

    * a shared ``*.pinout.json`` - returned as-is;
    * a split ``*.tcnf`` UI overlay - its ``pinout_ref`` sibling is loaded from
      the same directory and merged, so the overlay's pin enablement is applied;
    * a legacy combined ``*.tcnf`` - converted in memory.

    A script kept in a separate file (``script_ref``) is read back in, so the
    returned config always carries it inline as ``script``.

    Raises `ConfigFormatError` if the file is none of these.
    """
    config = load_pinout(path)

    if is_pinout(config):
        return config

    if is_overlay(config):
        pinout_path = os.path.join(
            os.path.dirname(path), os.path.basename(config[PINOUT_REF])
        )
        pinout = load_pinout(pinout_path)
        if not is_pinout(pinout):
            raise ConfigFormatError(
                f"{pinout_path} is referenced as a pinout file but its "
                f"'format' is not {PINOUT_FORMAT!r}"
            )
        converted = convert_configuration(merge_configuration(pinout, config))
        # The merge drops the annotation along with the rest of the envelope;
        # the file it describes is the same one, so put it back.
        if SOURCE_FIELD in pinout:
            converted = annotate_source(converted, pinout[SOURCE_FIELD])
        return converted

    if "pins" not in config:
        raise ConfigFormatError(f"{path} is not a TAC configuration file")

    return convert_configuration(config)


def rewrite_device_list(device_list, default_filename):
    """Point every catalog entry at the converted pinout file.

    Upstream ``devicelist.json`` entries carry repository-relative paths
    (``../../configurations/TAC_FTDI_15.tcnf``), and leave ``configPath`` empty
    for boards that rely on the generated default FTDI config. Every config is
    installed flat into one directory, so entries are reduced to the bare
    pinout file name, and empty ones point at ``default_filename``.

    Mutates ``device_list`` in place and returns the number of entries changed.
    """
    patched = 0
    for entry in device_list.get("catalog", []):
        config_path = entry.get("configPath")
        new_path = pinout_filename_for(config_path) if config_path else default_filename
        if new_path != config_path:
            entry["configPath"] = new_path
            patched += 1
    return patched


def _write_json(path, obj):
    with open(path, "w") as handle:
        json.dump(obj, handle, indent=4)
        handle.write("\n")


def convert_directory(
    source,
    destination=None,
    default_filename=None,
    write_overlay=False,
    dry_run=False,
    source_info=None,
    annotate=True,
):
    """Convert every TAC config in ``source`` into ``destination``.

    Script indentation is normalised to tabs on the way through, so that what
    is written out is what `Board.parse_script` requires, and functions are
    renamed to one spelling (see `normalize_function_names`).

    Each ``*.tcnf`` (legacy or already split) becomes a ``*.pinout.json`` with
    its script in ``*.alpaca`` files (see `write_pinout`).
    ``*.pinout.json`` files that have no ``*.tcnf`` beside them are converted
    too, so a directory of upstream pinout files can be installed directly.
    A ``devicelist.json`` in ``source`` is copied over with its ``configPath``
    entries rewritten to the converted file names.

    With ``write_overlay`` the slim UI overlay is written alongside each pinout
    file, giving the complete two-file layout rather than just the half pytactl
    needs. With ``dry_run`` nothing is written.

    Each converted config is annotated with the upstream repository, revision
    and file it came from (see `annotate_source`). ``source_info`` supplies that
    provenance and always wins; when it is omitted and ``source`` is a git
    checkout, provenance is read from the checkout and applied only to configs
    that do not already carry an annotation - so re-converting an imported set
    in place does not restamp it with the wrong repository. Pass
    ``annotate=False`` to write configs that carry no annotation and so validate
    against upstream's pinout schema unchanged; a config that is already
    annotated then keeps the annotation it has.

    Returns a ``{"converted": [...], "failed": [(name, error), ...],
    "device_list": bool}`` summary.
    """
    destination = destination or source
    default_filename = default_filename or ("default" + PINOUT_EXTENSION)
    # Provenance the caller states is authoritative; provenance merely observed
    # from the source directory is not, and defers to any already recorded.
    stated_source = source_info is not None
    if annotate and source_info is None:
        source_info = git_source_info(source)
        if source_info is None:
            logger.debug(
                "%s is not a git checkout; converting without provenance", source
            )
    if not dry_run:
        os.makedirs(destination, exist_ok=True)

    converted = []
    failed = []

    sources = sorted(glob.glob(os.path.join(source, "*.tcnf")))
    # Pinout files whose overlay is missing (or that were never split at all)
    # are not reachable through a .tcnf, so pick them up directly.
    covered = {pinout_filename_for(path) for path in sources}
    sources += [
        path
        for path in sorted(glob.glob(os.path.join(source, "*" + PINOUT_EXTENSION)))
        if os.path.basename(path) not in covered
    ]

    for path in sources:
        name = os.path.basename(path)
        pinout_name = pinout_filename_for(name)
        try:
            config = load_pinout(path)
            if is_pinout(config):
                pinout, overlay = config, None
            elif is_overlay(config):
                pinout = convert_file(path)
                overlay = None
            else:
                pinout, overlay = split_configuration(config, pinout_ref=pinout_name)
                shadowed = shadowed_pin_keys(config.get("pins", []))
                if shadowed:
                    pinout["pins"] = [
                        pin for pin in pinout["pins"] if pin_key(pin) not in shadowed
                    ]
                    overlay["pins"] = [
                        pin
                        for pin in overlay["pins"]
                        if pin_key(pin.get(PIN_REF, {})) not in shadowed
                    ]
        except (OSError, ValueError, ConfigFormatError) as error:
            logger.error("Failed to convert %s: %s", name, error)
            failed.append((name, str(error)))
            continue

        script, retabbed = normalize_script_indentation(pinout.get("script", ""))
        if retabbed:
            logger.info("%s: re-indented %d script line(s) with tabs", name, retabbed)
        script, renamed = normalize_function_names(script)
        for old, new in renamed:
            logger.info("%s: renamed %s to %s", name, old, new)
        if retabbed or renamed:
            pinout["script"] = script

        if annotate and source_info and (stated_source or SOURCE_FIELD not in pinout):
            pinout = annotate_source(pinout, source_info, name)

        if not dry_run:
            write_pinout(destination, pinout_name, pinout)
            if write_overlay and overlay is not None:
                _write_json(os.path.join(destination, name), overlay)
        logger.debug("Converted %s -> %s", name, pinout_name)
        converted.append(pinout_name)

    device_list_path = os.path.join(source, "devicelist.json")
    has_device_list = os.path.isfile(device_list_path)
    if has_device_list:
        with open(device_list_path) as handle:
            device_list = json.load(handle)
        patched = rewrite_device_list(device_list, default_filename)
        logger.debug("Rewrote %d configPath entries in devicelist.json", patched)
        if not dry_run:
            _write_json(os.path.join(destination, "devicelist.json"), device_list)

    return {
        "converted": converted,
        "failed": failed,
        "device_list": has_device_list,
    }


def list_directory(tac_config_path):
    """Describe the config set in ``tac_config_path`` as pytactl would load it.

    Every ``devicelist.json`` catalog entry is resolved to its config file the
    way the board classes do it (by bare file name inside the directory) and
    that file is loaded with `convert_file`. Config scripts are not parsed: a
    script error surfaces when a board is created from the config, and is not
    a sign the config set is missing.

    Returns ``{"entries": [...], "unreferenced": [...]}``. Each entry is the
    catalog entry's ``name``, ``platform_id``, ``usb_descriptor`` and
    ``configPath``, plus ``status`` (``"ok"``, ``"missing"`` or ``"invalid"``)
    and, unless the config loaded, ``error``. ``unreferenced`` names the
    ``*.pinout.json`` files no catalog entry points at.

    Raises `OSError` or `ValueError` if ``devicelist.json`` cannot be read.
    """
    with open(os.path.join(tac_config_path, "devicelist.json")) as handle:
        device_list = json.load(handle)

    entries = []
    referenced = set()
    for entry in device_list.get("catalog", []):
        config_name = os.path.basename(entry.get("configPath") or "")
        info = {
            "name": entry.get("name", ""),
            "platform_id": entry.get("platform_id"),
            "usb_descriptor": entry.get("usb_descriptor", ""),
            "configPath": config_name,
            "status": "ok",
        }
        config_path = os.path.join(tac_config_path, config_name)
        if not config_name or not os.path.isfile(config_path):
            info["status"] = "missing"
            info["error"] = "no configPath" if not config_name else "file not found"
        else:
            referenced.add(config_name)
            try:
                convert_file(config_path)
            except (OSError, ValueError, ConfigFormatError) as error:
                info["status"] = "invalid"
                info["error"] = str(error)
        entries.append(info)

    unreferenced = sorted(
        name
        for name in os.listdir(tac_config_path)
        if name.endswith(PINOUT_EXTENSION) and name not in referenced
    )
    return {"entries": entries, "unreferenced": unreferenced}
