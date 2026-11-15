#!/usr/bin/env python3
"""Limited temporary uinput keyboard; Linux kernel IO is never host evidence."""
import os
from pathlib import Path
import struct
import sys
import time
import uuid


KEYS = {'Tab': 15, 'Home': 102, 'Up': 103, 'Down': 108, 'Space': 57,
        'Escape': 1, 'Enter': 28}
CHORDS = {('Shift', 'Tab'): (42, 15), ('Alt', 'F4'): (56, 62)}
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


def event_format(word_bytes=None):
    word_bytes = struct.calcsize('l') if word_bytes is None else word_bytes
    if word_bytes not in (4, 8):
        raise ValueError('Linux timeval word size must be 4 or 8')
    return '<iiHHi' if word_bytes == 4 else '<qqHHi'


def encode_event(event_type, code, value, timestamp=None, word_bytes=None):
    timestamp = time.time() if timestamp is None else timestamp
    seconds = int(timestamp)
    microseconds = int(round((timestamp - seconds) * 1000000))
    if microseconds == 1000000:
        seconds, microseconds = seconds + 1, 0
    return struct.pack(event_format(word_bytes), seconds, microseconds, event_type, code, value)


def decode_event(raw, word_bytes=None):
    return struct.unpack(event_format(word_bytes), raw)


def device_nodes(name):
    nodes = []
    for path in Path('/sys/class/input').glob('event*/device/name'):
        try:
            if path.read_text().strip() == name:
                nodes.append('/dev/input/' + path.parents[1].name)
        except OSError:
            continue
    return sorted(nodes)


class LinuxIO:
    def open(self, path, flags):
        if not sys.platform.startswith('linux'):
            raise RuntimeError('uinput requires Linux; host tests do not exercise the kernel')
        return os.open(path, flags)

    def ioctl(self, fd, operation, value=0):
        import fcntl
        return fcntl.ioctl(fd, operation, value)

    write = staticmethod(os.write)
    close = staticmethod(os.close)


class VirtualKeyboard:
    def __init__(self, owner_check, io=None, device_lookup=None):
        self.owner_check = owner_check
        self.io = LinuxIO() if io is None else io
        self.device_lookup = device_nodes if device_lookup is None else device_lookup
        self.name = 'helmet-normal-' + uuid.uuid4().hex
        self.fd = None
        self.created = False
        self.held = []
        self.remaining = None

    def _write(self, data):
        if self.io.write(self.fd, data) != len(data):
            raise OSError('short uinput write')

    def __enter__(self):
        self.owner_check()
        self.fd = self.io.open('/dev/uinput', os.O_WRONLY | getattr(os, 'O_NONBLOCK', 0x800))
        try:
            self.io.ioctl(self.fd, UI_SET_EVBIT, 1)
            # Standard keyboard capability bits allow udev/libinput keyboard
            # classification. Capability is not permission to send those keys.
            for code in sorted(set(range(1, 84)) | set(KEYS.values())):
                self.io.ioctl(self.fd, UI_SET_KEYBIT, code)
            descriptor = struct.pack('<80sHHHHi', self.name.encode('ascii'), 3, 0x1, 0x1, 1, 0)
            self._write(descriptor + bytes(4 * 64 * 4))
            self.io.ioctl(self.fd, UI_DEV_CREATE)
            self.created = True
        except BaseException:
            self.close()
            raise
        return self

    def _release(self):
        errors = []
        for code in reversed(self.held):
            try:
                self._write(encode_event(1, code, 0))
            except OSError as error:
                errors.append(error)
        self.held.clear()
        try:
            self._write(encode_event(0, 0, 0))
        except OSError as error:
            errors.append(error)
        if errors:
            raise errors[0]

    def _send(self, codes):
        self.owner_check()
        if self.fd is None or not self.created:
            raise RuntimeError('keyboard is closed')
        try:
            for code in codes:
                # A failed/short write may still have submitted a key-down.
                self.held.append(code)
                self._write(encode_event(1, code, 1))
            self._write(encode_event(0, 0, 0))
            self._release()
        except BaseException:
            self.close()
            raise

    def press(self, key):
        if key not in KEYS:
            raise ValueError('key is outside the navigation whitelist')
        self._send((KEYS[key],))

    def chord(self, modifier, key):
        if (modifier, key) not in CHORDS:
            raise ValueError('chord is outside the navigation whitelist')
        self._send(CHORDS[(modifier, key)])

    def close(self):
        if self.fd is None:
            return
        errors = []
        if self.held:
            try:
                self._release()
            except OSError as error:
                errors.append(error)
        if self.created:
            try:
                self.io.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError as error:
                errors.append(error)
        try:
            self.io.close(self.fd)
        finally:
            self.fd, self.created = None, False
        # Kernel removal may be asynchronous; wait only a bounded interval.
        deadline = time.monotonic() + 2
        self.remaining = self.device_lookup(self.name)
        while self.remaining and time.monotonic() < deadline:
            time.sleep(.05)
            self.remaining = self.device_lookup(self.name)
        if self.remaining:
            errors.append(OSError('temporary uinput device remains present'))
        if errors:
            raise errors[0]

    def __exit__(self, error_type, error, traceback):
        self.close()
        return False
