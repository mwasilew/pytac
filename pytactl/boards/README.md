# Board files

One TOML file per debug board, built on the profiles in `profiles/`. The format
is described in full in [`pytactl/boardconfig.py`](../boardconfig.py); this is
the short version.

## Profiles

| Profile | What it holds |
|---------|---------------|
| `ftdi` | Pin map of the FTDI (Alpaca) boards, as the ALPACA LITE default has it. |
| `psoc` | Pin map most PSOC boards have. |
| `common` | `powerOffTheDevice`, `powerOnTheDevice`, `powerOff`, `powerOn`, `bootToEDL`, `bootToSecondaryEDL`, `bootToUEFI`, `bootToFastboot`, with a default timing for every delay. |
| `common-no-usb1` | The two power functions for boards with one USB line. |
| `common-usb3456` | The two power functions for boards that also switch `usb3456`. |
| `common-uefi-pkey-confirm` | `bootToUEFI` confirming with the power key alone. |
| `common-battery-edl` | Adds `bootToBatteryEDL` and `batteryReset`. |
| `battery-pkey` | Functions of the CSM boards. |
| `pwr-off` | Pin map and functions of the boards powered through `pwr_off`. |
| `pic32cx` | Pin map and functions of the PIC32CX boards. |

## A board file

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

Everything the file does not say comes from the profiles it extends, in order.

* **Pins** are keyed by the physical pin: `D1` on FTDI (bus and bit), the pin
  number on PSOC and PIC32CX. `false` drops an inherited pin; giving a command
  to a pin moves it there from wherever a profile had it. A pin can be
  `{ command = "battery", inverted = true, initial = 1 }`, and a pin with only
  `initial` is set when the board connects and never driven after that.
* **Timings** are milliseconds, used in scripts as `$name`.
* **Functions** are one statement per line: `pkey 1`, `delay 800`,
  `delay $edl`, or the name of another function to run. `//` or `#` starts a
  comment. `false` drops an inherited function.

How pytactl recognises the board: `usb_product` (FTDI USB product string),
`board_id` (the id a PSOC board reports) or `serial_prefix` (the part of a
PIC32CX serial number before `XX`). `fallback = true` marks the board used for
FTDI boards nothing else matches.

## Seeing what a board ends up with

```sh
pytactl showconfig ftdi-65            # pins, timings, functions, and where each comes from
pytactl showconfig ftdi-65 --scripts  # with the function bodies
pytactl listconfigs                   # every board, and whether it loads
```

## Where these files came from

They were converted from the upstream TAC config set (each file names the
config it came from) by `scripts/migrate_tac_configs.py` in the history of
this branch. Where the upstream config could not work as written - a script
driving a pin the board does not have, a pin on an FTDI bus that is not a GPIO
port, two pins given one command - the board file says what was left out and
why, in a comment at its top or beside the function.
