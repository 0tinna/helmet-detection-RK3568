#!/usr/bin/env python3
"""Send one labeled tap to the persistent test touchscreen."""

import argparse
import json
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--to-x", type=int)
    parser.add_argument("--to-y", type=int)
    parser.add_argument("--duration-ms", type=int, default=450)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if (args.to_x is None) != (args.to_y is None):
        parser.error("--to-x and --to-y must be provided together")
    payload = json.dumps(
        {
            "x": args.x,
            "y": args.y,
            "to_x": args.to_x,
            "to_y": args.to_y,
            "duration_ms": args.duration_ms,
            "label": args.label,
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(args.socket)
        connection.sendall(payload)
        raw = b""
        while b"\n" not in raw and len(raw) <= 4096:
            chunk = connection.recv(1024)
            if not chunk:
                break
            raw += chunk
    reply = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
    print(json.dumps(reply, separators=(",", ":")))
    if not reply.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
