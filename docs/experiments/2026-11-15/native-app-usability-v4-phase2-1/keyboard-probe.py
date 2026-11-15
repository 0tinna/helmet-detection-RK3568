#!/usr/bin/env python3
"""Send a bounded sequence of keys through a temporary uinput keyboard."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import struct
import time
import uuid


EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
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
    parser.add_argument("--keys", required=True, help="comma-separated Linux key codes")
    parser.add_argument("--hold-ms", type=int, default=60)
    parser.add_argument("--gap-ms", type=int, default=120)
    args = parser.parse_args()
    keys = [int(value, 0) for value in args.keys.split(",")]
    if not keys or len(keys) > 32 or any(not 1 <= value <= 767 for value in keys):
        raise ValueError("one to 32 valid key codes are required")
    if not 20 <= args.hold_ms <= 500 or not 50 <= args.gap_ms <= 1000:
        raise ValueError("key timing is outside the bounded range")

    name = "helmet-keyboard-" + uuid.uuid4().hex[:20]
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    created = False
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for key in sorted(set(keys)):
            fcntl.ioctl(fd, UI_SET_KEYBIT, key)
        setup = struct.pack(
            "<HHHH80sI", 3, 0x1, 0x1, 1, name.encode("ascii"), 0
        )
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        created = True
        deadline = time.monotonic() + 3
        while not device_nodes(name) and time.monotonic() < deadline:
            time.sleep(0.05)
        nodes = device_nodes(name)
        if not nodes:
            raise RuntimeError("temporary keyboard did not appear in input sysfs")
        time.sleep(0.5)
        for key in keys:
            os.write(
                fd,
                event(EV_KEY, key, 1) + event(EV_SYN, SYN_REPORT, 0),
            )
            time.sleep(args.hold_ms / 1000)
            os.write(
                fd,
                event(EV_KEY, key, 0) + event(EV_SYN, SYN_REPORT, 0),
            )
            time.sleep(args.gap_ms / 1000)
        print(
            json.dumps(
                {"device": name, "nodes": nodes, "keys": keys},
                separators=(",", ":"),
            )
        )
    finally:
        if created:
            try:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
            except OSError:
                pass
        os.close(fd)


if __name__ == "__main__":
    main()
