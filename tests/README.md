# pytactl test suite

The tests run offline, with the USB devices, GPIO and serial hardware mocked
(`tests/conftest.py`), against the board files bundled in `pytactl/boards/`.

```sh
. ./venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

| File | What it covers |
|------|----------------|
| `test_boardconfig.py` | The board file format: layers and how they override each other, script statements, the checks and the file and line they report, finding a board, `describe`. |
| `test_bundled_boards.py` | Every bundled board file loads; no two claim the same USB product string, board id or serial prefix; every profile is used. |
| `test_config_loading.py` | Every bundled board file driven through its platform's board class: its functions are quick methods, its pins commands, and every function runs. Boards that lack `powerOn`, `powerOff` or `bootToEDL` are listed there, with why. |
| `test_create_board.py` | `Board.create_board()`: dispatch by USB vendor/product, and how FTDI, PSOC and PIC32CX boards are recognised against the real board files; the Bughopper boards. |
| `test_scripts.py` | Running a board's script: statement order, timings, initial levels, pins without a command, and how a broken board file reaches the user. |
| `test_pins.py` | Pin polarity: an inverted pin is driven at the inverse level. |
| `test_smoke.py` | The CLI: `listconfigs`, `showconfig`. |

A distribution that strips the bundled board files still runs everything
that does not need them; the rest skips.
