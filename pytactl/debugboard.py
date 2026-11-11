# Copyright (c)i 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import re
import sys
from time import sleep
from types import MethodType

import serial
import serial.tools.list_ports
import usb
from pexpect import fdpexpect
from pyftdi.gpio import GpioAsyncController

from . import boardconfig

logger = logging.getLogger()

try:
    import hid
except ImportError as e:
    # ignore importing hid. It's only required for bughopper v2.
    logger.error(e)
    pass

# wait time for other GPIOs to be truly set before touching the reset GPIO
PRE_RESET_DELAY = 0.1
# How long the Bughopper holds VOL_DOWN asserted to wake the target from
# suspend. VOL_DOWN is a gpio-keys wakeup source on the UNO Q; JCTL carries no
# power key, so this is the closest stand-in for a power button press.
WAKE_PULSE_DELAY = 0.3

BITMODE_CBUS = 0x20

# USB ctrl transfer request for FT230X CBUS
SIO_SET_BITMODE_REQUEST = 0x0B


class TACException(Exception):
    status_code = 420
    detail = "Unable to perform requested operation"


class ConfigScriptError(TACException):
    """A board file pytactl cannot turn into callable board commands."""

    detail = "Unable to load the board file"


class Board(dict):
    ID_VENDOR_FTDI = 0x0403
    ID_PRODUCT_FTDI = 0x6011
    ID_VENDOR_QCOM = 0x05C6
    ID_PRODUCT_QCOM = 0x9302
    ID_PRODUCT_BUGHOPPER_V1 = 0x6015
    ID_VENDOR_BUGHOPPER_V2 = 0x2341
    ID_PRODUCT_BUGHOPPER_V2 = 0xB001
    ID_VENDOR_PIC32CX = 0x04D8
    ID_PRODUCT_PIC32CX = 0x000A

    @classmethod
    def create_from_config(cls, board_file):
        return DummyBoard(board_file)

    @classmethod
    def known_boards(cls):
        # (idVendor, idProduct) -> human-readable board type. A pair may map
        # to more than one model (e.g. a pic32cx also enumerates as FTDI
        # 0403:6011); the label is the USB-level category, not the exact
        # model, which can only be resolved by querying the board.
        return {
            (cls.ID_VENDOR_FTDI, cls.ID_PRODUCT_BUGHOPPER_V1): "Bughopper V1",
            (cls.ID_VENDOR_FTDI, cls.ID_PRODUCT_FTDI): "FTDI",
            (cls.ID_VENDOR_QCOM, cls.ID_PRODUCT_QCOM): "PSOC",
            (cls.ID_VENDOR_BUGHOPPER_V2, cls.ID_PRODUCT_BUGHOPPER_V2): "Bughopper V2",
            (cls.ID_VENDOR_PIC32CX, cls.ID_PRODUCT_PIC32CX): "PIC32CX",
        }

    @staticmethod
    def _serial_ports():
        """Yield connected serial ports in a cross-platform way.

        Uses ``pyserial``'s ``list_ports`` instead of ``pyudev`` so the tool
        works on macOS and Windows as well as Linux (``pyudev`` depends on
        ``libudev``, which only exists on Linux).
        """
        yield from serial.tools.list_ports.comports()

    @classmethod
    def list_boards(cls):
        """Enumerate connected debug boards via their serial (tty) devices.

        Boards are discovered by walking the connected serial ports (the same
        place a board's serial port is found when driving it) and reading the
        ``serial_number`` / ``vid`` / ``pid`` reported by ``pyserial``. Using
        the serial port rather than the parent USB device ensures the serial
        number is available for all boards, including pic32cx.

        Returns a list of dicts with the board ``type``, USB ``serial``
        number and ``vid``/``pid``, de-duplicated by serial (a board may
        expose more than one serial interface).
        """
        known = cls.known_boards()
        boards = []
        seen = set()
        for port in cls._serial_ports():
            serial_short = port.serial_number
            if not serial_short or serial_short in seen:
                continue
            if port.vid is None or port.pid is None:
                continue
            vid = port.vid
            pid = port.pid
            board_type = known.get((vid, pid))
            if board_type is None:
                continue
            seen.add(serial_short)
            boards.append(
                {
                    "type": board_type,
                    "serial": serial_short,
                    "vid": vid,
                    "pid": pid,
                }
            )

        # The PIC32CX board also exposes an onboard FTDI FT4232H as a
        # UART/SPI bridge, which enumerates with the same VID/PID as a
        # standalone FTDI Alpaca board. It is not a separate debug board, so
        # drop it whenever a PIC32CX board is also present.
        if any(board["type"] == "PIC32CX" for board in boards):
            boards = [board for board in boards if board["type"] != "FTDI"]

        return boards

    @classmethod
    def create_board(cls, serial, boards_dir=boardconfig.BOARDS_DIR):
        device = usb.core.find(serial_number=serial)
        if device:
            if (
                device.idVendor == Board.ID_VENDOR_FTDI
                and device.idProduct == Board.ID_PRODUCT_BUGHOPPER_V1
            ):
                logger.debug("Found Bughopper V1")
                return BughopperV1Board(device)
            if (
                device.idProduct == Board.ID_PRODUCT_FTDI
                and device.idVendor == Board.ID_VENDOR_FTDI
            ):
                logger.debug("Found FTDI Board")
                return FtdiBoard(device, boards_dir)
            if (
                device.idProduct == Board.ID_PRODUCT_QCOM
                and device.idVendor == Board.ID_VENDOR_QCOM
            ):
                logger.debug("Found Psoc Board")
                return PsocBoard(device, boards_dir)
            if (
                device.idVendor == Board.ID_VENDOR_BUGHOPPER_V2
                and device.idProduct == Board.ID_PRODUCT_BUGHOPPER_V2
            ):
                logger.debug("Found Bughopper V2")
                if "hid" not in sys.modules:
                    logger.error(
                        "hid module not loaded. Check if the required packages are installed"
                    )
                    sys.exit(1)

                return BughopperV2Board(device)

        # The PIC32CX board is driven purely over its CDC serial port and is not
        # visible to libusb (pyusb can't read its descriptors), so it is detected
        # via udev instead of usb.core.find.
        if Pic32cxBoard.detect(serial):
            logger.debug("Found PIC32CX Board")
            return Pic32cxBoard(serial, boards_dir)

    def __init__(self):
        self.ports = {}
        self.pins = {}
        self.commands = {}
        self.quick_methods = {}
        self.usb_device = None
        dict.__init__(
            self, ports=self.ports, pins=self.pins, quick_methods=self.quick_methods
        )

    def logComment(self, comment):
        print(comment)

    def delay(self, length):
        d = float(length / 1000)
        self.logComment(f"Sleeping for {d} seconds")
        sleep(d)

    def create_pins(self):
        raise NotImplementedError()

    def create_ports(self):
        raise NotImplementedError()

    def _add_pin(self, pin):
        """Register a pin created from the board file, and its command."""
        self.pins[pin.key] = pin
        if pin.command:
            logger.debug(f"Adding {pin.command}")
            self.commands[pin.command] = pin.command
            setattr(self, pin.command, pin.set)

    def apply_config(self, config):
        """Drive this board as the board file ``config`` describes.

        Creates its ports and pins, sets each pin to its initial level, and
        adds each function of the board's script as a quick method.
        """
        reserved = set(dir(type(self)))
        clashes = sorted(
            name for name in (*config.commands, *config.functions) if name in reserved
        )
        if clashes:
            raise ConfigScriptError(
                f"{config.path}: {', '.join(clashes)} would replace pytactl's own "
                f"board method{'s' if len(clashes) > 1 else ''}; rename "
                f"{'them' if len(clashes) > 1 else 'it'}"
            )
        self.config = config
        self.create_ports()
        self.create_pins()
        for name in config.functions:
            logger.debug(f"Adding {name}")
            setattr(self, name, MethodType(_script_function(config, name), self))
            self.quick_methods[name] = QuickMethod(self, name)

    def _pin_configs(self):
        """The board file's pins, as the dicts the pin classes take."""
        for pin in self.config.pins.values():
            yield {
                "bus": pin.bus,
                "command": pin.command,
                "initial_value": bool(pin.initial),
                "inverted": pin.inverted,
                "pin_number": pin.number,
            }


def _script_function(config, name):
    """The quick method running function ``name`` of a board's script."""
    steps = [
        (
            statement.kind,
            statement.target,
            config.delay_ms(statement)
            if statement.kind == "delay"
            else statement.value,
        )
        for statement in config.functions[name].statements
    ]

    def run(board):
        board.logComment(f"====== {name} ======")
        for kind, target, value in steps:
            if kind == "set":
                getattr(board, target)(value)
            elif kind == "delay":
                board.delay(value)
            else:
                getattr(board, target)()

    run.__name__ = name
    return run


def _load(board_file):
    try:
        return boardconfig.load_board(board_file)
    except boardconfig.ConfigError as error:
        raise ConfigScriptError(str(error)) from error


def _find(boards_dir, description, **key):
    try:
        config = boardconfig.find_board(boards_dir, **key)
    except boardconfig.ConfigError as error:
        raise ConfigScriptError(str(error)) from error
    if config is None:
        logger.error("No board file in %s matches %s", boards_dir, description)
        sys.exit(1)
    return config


class DummyBoard(Board):
    def __init__(self, board_file):
        Board.__init__(self)
        self.usb_device = lambda: None
        self.usb_device.serial_number = "123456"
        self.apply_config(_load(board_file))

    def create_ports(self):
        logger.debug("creating ports")
        for bus in self.config.gpio_buses:
            self.ports[bus] = DummyPort(bus, "123456")
        if not self.config.gpio_buses:
            self.ports[0] = DummyPort("123456", None)

    def create_pins(self):
        logger.debug("creating pins")
        for config in self._pin_configs():
            pin = DummyPin(self, config)
            pin.setPort(self.ports.get(pin.bus, self.ports.get(0)))
            self._add_pin(pin)


class BughopperV1Board(Board):
    def __init__(self, usb_device):
        Board.__init__(self)
        self.usb_device = usb_device
        self.quick_methods.update({"powerOn": QuickMethod(self, "powerOn")})
        self.quick_methods.update({"bootToEDL": QuickMethod(self, "bootToEDL")})
        self.quick_methods.update({"powerOff": QuickMethod(self, "powerOff")})
        self.quick_methods.update({"reset": QuickMethod(self, "reset")})
        self.quick_methods.update(
            {"forceUsbcHostMode": QuickMethod(self, "forceUsbcHostMode")}
        )
        self.quick_methods.update({"wakeUp": QuickMethod(self, "wakeUp")})

        self.EDL_BIT = 0b00000001
        self.POWER_DISABLE_BIT = 0b00000100
        self.VOL_DOWN_BIT = 0b00001000

        self.EDL_MASK = self.EDL_BIT << 4
        self.POWER_DISABLE_MASK = self.POWER_DISABLE_BIT << 4
        self.VOL_DOWN_MASK = self.VOL_DOWN_BIT << 4

    def _ftdi_set_bitmode(self, bitmask):
        bmRequestType = usb.util.build_request_type(
            usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE
        )

        wValue = bitmask | (BITMODE_CBUS << 8)
        self.usb_device.ctrl_transfer(bmRequestType, SIO_SET_BITMODE_REQUEST, wValue)

    def powerOn(self):
        logger.debug("Power cycle to normal boot mode")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK | self.EDL_MASK | self.VOL_DOWN_MASK
        )

    def bootToEDL(self):
        logger.debug("Power cycle to USB boot mode")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.EDL_MASK | self.EDL_BIT)

    def reset(self):
        logger.debug("MPU reset pulse")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK | self.EDL_MASK | self.VOL_DOWN_MASK
        )

    def powerOff(self):
        logger.debug("MPU poweroff")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)

    def wakeUp(self):
        logger.debug("VOL_DOWN pulse to wake from suspend")
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK
            | self.EDL_MASK
            | self.VOL_DOWN_MASK
            | self.VOL_DOWN_BIT
        )
        sleep(WAKE_PULSE_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK | self.EDL_MASK | self.VOL_DOWN_MASK
        )

    def forceUsbcHostMode(self):
        logger.debug("Forcing host mode")
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK
            | self.VOL_DOWN_MASK
            | self.POWER_DISABLE_BIT
            | self.VOL_DOWN_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK
            | self.EDL_MASK
            | self.VOL_DOWN_MASK
            | self.VOL_DOWN_BIT
        )


class BughopperV2Board(Board):
    def __init__(self, usb_device):
        Board.__init__(self)
        self.usb_device = hid.Device(usb_device.idVendor, usb_device.idProduct)
        self.usb_device.serial_number = self.usb_device.serial
        self.quick_methods.update({"powerOn": QuickMethod(self, "powerOn")})
        self.quick_methods.update({"bootToEDL": QuickMethod(self, "bootToEDL")})
        self.quick_methods.update({"powerOff": QuickMethod(self, "powerOff")})
        self.quick_methods.update({"reset": QuickMethod(self, "reset")})
        self.quick_methods.update(
            {"forceUsbcHostMode": QuickMethod(self, "forceUsbcHostMode")}
        )
        self.quick_methods.update({"wakeUp": QuickMethod(self, "wakeUp")})

        self.CMD_GPIO = 0x1

        self.EDL_BIT = 0x1
        self.POWER_DISABLE_BIT = 0x4
        self.VOL_DOWN_BIT = 0x8

    def _hid_set_bitmode(self, command, gpio_value, mask=0xF):
        # The leading 0x00 is the HID report-ID placeholder for this unnumbered
        # RawHID report. hidapi always consumes byte 0 of a write as the report
        # ID: Linux/hidraw tolerates its absence, but on Windows the command
        # byte would be swallowed as a (non-existent) report ID and stripped,
        # so the firmware never sees the GPIO command. Prepending 0x00 keeps the
        # [command, gpio_value, mask] payload intact on every platform.
        self.usb_device.write(bytes([0x00, command, gpio_value, mask]))

    def powerOn(self):
        logger.debug("Power cycle to normal boot mode")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            0x0,
            self.POWER_DISABLE_BIT | self.EDL_BIT | self.VOL_DOWN_BIT,
        )

    def bootToEDL(self):
        logger.debug("Power cycle to USB boot mode")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO, self.EDL_BIT, self.EDL_BIT | self.POWER_DISABLE_BIT
        )

    def reset(self):
        logger.debug("MPU reset pulse")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            0x0,
            self.POWER_DISABLE_BIT | self.EDL_BIT | self.VOL_DOWN_BIT,
        )

    def powerOff(self):
        logger.debug("MPU poweroff")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )

    def wakeUp(self):
        logger.debug("VOL_DOWN pulse to wake from suspend")
        self._hid_set_bitmode(self.CMD_GPIO, self.VOL_DOWN_BIT, self.VOL_DOWN_BIT)
        sleep(WAKE_PULSE_DELAY)
        self._hid_set_bitmode(self.CMD_GPIO, 0x0, self.VOL_DOWN_BIT)

    def forceUsbcHostMode(self):
        logger.debug("Forcing host mode")
        self._hid_set_bitmode(
            self.CMD_GPIO,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT,
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            self.VOL_DOWN_BIT,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT | self.EDL_BIT,
        )


class FtdiBoard(Board):
    def __init__(self, usb_device, boards_dir=boardconfig.BOARDS_DIR):
        Board.__init__(self)
        self.usb_device = usb_device
        # A board whose USB product string no board file claims is driven
        # with the fallback board file (the ALPACA LITE default).
        self.apply_config(
            _find(
                boards_dir,
                f"USB product {usb_device.product!r}",
                usb_product=usb_device.product,
                fallback=True,
            )
        )

    def create_ports(self):
        for bus in self.config.gpio_buses:
            self.ports[bus] = FtdiPort(bus, self.usb_device.serial_number)

    def create_pins(self):
        for config in self._pin_configs():
            pin = FtdiPin(self, config)
            pin.setPort(self.ports.get(pin.bus))
            self._add_pin(pin)


class PsocBoard(Board):
    def __init__(self, usb_device, boards_dir=boardconfig.BOARDS_DIR):
        Board.__init__(self)
        self.usb_device = usb_device
        self.board_id = self.__get_board_id()
        self.apply_config(
            _find(boards_dir, f"PSOC board id {self.board_id}", board_id=self.board_id)
        )
        self.quick_methods.update({"devicePowerOn": QuickMethod(self, "devicePowerOn")})
        self.quick_methods.update(
            {"devicePowerOff": QuickMethod(self, "devicePowerOff")}
        )
        self.quick_methods.update(
            {"usbDevicePowerOn": QuickMethod(self, "usbDevicePowerOn")}
        )
        self.quick_methods.update(
            {"usbDevicePowerOff": QuickMethod(self, "usbDevicePowerOff")}
        )

    def devicePower(self, value):
        logger.debug(f"Calling devicePower {value}")
        if value:
            self.ports[0].call_method("devicePower", "on")
        else:
            self.ports[0].call_method("devicePower", "off")

    def usbDevicePower(self, value):
        logger.debug(f"Calling usbDevicePower {value}")
        if value:
            self.ports[0].call_method("usbDevicePower", "on")
        else:
            self.ports[0].call_method("usbDevicePower", "off")

    def devicePowerOn(self):
        self.devicePower(True)

    def devicePowerOff(self):
        self.devicePower(False)

    def usbDevicePowerOn(self):
        self.usbDevicePower(True)

    def usbDevicePowerOff(self):
        self.usbDevicePower(False)

    def __get_board_id(self):
        # connect to serial and call "getboardid"
        serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == self.usb_device.serial_number:
                serial_port = port.device
                break

        logger.info(f"Opening {serial_port}")
        expect_connection = None

        connection = serial.Serial()
        connection.baudrate = 115200
        connection.port = serial_port
        try:
            connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)
        expect_connection = fdpexpect.fdspawn(connection)
        if not expect_connection.isalive():
            logger.error("Expect connection not created")
            sys.exit(1)

        expect_connection.send("\r")
        expect_connection.expect("CMD")
        expect_connection.send("getboardid\r")
        expect_connection.expect("ok")
        ret_value = expect_connection.before
        expect_connection.close()
        # find returned value
        r = re.search(r"\d+", ret_value.decode())
        if r:
            return int(r.group(0))
        return None

    def create_ports(self):
        self.ports.update({0: PsocPort(self.usb_device.serial_number)})

    def create_pins(self):
        for config in self._pin_configs():
            pin = PsocPin(self, config)
            pin.setPort(self.ports.get(0))
            self._add_pin(pin)


class Pic32cxBoard(Board):
    @staticmethod
    def detect(serial_number):
        """Return True if a PIC32CX tty matching ``serial_number`` is present.

        The PIC32CX board is not visible to libusb, so it is discovered via
        the serial port by matching the serial number together with its USB
        VID/PID.
        """
        for port in serial.tools.list_ports.comports():
            if port.serial_number != serial_number:
                continue
            if port.vid is None or port.pid is None:
                continue
            if (
                port.vid == Board.ID_VENDOR_PIC32CX
                and port.pid == Board.ID_PRODUCT_PIC32CX
            ):
                return True
        return False

    def __init__(self, serial, boards_dir=boardconfig.BOARDS_DIR):
        Board.__init__(self)
        self.usb_device = lambda: None
        self.usb_device.serial_number = serial
        # The platform is identified by the part of the serial number before
        # "XX", which board files list as serial_prefix.
        self.serial_prefix = serial.split("XX")[0]
        self.apply_config(
            _find(
                boards_dir,
                f"serial prefix {self.serial_prefix!r} (serial {serial})",
                serial_prefix=self.serial_prefix,
            )
        )

    def create_ports(self):
        self.ports.update({0: Pic32cxPort(self.usb_device.serial_number)})

    def create_pins(self):
        for config in self._pin_configs():
            pin = Pic32cxPin(self, config)
            pin.setPort(self.ports.get(0))
            self._add_pin(pin)


class QuickMethod(dict):
    def __init__(self, board, name):
        self.name = name
        self.board = board
        dict.__init__(self, name=self.name, board=self.board.usb_device.serial_number)

    def call(self):
        try:
            getattr(self.board, self.name)()
        except Exception as e:
            logger.error(e)
            raise TACException
        return {}


class Pin(dict):
    def __init__(self, board, config):
        self.board = board
        self.bus = config.get("bus")
        self.command = config.get("command")
        self.initial_value = config.get("initial_value")
        self.value = self.initial_value
        self.inverted = config.get("inverted")
        self.pin_number = int(config.get("pin_number"))
        self.port = None
        dict.__init__(
            self,
            bus=self.bus,
            command=self.command,
            initial_value=self.initial_value,
            value=self.value,
            inverted=self.inverted,
            pin_number=self.pin_number,
            port=self.port,
        )

    @property
    def key(self):
        """The pin as board files name it: "D1" on FTDI, "53" elsewhere."""
        return f"{self.bus or ''}{self.pin_number}"

    def set(self, value):
        self.value = value
        super().__setitem__("value", self.value)

    def level(self, value):
        """The level to drive for the commanded ``value``.

        A pin marked ``inverted`` in the config is driven at the inverse of
        the commanded value, so that a script reads the same on every board
        (``battery 1`` disconnects the battery) whichever level the board's
        circuit needs. ``initial_value`` is a commanded value too.
        """
        return int(bool(value) != bool(self.inverted))

    def initialize(self):
        raise NotImplementedError()

    def setPort(self, port):
        self.port = port
        super().__setitem__("port", self.port)


class DummyPin(Pin):
    def initialize(self):
        logger.info(f"Initializing pin: {self.bus} {self.pin_number}")


class FtdiPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)
        self.pin_number_mask = 1 << self.pin_number

    def set(self, value):
        super().set(value)
        if not self.port:
            logger.warning(f"No port set for pin {self.bus}{self.pin_number}")
            return
        logger.debug(f"Port {self.bus} status: 0x{self.port.status:02X}")
        logger.debug(f"Setting {self.bus}{self.pin_number} to {value}")
        logger.debug(f"Mask: 0x{self.pin_number_mask:02X}")
        if self.level(self.value):
            self.port.write(self.port.status | self.pin_number_mask)
        else:
            self.port.write(self.port.status & ~self.pin_number_mask)

    def initialize(self):
        self.set(int(self.initial_value))

    def setPort(self, port):
        super().setPort(port)
        self.initialize()


class PsocPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)

    def set(self, value):
        lcl_value = None
        try:
            lcl_value = int(value)
        except ValueError:
            logger.error("Value has to be int")
            logger.error(f"Received {value}")
            return
        super().set(lcl_value)
        if not self.port:
            logger.warning(f"No port set for pin {self.pin_number}")
            return
        logger.debug(f"Setting {self.pin_number} to {lcl_value}")
        self.port.write(self.level(lcl_value), self.pin_number)

    def initialize(self):
        self.set(int(self.initial_value))

    def setPort(self, port):
        super().setPort(port)
        self.initialize()


class Pic32cxPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)

    def set(self, value):
        lcl_value = None
        try:
            lcl_value = int(value)
        except ValueError:
            logger.error("Value has to be int")
            logger.error(f"Received {value}")
            return
        super().set(lcl_value)
        if not self.port:
            logger.warning(f"No port set for pin {self.pin_number}")
            return
        logger.debug(f"Setting {self.pin_number} to {lcl_value}")
        self.port.write(self.level(lcl_value), self.pin_number)

    def initialize(self):
        # PIC32CX configs define no initial pin value, and the reference TAC
        # tool does not drive any pins on connect. Leave pins untouched until
        # explicitly commanded so connecting does not actuate the board.
        pass

    def setPort(self, port):
        super().setPort(port)


class Port(dict):
    def __init__(self, bus, serial):
        self.serial = serial
        self.bus = bus
        dict.__init__(self, serial=self.serial, bus=self.bus)

    def write(self, value, pin=None):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class DummyPort(Port):
    def write(self, value, pin=None):
        logger.info(f"Writing to port {self.bus} {self.serial}")

    def close(self):
        logger.info(f"Closing port {self.bus}")


class PsocPort(Port):
    def __init__(self, serialid):
        Port.__init__(self, None, serialid)
        self.serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == serialid:
                self.serial_port = port.device
                break

        self.expect_connection = None

        self.connection = serial.Serial()
        self.connection.baudrate = 115200
        self.connection.port = self.serial_port
        try:
            self.connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)
        self.expect_connection = fdpexpect.fdspawn(self.connection)
        if not self.expect_connection.isalive():
            logger.error("Expect connection not created")
            sys.exit(1)

    def close(self):
        if self.connection and self.connection.is_open:
            self.connection.close()

    def call_method(self, method, value):
        logger.debug(f"Calling {method} {value}")
        if self.expect_connection and self.expect_connection.isalive():
            self.expect_connection.send("\r")
            self.expect_connection.expect("CMD")
            message = f"{method} {value}\r"

            self.expect_connection.send(message)
            self.expect_connection.expect("ok")
        else:
            logger.error("No expect connection")
            sys.exit(1)

    def write(self, value, pin=None):
        if not pin:
            logger.warning("No pin selected")
            return
        if self.expect_connection and self.expect_connection.isalive():
            self.expect_connection.send("\r")
            self.expect_connection.expect("CMD")
            message = f"pin {value} {pin}\r"

            self.expect_connection.send(message)
            self.expect_connection.expect("ok")
        else:
            logger.error("No expect connection")
            sys.exit(1)


class Pic32cxPort(Port):
    def __init__(self, serialid):
        Port.__init__(self, None, serialid)
        self.serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == serialid:
                self.serial_port = port.device
                break

        if self.serial_port is None:
            logger.error(f"No serial port found for {serialid}")
            sys.exit(1)

        self.connection = serial.Serial()
        self.connection.baudrate = 115200
        self.connection.timeout = 0.5
        self.connection.port = self.serial_port
        try:
            self.connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)

        # Clear any pending data in the firmware's command buffer.
        self.__send("echo 1")

    def __send(self, message):
        logger.debug(f"Sending: {message}")
        self.connection.reset_input_buffer()
        self.connection.write(f"{message}\n".encode())
        self.connection.flush()
        # Drain the firmware's reply so it doesn't leak into the next command.
        response = self.connection.read_until().decode(errors="replace")
        logger.debug(f"Response: {response!r}")

    def write(self, value, pin=None):
        if pin is None:
            logger.warning("No pin selected")
            return
        # The firmware expects 'port 0' pins to be zero-padded to three digits.
        pin_str = str(pin)
        if len(pin_str) < 3:
            pin_str = "0" + pin_str
        self.__send(f"CONF:DIG:ON {int(bool(value))} (@{pin_str})")

    def close(self):
        if self.connection and self.connection.is_open:
            self.connection.close()


class FtdiPort(Port):
    def __init__(self, bus, serial, direction=0xFF):
        Port.__init__(self, bus, serial)
        self.direction = direction
        self.port = ord(self.bus) - ord("A") + 1
        self.url = f"ftdi://::{self.serial}/{self.port}"
        self.pins = {}
        self.status = 0x0
        self.gpio = GpioAsyncController()
        self.gpio.configure(self.url, direction=self.direction)

    def write(self, value, pin=None):
        self.status = value
        logger.debug(f"Port {self.bus} value: 0x{self.status:02X}")
        self.gpio.write(self.status)

    def close(self):
        logger.debug(f"Closing port {self.bus}")
        self.gpio.close()
