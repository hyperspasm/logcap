# logcap

Watch a serial log in the terminal and grab chunks of it to the clipboard.

Device output is written to stdout exactly as received, so colours and the terminal bell still work. While it runs:

| key    | action                                                       |
|--------|--------------------------------------------------------------|
| space  | start a capture; press again to restart it from this point   |
| return | end the capture and copy it to the clipboard                 |
| ctrl-c | quit                                                         |

The clipboard copy has ANSI escape sequences and bells stripped and CRLF normalised to LF. Pass `--raw` to copy the bytes exactly as received.

A capture that starts mid-line includes the whole line. If the device disappears (unplugged, re-enumerated) logcap waits for it to come back and keeps any capture in progress. Keys are never forwarded to the device; this is a viewer, not a terminal.

## Watching for text

```sh
./logcap -w 'error 123'
./logcap -w 'error \d+' -w 'assert|panic'
```

Each `-w` is a Python regular expression, matched case-insensitively anywhere in the line after colour codes are stripped. A matching line is printed in inverse video, preceded by a bell. Plain words work as-is; escape regex specials such as `. ( ) [ ] + * ?` with a backslash, e.g. `-w 'buf\[3\]'`. For a case-sensitive match wrap the pattern as `(?-i:Error)`.

In watch mode a line is shown once it is complete, so a line that stops short of its newline is held for up to 0.1 s before being shown as-is; if such a line then turns out to match, it is repeated highlighted. Without `-w` nothing is ever held back.

## Setup

```sh
./make_venv.sh                  # creates venv and installs pyserial
./make_venv.sh /path/to/python  # ...using a specific interpreter
```

## Usage

```sh
./logcap                              # auto-picks the one USB serial device plugged in
./logcap /dev/tty.usbserial-DM02BV9R  # or name it
./logcap -b 921600                    # other baud rate (default 115200)
./logcap --save-dir ~/captures        # also write each capture to a timestamped file
./logcap --no-dtr                     # don't assert DTR/RTS on open (boards that reset on DTR)
./logcap -w 'error \d+' -w panic     # highlight matching lines (see below)
```

`./logcap` is a small wrapper that runs `logcap.py` with the venv, and it works through a symlink, so `ln -s "$PWD/logcap" ~/bin/logcap` lets you run it from anywhere.
