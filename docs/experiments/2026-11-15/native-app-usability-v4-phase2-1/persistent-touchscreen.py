#!/usr/bin/env python3
"""Keep one DIRECT multitouch device alive and inject bounded test taps."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
import uuid


EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
SYN_REPORT = 0
BTN_TOUCH = 330
ABS_MT_TOUCH_MAJOR = 48
ABS_MT_WIDTH_MAJOR = 50
ABS_MT_POSITION_X = 53
ABS_MT_POSITION_Y = 54
ABS_MT_TRACKING_ID = 57

INPUT_PROP_DIRECT = 1
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_ABS_SETUP = 0x401C5504
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


def event_format():
    word_bytes = struct.calcsize("l")
    if word_bytes == 4:
        return "<iiHHi"
    if word_bytes == 8:
        return "<qqHHi"
    raise RuntimeError("unsupported Linux timeval word size")


def event(event_type, code, value):
    return struct.pack(event_format(), 0, 0, event_type, code, value)


def device_nodes(name):
    nodes = []
    for path in Path("/sys/class/input").glob("event*/device/name"):
        try:
            if path.read_text().strip() == name:
                nodes.append("/dev/input/" + path.parents[1].name)
        except OSError:
            continue
    return sorted(nodes)


def set_abs(fd, code, minimum, maximum):
    value = bytearray(
        struct.pack("<H2xiiiiii", code, 0, minimum, maximum, 0, 0, 0)
    )
    fcntl.ioctl(fd, UI_ABS_SETUP, value)


def tap(fd, x, y, hold_ms, tracking_id):
    os.write(
        fd,
        b"".join(
            (
                event(EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                event(EV_ABS, ABS_MT_POSITION_X, x),
                event(EV_ABS, ABS_MT_POSITION_Y, y),
                event(EV_ABS, ABS_MT_TOUCH_MAJOR, 100),
                event(EV_ABS, ABS_MT_WIDTH_MAJOR, 100),
                event(EV_KEY, BTN_TOUCH, 1),
                event(EV_SYN, SYN_REPORT, 0),
            )
        ),
    )
    time.sleep(hold_ms / 1000)
    os.write(
        fd,
        b"".join(
            (
                event(EV_ABS, ABS_MT_TRACKING_ID, -1),
                event(EV_KEY, BTN_TOUCH, 0),
                event(EV_SYN, SYN_REPORT, 0),
            )
        ),
    )


def drag(fd, x, y, to_x, to_y, duration_ms, tracking_id):
    os.write(
        fd,
        b"".join(
            (
                event(EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                event(EV_ABS, ABS_MT_POSITION_X, x),
                event(EV_ABS, ABS_MT_POSITION_Y, y),
                event(EV_ABS, ABS_MT_TOUCH_MAJOR, 100),
                event(EV_ABS, ABS_MT_WIDTH_MAJOR, 100),
                event(EV_KEY, BTN_TOUCH, 1),
                event(EV_SYN, SYN_REPORT, 0),
            )
        ),
    )
    steps = max(2, min(60, duration_ms // 16))
    for step in range(1, steps + 1):
        time.sleep(duration_ms / 1000 / steps)
        current_x = round(x + (to_x - x) * step / steps)
        current_y = round(y + (to_y - y) * step / steps)
        os.write(
            fd,
            b"".join(
                (
                    event(EV_ABS, ABS_MT_POSITION_X, current_x),
                    event(EV_ABS, ABS_MT_POSITION_Y, current_y),
                    event(EV_SYN, SYN_REPORT, 0),
                )
            ),
        )
    os.write(
        fd,
        b"".join(
            (
                event(EV_ABS, ABS_MT_TRACKING_ID, -1),
                event(EV_KEY, BTN_TOUCH, 0),
                event(EV_SYN, SYN_REPORT, 0),
            )
        ),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--raw-width", type=int, default=1080)
    parser.add_argument("--raw-height", type=int, default=1920)
    parser.add_argument("--hold-ms", type=int, default=120)
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        raise RuntimeError("the probe requires Linux")
    if min(args.width, args.height, args.raw_width, args.raw_height) < 2:
        raise RuntimeError("display dimensions must be at least two pixels")
    if not 1 <= args.hold_ms <= 1000:
        raise RuntimeError("hold duration is outside the bounded range")

    socket_path = Path(args.socket)
    receipt_path = Path(args.receipts)
    ready_path = Path(args.ready)
    for path in (socket_path, receipt_path, ready_path):
        if path.exists():
            raise RuntimeError("refusing to overwrite probe state: " + str(path))

    name = "helmet-touchscreen-" + uuid.uuid4().hex[:20]
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    created = False
    server = None
    stopping = False

    def stop(_number, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_MT_TOUCH_MAJOR)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_MT_WIDTH_MAJOR)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_MT_POSITION_X)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_MT_POSITION_Y)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_MT_TRACKING_ID)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        set_abs(fd, ABS_MT_TOUCH_MAJOR, 0, 255)
        set_abs(fd, ABS_MT_WIDTH_MAJOR, 0, 255)
        set_abs(fd, ABS_MT_POSITION_X, 0, args.raw_width - 1)
        set_abs(fd, ABS_MT_POSITION_Y, 0, args.raw_height - 1)
        set_abs(fd, ABS_MT_TRACKING_ID, 0, 255)
        setup = struct.pack(
            "<HHHH80sI", 0x18, 0x2020, 0x0001, 1, name.encode("ascii"), 0
        )
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        created = True

        deadline = time.monotonic() + 3
        while not device_nodes(name) and time.monotonic() < deadline:
            time.sleep(0.05)
        nodes = device_nodes(name)
        if not nodes:
            raise RuntimeError("temporary touchscreen did not appear in input sysfs")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(4)
        server.settimeout(0.5)
        ready_path.write_text(
            json.dumps(
                {
                    "device": name,
                    "nodes": nodes,
                    "socket": str(socket_path),
                    "screen": [args.width, args.height],
                    "raw": [args.raw_width, args.raw_height],
                    "property": "INPUT_PROP_DIRECT",
                    "protocol": "multitouch type B without MT_SLOT",
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="ascii",
        )
        print("READY " + ready_path.read_text(encoding="ascii").strip(), flush=True)

        tracking_id = 0
        while not stopping:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)
                raw = b""
                while b"\n" not in raw and len(raw) <= 4096:
                    chunk = connection.recv(1024)
                    if not chunk:
                        break
                    raw += chunk
                try:
                    value = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
                    x, y = value["x"], value["y"]
                    label = value["label"]
                    if type(x) is not int or type(y) is not int:
                        raise ValueError("coordinates must be integers")
                    if not (0 <= x < args.width and 0 <= y < args.height):
                        raise ValueError("coordinate outside display")
                    if not isinstance(label, str) or not 1 <= len(label) <= 80:
                        raise ValueError("label is required")
                    to_x = value.get("to_x")
                    to_y = value.get("to_y")
                    duration_ms = value.get("duration_ms", 450)
                    if (to_x is None) != (to_y is None):
                        raise ValueError("both drag endpoints are required")
                    if to_x is not None:
                        if type(to_x) is not int or type(to_y) is not int:
                            raise ValueError("drag coordinates must be integers")
                        if not (0 <= to_x < args.width and 0 <= to_y < args.height):
                            raise ValueError("drag endpoint outside display")
                    if type(duration_ms) is not int or not 100 <= duration_ms <= 2000:
                        raise ValueError("drag duration must be 100..2000 ms")
                    raw_x = round(x * (args.raw_width - 1) / (args.width - 1))
                    raw_y = round(y * (args.raw_height - 1) / (args.height - 1))
                    tracking_id = tracking_id % 255 + 1
                    if to_x is None:
                        tap(fd, raw_x, raw_y, args.hold_ms, tracking_id)
                        kind = "tap"
                        raw_to = None
                    else:
                        raw_to_x = round(to_x * (args.raw_width - 1) / (args.width - 1))
                        raw_to_y = round(to_y * (args.raw_height - 1) / (args.height - 1))
                        drag(
                            fd,
                            raw_x,
                            raw_y,
                            raw_to_x,
                            raw_to_y,
                            duration_ms,
                            tracking_id,
                        )
                        kind = "drag"
                        raw_to = [raw_to_x, raw_to_y]
                    row = {
                        "label": label,
                        "kind": kind,
                        "screen": [x, y],
                        "raw": [raw_x, raw_y],
                        "to_screen": None if to_x is None else [to_x, to_y],
                        "to_raw": raw_to,
                        "duration_ms": None if to_x is None else duration_ms,
                        "device": name,
                        "nodes": nodes,
                        "timestamp_monotonic": time.monotonic(),
                    }
                    with receipt_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    reply = {"ok": True, **row}
                except Exception as error:
                    reply = {"ok": False, "error": type(error).__name__ + ": " + str(error)}
                connection.sendall(
                    json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n"
                )
    finally:
        if server is not None:
            server.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        if created:
            try:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
            except OSError:
                pass
        os.close(fd)


if __name__ == "__main__":
    main()
