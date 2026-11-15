"""Real image/video replay checks; no camera or RKNN is needed."""
import importlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np


class ReplaySources(unittest.TestCase):
    def sources(self):
        try:
            return importlib.import_module('helmet.sources')
        except ImportError:
            self.fail('reusable replay sources have not been implemented')

    def test_image_replay_is_paced_and_loops_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as folder:
            cv2.imwrite(str(Path(folder) / 'b.png'), np.full((12, 16, 3), 90, np.uint8))
            cv2.imwrite(str(Path(folder) / 'a.png'), np.full((12, 16, 3), 30, np.uint8))
            source = self.sources().ImageFolderSource(folder, replay_fps=20)
            started = time.monotonic()
            frames = [source.read()[1] for _ in range(3)]
            self.assertGreaterEqual(time.monotonic() - started, 0.085)
            self.assertEqual([int(frame[0, 0, 0]) for frame in frames], [30, 90, 30])
            source.release()

    def test_empty_and_corrupt_image_folders_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                self.sources().ImageFolderSource(folder)
            (Path(folder) / 'bad.jpg').write_bytes(b'broken')
            with self.assertRaises(ValueError):
                self.sources().ImageFolderSource(folder)

    def test_single_image_and_missing_source_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.png'
            cv2.imwrite(str(path), np.zeros((8, 8, 3), np.uint8))
            source = self.sources().make_source(str(path), replay_fps=10)
            self.assertTrue(source.read()[0])
            source.release()
            with self.assertRaises(ValueError):
                self.sources().make_source(str(Path(folder) / 'absent.mp4'))
            with self.assertRaises(ValueError):
                self.sources().make_source(folder, replay_fps=0)

    def test_stop_interrupts_replay_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            cv2.imwrite(str(Path(folder) / 'a.png'), np.zeros((8, 8, 3), np.uint8))
            source = self.sources().ImageFolderSource(folder, replay_fps=0.1)
            source.read()
            stopped = threading.Event()
            stopped.set()
            started = time.monotonic()
            self.assertFalse(source.read(stopped)[0])
            self.assertLess(time.monotonic() - started, 0.1)
            source.release()

    def test_video_replay_is_paced_and_rewinds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'two.avi')
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'MJPG'), 20, (32, 24))
            self.assertTrue(writer.isOpened(), 'local MJPG encoder is required for replay test')
            writer.write(np.full((24, 32, 3), 30, np.uint8))
            writer.write(np.full((24, 32, 3), 90, np.uint8))
            writer.release()
            source = self.sources().make_source(path, replay_fps=20)
            started = time.monotonic()
            frames = [source.read()[1] for _ in range(3)]
            self.assertGreaterEqual(time.monotonic() - started, 0.085)
            self.assertEqual([int(frame[0, 0, 0]) for frame in frames], [30, 90, 30])
            source.release()

    def test_live_video_reconnect_surfaces_failure_before_recovered_frame(self):
        sources = self.sources()
        class Capture:
            def __init__(self, frames):
                self.frames = iter(frames)
            def isOpened(self):
                return True
            def read(self):
                return next(self.frames)
            def release(self):
                pass
        first = np.full((24, 32, 3), 30, np.uint8)
        recovered = np.full((24, 32, 3), 90, np.uint8)
        with patch.object(sources.VideoSource, '_open', side_effect=[
                Capture([(True, first), (False, None)]), Capture([(True, recovered)])]):
            source = sources.VideoSource('0')
            self.assertTrue(source.read()[0])
            ok, frame = source.read()
            self.assertFalse(ok, 'the pipeline must observe the failed live read')
            self.assertIsNone(frame)
            ok, frame = source.read()
            self.assertTrue(ok)
            self.assertEqual(int(frame[0, 0, 0]), 90)
            source.release()


if __name__ == '__main__':
    unittest.main()
