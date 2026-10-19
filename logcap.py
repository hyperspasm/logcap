#!/usr/bin/env python3
"""
logcap: watch a serial log and grab chunks of it to the clipboard.

Everything the device sends is written to stdout exactly as received, so
colours and the terminal bell still work.  Keys:

    space    start a capture (press again to restart it from this point)
    return   end the capture and copy it to the clipboard
    ctrl-c   quit

Lines matching a --watch pattern are shown in inverse video, preceded by a
bell, so they catch your eye (and ear) when you are looking elsewhere.

The clipboard copy is cleaned up unless --raw is given: ANSI escape
sequences and terminal bells are removed and CRLF becomes LF.
"""

import argparse
import datetime
import glob
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty

try:
    import serial
except ImportError:
    sys.exit("logcap: pyserial is not installed; run ./make_venv.sh and start it with ./logcap")

KEYS_HELP = r"""
keys:
  space    start a capture (press again to restart it from this point)
  return   end the capture and copy it to the clipboard
  ctrl-c   quit

watch patterns are Python regular expressions, matched case-insensitively
anywhere in the line after colour codes are stripped, for example:
  -w 'error 123'    -w 'error \d+'    -w 'assert|panic'    -w '^E '
"""

PORT_PATTERNS = (
    "/dev/tty.usbserial-*",
    "/dev/tty.usbmodem*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
)

# CSI  (ESC [ ... final byte): colours, cursor movement, erase
# OSC  (ESC ] ... BEL or ESC \): window title and friends
# plus any other two-byte ESC sequence
ANSI_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")
BELL = b"\x07"
INVERSE = b"\x1b[7m"
RESET = b"\x1b[0m"

READ_CHUNK = 65536
LINE_MAX = 65536           # a "line" longer than this is shown and forgotten (no newline in sight)
HOLD_DELAY = 0.1           # watch mode: how long a partial line may wait for its newline before being shown
RECONNECT_INTERVAL = 0.5   # seconds between attempts to (re)open the port

CLIPBOARD_COMMANDS = (
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


def strip_ansi(data: bytes) -> bytes:
    """Remove ANSI escape sequences and bells."""
    return ANSI_RE.sub(b"", data).replace(BELL, b"")


def clean(data: bytes) -> bytes:
    """Strip ANSI escapes and bells, normalise CRLF to LF."""
    return strip_ansi(data).replace(b"\r\n", b"\n")


def copy_to_clipboard(data: bytes) -> None:
    for cmd in CLIPBOARD_COMMANDS:
        if shutil.which(cmd[0]):
            break
    else:
        raise RuntimeError("no clipboard tool found (pbcopy, wl-copy, xclip or xsel)")
    env = dict(os.environ)
    env.setdefault("LC_CTYPE", "UTF-8")  # make pbcopy read the bytes as UTF-8
    subprocess.run(cmd, input=data, env=env, check=True)


def save_capture(directory: str, text: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"capture-{stamp}.log")
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(directory, f"capture-{stamp}-{n}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def autodetect_port() -> str:
    found = sorted(p for pattern in PORT_PATTERNS for p in glob.glob(pattern))
    if len(found) == 1:
        return found[0]
    if not found:
        sys.exit("logcap: no serial device found; pass the port, e.g. logcap /dev/tty.usbserial-XXXX")
    sys.exit("logcap: more than one serial device found, pass one of:\n  " + "\n  ".join(found))


def open_port(path: str, baud: int, assert_dtr: bool) -> serial.Serial:
    ser = serial.Serial(baudrate=baud, timeout=0, exclusive=True)
    ser.dtr = assert_dtr   # applied when the port opens
    ser.rts = assert_dtr
    ser.port = path
    ser.open()
    return ser


class LogCap:
    def __init__(self, args):
        self.args = args
        self.watch = args.watch    # compiled regexes; empty when not watching
        self.out = sys.stdout.buffer
        self.styled = sys.stderr.isatty()
        self.line = bytearray()    # raw bytes of the line in progress (since the last newline)
        self.shown = 0             # how many of those bytes are already on the screen
        self.hold_since = None     # when unshown bytes first appeared, while waiting for the newline
        self.mid_line = False      # the screen cursor is not at the start of a line
        self.capture = None        # bytearray while a capture is in progress
        self.ser = None
        self.last_open_error = None

    # -- status output ------------------------------------------------------

    def say(self, msg: str) -> None:
        """Status line on stderr, on its own line, visually distinct from device output."""
        self.out.flush()
        prefix = "\n" if self.mid_line else ""
        tag = "\x1b[7m logcap \x1b[0m" if self.styled else "[logcap]"
        sys.stderr.write(f"{prefix}{tag} {msg}\n")
        sys.stderr.flush()
        self.mid_line = False

    # -- device output ------------------------------------------------------

    def write(self, data: bytes) -> None:
        if data:
            self.out.write(data)
            self.out.flush()
            self.mid_line = not data.endswith(b"\n")

    def matches(self, body: bytes) -> bool:
        text = strip_ansi(body).decode("utf-8", errors="replace")
        return any(p.search(text) for p in self.watch)

    def emit_line(self, line: bytes) -> None:
        """Write one complete line, ending included. Its first self.shown bytes are already on screen."""
        body = line.rstrip(b"\r\n")
        if not (self.watch and self.matches(body)):
            self.write(line[self.shown:])
        else:
            ending = line[len(body):]
            highlighted = BELL + INVERSE + strip_ansi(body) + RESET + ending
            if self.shown:
                # part of the line is already on screen: finish it, then repeat it highlighted
                self.write(line[self.shown:] + highlighted)
            else:
                self.write(highlighted)

    def flush_partial(self) -> None:
        """Show whatever part of the line in progress is not on screen yet."""
        if self.shown < len(self.line):
            self.write(bytes(self.line[self.shown:]))
            self.shown = len(self.line)
        self.hold_since = None

    def handle_serial(self) -> None:
        try:
            data = self.ser.read(READ_CHUNK)
        except (serial.SerialException, OSError):
            self.drop_port()
            return
        if not data:
            return
        if self.capture is not None:
            self.capture.extend(data)      # captures always get the raw bytes
        self.line.extend(data)
        while True:
            nl = self.line.find(b"\n", self.shown)
            if nl < 0:
                break
            self.emit_line(bytes(self.line[: nl + 1]))
            del self.line[: nl + 1]
            self.shown = 0
            self.hold_since = None
        if len(self.line) > LINE_MAX:
            self.flush_partial()
            del self.line[:]
            self.shown = 0
        if not self.watch:
            self.flush_partial()           # nothing to match against: show everything immediately
        elif self.shown < len(self.line) and self.hold_since is None:
            self.hold_since = time.monotonic()

    # -- serial port ----------------------------------------------------------

    def try_open(self) -> None:
        try:
            self.ser = open_port(self.args.port, self.args.baud, not self.args.no_dtr)
        except (serial.SerialException, OSError) as e:
            if not os.path.exists(self.args.port):
                msg = f"waiting for {self.args.port} to appear..."
            else:
                msg = f"cannot open {self.args.port}: {e} (retrying)"
            if msg != self.last_open_error:
                self.say(msg)
                self.last_open_error = msg
            return
        self.last_open_error = None
        self.say(f"connected to {self.args.port} @ {self.args.baud}")

    def drop_port(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.flush_partial()
        note = " (capture still running)" if self.capture is not None else ""
        self.say(f"lost {self.args.port}; waiting for it to come back{note}")
        del self.line[:]           # the device will come back mid-reboot; don't glue its first line onto this one
        self.shown = 0
        # the retry loop would announce "waiting for ... to appear" right away; we just said so
        self.last_open_error = f"waiting for {self.args.port} to appear..."

    # -- keys -----------------------------------------------------------------

    def handle_keys(self, keys: bytes) -> None:
        for key in keys:
            if key == 0x20:              # space
                self.start_capture()
            elif key in (0x0D, 0x0A):    # return arrives as CR or LF depending on the tty mode
                self.end_capture()
            # anything else is ignored: keys are never forwarded to the device

    def start_capture(self) -> None:
        restarted = self.capture is not None
        self.capture = bytearray(self.line)   # include the line in progress
        self.say("capture restarted" if restarted else "capture started")

    def end_capture(self) -> None:
        if self.capture is None:
            self.say("no capture in progress (press space to start one)")
            return
        data = bytes(self.capture)
        self.capture = None
        if not self.args.raw:
            data = clean(data)
        if not data.strip():
            self.say("capture is empty; clipboard left alone")
            return
        text = data.decode("utf-8", errors="replace")
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        try:
            copy_to_clipboard(text.encode("utf-8"))
        except Exception as e:
            self.say(f"clipboard copy failed: {e}")
        else:
            self.say(f"copied {lines} lines ({len(text)} chars) to the clipboard")
        if self.args.save_dir:
            try:
                self.say(f"saved to {save_capture(self.args.save_dir, text)}")
            except OSError as e:
                self.say(f"could not save capture: {e}")

    # -- main loop ------------------------------------------------------------

    def run(self) -> None:
        stdin_fd = sys.stdin.fileno()
        self.say("space: start/restart capture   return: copy to clipboard   ctrl-c: quit")
        if self.watch:
            self.say("watching for: " + "   ".join(repr(p.pattern) for p in self.watch))
        while True:
            if self.ser is None:
                self.try_open()
            fds = [stdin_fd]
            if self.ser is not None:
                fds.append(self.ser.fileno())
            timeout = RECONNECT_INTERVAL
            if self.hold_since is not None:
                timeout = min(timeout, max(0.0, self.hold_since + HOLD_DELAY - time.monotonic()))
            ready, _, _ = select.select(fds, [], [], timeout)
            if stdin_fd in ready:
                keys = os.read(stdin_fd, 64)
                if not keys:
                    raise EOFError
                self.handle_keys(keys)
            if self.ser is not None and self.ser.fileno() in ready:
                self.handle_serial()
            if self.hold_since is not None and time.monotonic() - self.hold_since >= HOLD_DELAY:
                self.flush_partial()       # the newline is taking too long; show the line so far


def parse_args():
    p = argparse.ArgumentParser(
        prog="logcap",
        description="Dump a serial port to the terminal; space starts a capture, return copies it to the clipboard.",
        epilog=KEYS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("port", nargs="?",
                   help="serial device (default: the one USB serial device that is plugged in)")
    p.add_argument("-b", "--baud", type=int, default=115200, help="baud rate (default: 115200)")
    p.add_argument("-w", "--watch", metavar="REGEX", action="append", default=[],
                   help="show lines matching REGEX in inverse video, preceded by a bell (repeatable)")
    p.add_argument("--raw", action="store_true",
                   help="copy the capture exactly as received (keep ANSI escapes, bells and CRLF)")
    p.add_argument("--save-dir", metavar="DIR",
                   help="also write each capture to DIR/capture-<timestamp>.log")
    p.add_argument("--no-dtr", action="store_true",
                   help="do not assert DTR/RTS when opening the port (for boards that reset on DTR)")
    args = p.parse_args()
    if args.save_dir:
        args.save_dir = os.path.expanduser(args.save_dir)
    try:
        args.watch = [re.compile(w, re.IGNORECASE) for w in args.watch]
    except re.error as e:
        p.error(f"bad --watch pattern {e.pattern!r}: {e}")
    return args


def main() -> None:
    args = parse_args()
    if args.port is None:
        args.port = autodetect_port()
    if not sys.stdin.isatty():
        sys.exit("logcap: stdin must be a terminal (space and return are read from it)")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)          # keys arrive immediately and are not echoed; ctrl-c still works
    app = LogCap(args)
    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        app.flush_partial()    # don't swallow a held line on the way out
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if app.ser is not None:
            app.ser.close()
        if app.mid_line:
            sys.stderr.write("\n")   # leave the shell prompt on a fresh line


if __name__ == "__main__":
    main()
