#!/usr/bin/env python3
"""Bounded absolute-pointer probe with the corrected uinput ABI."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import struct
import sys
import time
import uuid


EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
SYN_REPORT = 0
ABS_X = 0
ABS_Y = 1
BTN_LEFT = 272

INPUT_PROP_POINTER = 2
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
    value = bytearray(struct.pack("<H2xiiiiii", code, 0, minimum, maximum, 0, 0, 0))
    fcntl.ioctl(fd, UI_ABS_SETUP, value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--hold-ms", type=int, default=120)
    parser.add_argument("--settle-ms", type=int, default=500)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not (0 <= args.x < args.width and 0 <= args.y < args.height):
        raise ValueError("pointer coordinate is outside the declared display")
    if not (1 <= args.hold_ms <= 1000 and 0 <= args.settle_ms <= 2000):
        raise ValueError("pointer timing is outside the bounded probe range")
    if not sys.platform.startswith("linux"):
        raise RuntimeError("the probe requires Linux")

    name = "helmet-absolute-" + uuid.uuid4().hex[:20]
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    created = False
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_X)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_Y)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)
        set_abs(fd, ABS_X, 0, args.width - 1)
        set_abs(fd, ABS_Y, 0, args.height - 1)

        setup = struct.pack("<HHHH80sI", 3, 0x1, 0x1, 1, name.encode("ascii"), 0)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        created = True

        deadline = time.monotonic() + 2
        while not device_nodes(name) and time.monotonic() < deadline:
            time.sleep(0.05)
        nodes = device_nodes(name)
        if not nodes:
            raise RuntimeError("temporary pointer did not appear in input sysfs")
        time.sleep(0.35)

        os.write(
            fd,
            b"".join(
                (
                    event(EV_ABS, ABS_X, args.x),
                    event(EV_ABS, ABS_Y, args.y),
                    event(EV_SYN, SYN_REPORT, 0),
                )
            ),
        )
        time.sleep(0.2)
        os.write(
            fd,
            b"".join(
                (
                    event(EV_KEY, BTN_LEFT, 1),
                    event(EV_SYN, SYN_REPORT, 0),
                )
            ),
        )
        time.sleep(args.hold_ms / 1000)
        os.write(
            fd,
            b"".join(
                (
                    event(EV_KEY, BTN_LEFT, 0),
                    event(EV_SYN, SYN_REPORT, 0),
                )
            ),
        )
        time.sleep(args.settle_ms / 1000)
        print(
            json.dumps(
                {
                    "label": args.label,
                    "device": name,
                    "nodes": nodes,
                    "screen": [args.x, args.y],
                    "range": [[0, args.width - 1], [0, args.height - 1]],
                    "button": "BTN_LEFT",
                    "property": "INPUT_PROP_POINTER",
                },
                separators=(",", ":"),
            )
        )
    finally:
        if created:
            try:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
            finally:
                pass
        os.close(fd)


if __name__ == "__main__":
    main()
