"""Raw serial↔stdio relay for the `/sbx flipper` interactive shell.

Spawned by `SerialConn.open_pty()` INSIDE a local PTY (stdin/stdout are the PTY
slave). It bridges a Flipper Zero serial port to that PTY so the device's own
interactive CLI renders in the room's sandbox pane and room keystrokes drive it —
the same raw-drive contract as the pager's `ssh -tt` channel, but for USB serial.

Pure `pyserial` (already on the host) — no picocom/minicom/screen dependency. On
device disappearance (unplugged cable) it exits, the bridge sees EOF and tears the
shell down cleanly.
"""
from __future__ import annotations

import os
import select
import sys

try:
    import serial
except ImportError:      # pragma: no cover - system python3 carries pyserial
    sys.stderr.write("serial_relay: pyserial not available\n")
    sys.exit(2)

BAUD = 230400


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: serial_relay <device> [baud]\n")
        return 2
    dev = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else BAUD
    try:
        ser = serial.Serial(dev, baud, timeout=0)
    except serial.SerialException as e:
        sys.stderr.write(f"serial_relay: cannot open {dev}: {e}\n")
        return 1

    out_fd, in_fd = sys.stdout.fileno(), sys.stdin.fileno()
    ser_fd = ser.fileno()
    # Nudge the Flipper CLI to print a fresh prompt on connect.
    try:
        ser.write(b"\r\n")
    except serial.SerialException:
        return 1
    try:
        while True:
            r, _, _ = select.select([ser_fd, in_fd], [], [], 0.1)
            if ser_fd in r:
                try:
                    data = ser.read(4096)
                except serial.SerialException:
                    break                     # device went away
                if data:
                    os.write(out_fd, data)
            if in_fd in r:
                data = os.read(in_fd, 4096)
                if not data:                  # PTY master closed → shell ended
                    break
                try:
                    ser.write(data)
                except serial.SerialException:
                    break
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
