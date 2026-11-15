"""Interchangeable BGR sources with paced, looping file replay.

Sources implement read(stop_event=None) -> (ok, image) and release(). The
capture worker owns release(); a caller must never release a blocked reader.
"""
import glob
import math
from pathlib import Path
import threading
import time

import cv2


IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}


class ReplayPacer:
    def __init__(self, fps):
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('replay FPS must be finite and positive')
        self.period = 1.0 / fps
        self.next_at = None
        self.stopped = threading.Event()

    def wait(self, stop_event=None):
        now = time.monotonic()
        target = now if self.next_at is None else self.next_at
        while now < target:
            if self.stopped.is_set() or (stop_event is not None and stop_event.is_set()):
                return False
            self.stopped.wait(min(target - now, 0.02))
            now = time.monotonic()
        if self.stopped.is_set() or (stop_event is not None and stop_event.is_set()):
            return False
        # Do not issue a burst when a reader falls behind the replay clock.
        self.next_at = max(target + self.period, now + self.period)
        return True


class ImageFolderSource:
    def __init__(self, folder, replay_fps=10.0):
        self.pacer = ReplayPacer(replay_fps)
        path = Path(folder)
        if path.is_dir():
            self.files = sorted(str(item) for item in path.iterdir()
                                if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
        elif path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            self.files = [str(path)]
        else:
            self.files = sorted(item for item in glob.glob(str(folder))
                                if Path(item).is_file() and Path(item).suffix.lower() in IMAGE_SUFFIXES)
        if not self.files:
            raise ValueError('no replay images in ' + str(folder))
        # Validate every file so a permanently broken replay cannot report normal startup.
        for item in self.files:
            image = cv2.imread(item)
            if image is None or image.size == 0:
                raise ValueError('unreadable replay image: ' + item)
        self.idx = 0

    def read(self, stop_event=None):
        if not self.pacer.wait(stop_event):
            return False, None
        path = self.files[self.idx % len(self.files)]
        self.idx += 1
        image = cv2.imread(path)
        return image is not None and image.size > 0, image

    def release(self):
        self.pacer.stopped.set()


class VideoSource:
    def __init__(self, path, width=640, height=480, replay_fps=10.0):
        if width <= 0 or height <= 0:
            raise ValueError('capture dimensions must be positive')
        self.path = str(path)
        self.width, self.height = width, height
        self.is_file = not (self.path.startswith('/dev/') or '://' in self.path
                            or self.path.isdecimal())
        if self.is_file and not Path(self.path).is_file():
            raise ValueError('missing video source: ' + self.path)
        self.pacer = ReplayPacer(replay_fps) if self.is_file else None
        self.cap = self._open()
        self.prefetched = None
        self.next_reconnect = 0.0
        self.needs_reconnect = False
        if not self.cap.isOpened():
            self.cap.release()
            raise ValueError('cannot open video source: ' + self.path)
        if self.is_file:
            ok, self.prefetched = self.cap.read()
            if not ok or self.prefetched is None or self.prefetched.size == 0:
                self.cap.release()
                raise ValueError('empty or undecodable video: ' + self.path)

    def _open(self):
        if self.path.startswith('/dev/video'):
            cap = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        elif self.path.isdecimal():
            cap = cv2.VideoCapture(int(self.path))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        elif '://' in self.path:
            # FFmpeg timeouts bound ordinary network read/open interruptions.
            cap = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1000,
            ])
        else:
            cap = cv2.VideoCapture(self.path)
        return cap

    def read(self, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            return False, None
        if self.pacer is not None and not self.pacer.wait(stop_event):
            return False, None
        if self.prefetched is not None:
            frame, self.prefetched = self.prefetched, None
            return True, frame
        if self.needs_reconnect:
            if time.monotonic() < self.next_reconnect:
                return False, None
            self.cap.release()
            self.cap = self._open()
            self.next_reconnect = time.monotonic() + 1.0
            if not self.cap.isOpened():
                return False, None
            self.needs_reconnect = False
        ok, frame = self.cap.read()
        if not ok and self.is_file:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        elif not ok or frame is None or frame.size == 0:
            # Surface the interruption before a potentially successful/blocking
            # reconnect. The pipeline invalidates its epoch and alarm evidence
            # before the next read can deliver a replacement frame.
            self.needs_reconnect = True
            return False, None
        return bool(ok and frame is not None and frame.size > 0), frame

    def release(self):
        if self.pacer is not None:
            self.pacer.stopped.set()
        self.cap.release()


def make_source(path, w=640, h=480, replay_fps=10.0):
    if not str(path).strip():
        raise ValueError('source cannot be empty')
    # Validate FPS consistently, including camera configurations prepared for replay.
    if not math.isfinite(replay_fps) or replay_fps <= 0:
        raise ValueError('replay FPS must be finite and positive')
    source_path = Path(path)
    if source_path.is_dir() or source_path.suffix.lower() in IMAGE_SUFFIXES or glob.has_magic(str(path)):
        return ImageFolderSource(path, replay_fps)
    return VideoSource(path, w, h, replay_fps)
