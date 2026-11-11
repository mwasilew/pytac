# pytactl

Python implementation of Test Automation Controller (TAC/Alpaca) for controlling Qualcomm debug boards.
It drives the boards of the original TAC (Alpaca) system, PSOC firmware included, from board files of its own.

# Installation

Install the `pytactl` command on your system from [PyPI](https://pypi.org/project/pytactl/)
with [pipx](https://pipx.pypa.io):

    pipx install pytactl

To install from a checkout of this repository instead:

    pipx install .

This puts a single `pytactl` entry point on your `PATH`. Note that the `hid`
dependency needs `libhidapi` package to be installed (required for Bughopper V2 boards).

Alternatively, for development in a virtualenv:

    virtualenv -p python3 venv
    . ./venv/bin/activate
    pip install -e .

Run `pytactl -h` to see all available options.

## System dependencies

pytactl uses `hidapi` for HID-based debug boards, including Bughopper V2.
Install the native HIDAPI library before installing or running `pytactl`.

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y libhidapi-hidraw0 libhidapi-libusb0
```

### Fedora

```bash
sudo dnf install -y hidapi
```

## USB permissions

By default, USB devices are not accessible without root. Install the udev rules
file to have access to the supported boards as an user:

    sudo cp 60-pytactl.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

Then make sure your user is in the `plugdev` group (log out and back in after):

    sudo usermod -aG plugdev $USER

# Configuration

**Bughopper boards (V1 and V2) work out of the box.** They are self-describing and need no
board file.

**All other debug boards (FTDI, PSOC, PIC32CX) are described by a board file, and the board
files ship with pytactl** in `pytactl/boards/`. Nothing needs fetching before a board can be
driven:

    pytactl shell --serial <serial>

## Board files

A board file is a short TOML file that says which *profiles* the board builds on and what is
particular to it. Profiles, in `pytactl/boards/profiles/`, hold what boards share: a platform's
default pin map, the scripts most boards run, and small overlays that change a few functions.

```toml
name = "Hawi MTP8975"
description = "MTP8975 (Hawi) FTDI IO"
usb_product = "ALPACA-LITE MTP8975"
extends = ["ftdi", "common", "common-no-usb1", "common-battery-edl"]

[pins]
C5 = "msmreset"
C6 = "rcmsel"

[timings]
fastboot = 6000

[functions]
# bootToSecondaryEDL drives sedl: it could only fail.
bootToSecondaryEDL = false
```

Everything the file does not say comes from the profiles it extends, applied in order:

* **pins** map a physical pin (`D1` on FTDI, a pin number on PSOC and PIC32CX) to the command
  that drives it. `false` drops an inherited pin, and giving a command to a pin moves it there.
  A pin can be `{ command = "battery", inverted = true, initial = 1 }`: an inverted pin is driven
  at the inverse of the commanded level, and `initial` is the level commanded when the board
  connects.
* **timings** are milliseconds, used in scripts as `$name`.
* **functions** are scripts, one statement per line: `pkey 1` drives a pin, `delay 800` or
  `delay $edl` waits, and a function's name runs that function. `//` or `#` starts a comment.
  `false` drops an inherited function. Every function is a quick method.

pytactl recognises an FTDI board by its USB product string (`usb_product`), a PSOC board by the
id it reports (`board_id`), and a PIC32CX board by the part of its serial number before `XX`
(`serial_prefix`). An FTDI board nothing matches is driven with the ALPACA LITE default, the board
file marked `fallback = true`.

Every name a script uses is checked when the board is loaded, and an error names the file, line
and function it is in. [`pytactl/boards/README.md`](pytactl/boards/README.md) lists the profiles,
and [`pytactl/boardconfig.py`](pytactl/boardconfig.py) describes the format in full.

## Seeing what a board ends up with

    pytactl showconfig ftdi-65 --log-level INFO

shows the board's pins, timings and functions after every profile is applied, each with the file
it comes from, so it is clear what is particular to the board and what is inherited; `--scripts`
adds the function bodies.

    pytactl listconfigs --log-level INFO

lists every board file with what it is recognised by, and exits with status 1 when one does not
load, naming the problem.

## Using board files of your own

`--boards-dir <dir>` points pytactl at another directory of board files, with their profiles in a
`profiles/` directory beside them. To try a single board file without looking for a connected
board, pass it with `--board-file`:

    pytactl oneshot powerOn --board-file my-board.toml

## Finding your board's serial number

The `--serial` argument takes the USB serial number, not a device path. The easiest way to
discover connected boards and their serial numbers is the `list` subcommand:

    pytactl list

It prints every recognised debug board with its type, USB vendor/product ID, and serial number
(read from udev, the same `ID_SERIAL_SHORT` value you pass to `--serial`):

    Connected debug boards:
      Bughopper V1   vid:pid=0403:6015  serial=DP05DIAN
      PSOC           vid:pid=05c6:9302  serial=0123456789

Alternatively, find it manually with `udevadm`:

    udevadm info /dev/ttyACM0 | grep ID_SERIAL_SHORT

Or using `lsusb` (replace `VID:PID` with `0403:6011` for FTDI or `05c6:9302` for PSOC):

    lsusb -v -d VID:PID | grep iSerial

# Using as a shell

Start the interactive shell with the `shell` subcommand:

    pytactl shell --serial <ID_SERIAL_SHORT>

Optional arguments:

    --boards-dir <dir>        # use board files other than the bundled ones (see Configuration)
    --board-file <file>       # drive a dummy board from one board file, touching no hardware
    --log-level DEBUG         # log verbosity (default: DEBUG)

Once started, the shell prompt accepts the functions and pin commands of your board's board file. They depend on the board — not every board defines every function (`pytactl showconfig <board>` lists them). Typical commands:

**Power control:**

    powerOn
    powerOff
    devicePowerOn
    devicePowerOff
    usbDevicePowerOn
    usbDevicePowerOff

**Boot modes:**

    bootToEDL
    bootToFastboot
    bootToUEFI
    reset

**GPIO pins** (use with `1` to assert, `0` to deassert):

    pkey 1      # press power key
    pkey 0      # release power key
    volup 1
    voldn 1

Type `help` in the shell to list all commands available for your specific board.

# Running a single command

Use the `oneshot` subcommand to run one command and exit, without entering the interactive
shell. This is handy for scripting:

    pytactl oneshot bootToEDL --serial <ID_SERIAL_SHORT>
    pytactl oneshot reset --serial <ID_SERIAL_SHORT>

GPIO pin commands take an integer value (`1` to assert, `0` to deassert):

    pytactl oneshot pkey 1 --serial <ID_SERIAL_SHORT>
    pytactl oneshot pkey 0 --serial <ID_SERIAL_SHORT>

The same commands available in the shell can be used here. An unknown command exits with an
error listing the commands supported by your board.

# Using as a service

    pytactl service --serial <ID_SERIAL_SHORT_1> [<ID_SERIAL_SHORT_2> ...]

The REST API runs on `http://localhost:5000`. Example usage with curl:

    # List connected boards
    curl http://localhost:5000/

    # List available quick methods (bootToEDL, powerOn, etc.)
    curl http://localhost:5000/<boardid>/quick

    # Power on/off
    curl -X PUT http://localhost:5000/<boardid>/quick/powerOn
    curl -X PUT http://localhost:5000/<boardid>/quick/powerOff

    # Boot to EDL
    curl -X PUT http://localhost:5000/<boardid>/quick/bootToEDL

    # Boot to fastboot
    curl -X PUT http://localhost:5000/<boardid>/quick/bootToFastboot

    # Set a named pin
    curl -X PUT "http://localhost:5000/<boardid>/command/reset?value=1"

    # Set a raw pin (e.g., bus A, pin 0)
    curl -X PUT "http://localhost:5000/<boardid>/pin/A0?value=1"

Note: REST API server runs in debug mode. Running with multiple concurrent threads may lead to unexpected behaviour.

# License

pytactl is licensed under the [BSD-3-clause License](https://spdx.org/licenses/BSD-3-Clause.html). See [LICENSE](LICENSE) for the full license text.
