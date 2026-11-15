#!/usr/bin/env python3
"""Bounded relative-pointer probe for the normal Weston session."""

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
EV_REL = 2
SYN_REPORT = 0
REL_X = 0
REL_Y = 1
BTN_LEFT = 272

INPUT_PROP_POINTER = 2
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_PROPBIT = 0x4004556E
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--hold-ms", type=int, default=120)
    parser.add_argument("--settle-ms", type=int, default=500)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not (-4096 <= args.x <= 4096 and -4096 <= args.y <= 4096):
        raise ValueError("relative movement is outside the bounded probe range")
    if not (1 <= args.hold_ms <= 1000 and 0 <= args.settle_ms <= 2000):
        raise ValueError("pointer timing is outside the bounded probe range")
    if not sys.platform.startswith("linux"):
        raise RuntimeError("the probe requires Linux")

    name = "helmet-pointer-" + uuid.uuid4().hex[:20]
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    created = False
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_X)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_Y)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)

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

        # Clamp the pointer at the top-left edge, then apply the requested
        # bounded displacement so the target does not depend on cursor history.
        os.write(
            fd,
            b"".join(
                (
                    event(EV_REL, REL_X, -4096),
                    event(EV_REL, REL_Y, -4096),
                    event(EV_SYN, SYN_REPORT, 0),
                )
            ),
        )
        time.sleep(0.2)
        os.write(
            fd,
            b"".join(
                (
                    event(EV_REL, REL_X, args.x),
                    event(EV_REL, REL_Y, args.y),
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
                    "relative": [args.x, args.y],
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
